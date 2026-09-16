"""AgentLoopProcessor: the §4.10 mandated tests at the loop level.

FR-42 (a)-(f): cap termination, tool timeout as a result, handler crash →
spoken fallback, NFR-10 recorder split, tool-free greeting, empty-step
fallback. FR-43: filler on a silent tool round (before any handler), never
on a plain turn. FR-44 (iii)/(iv): straggler boundary, scratch reset.
FR-46: barge-in mid-stream and mid-chain (writes finish and land in
place, reads abandoned, no further LLM call). FR-47 (i)/(iv): one
step-indexed row, usage once per call.

The loop runs here with its frame plumbing stubbed (push_frame recorded,
tasks plain asyncio) — the pipeline integration was proven by the FR-42
spike; these tests pin the loop's own semantics.
"""

import asyncio
import json
from types import SimpleNamespace

from pipecat.frames.frames import LLMTextFrame

import agent.loop as loop_mod
from agent.context import INTERRUPTED_SUFFIX, ContextProvider
from agent.loop import AgentLoopProcessor
from agent.prompts import (
    FALLBACK_GREETING_LINES,
    FALLBACK_LINES,
    FILLER_LINES,
    GREETING_TRIGGER,
    WRAP_UP_INSTRUCTION,
)
from agent.providers import LLMDone, LLMTextDelta, LLMToolCall, LLMUsage
from agent.tools import Tool
from db.models import LLMTrace, TurnMetric, UsageEvent
from obs.trace import TraceRecorder
from obs.usage import UsageRecorder


def _done(finish="stop", usage=None):
    return LLMDone(finish, usage)


class FakeLLM:
    """Scripted FR-48 client. Each script is a list of events for one call;
    floats mean 'sleep this long mid-stream'."""

    def __init__(self, scripts):
        self._scripts = [list(s) for s in scripts]
        self.calls: list[dict] = []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.calls.append(
            {"messages": messages, "tools": tools, "tool_choice": tool_choice}
        )
        script = self._scripts.pop(0) if self._scripts else [_done()]
        for event in script:
            if isinstance(event, (int, float)):
                await asyncio.sleep(event)
            else:
                yield event


def read_tool(name="echo", handler=None, delay=0.0, result=None, raises=None):
    async def default_handler(args, ctx):
        if delay:
            await asyncio.sleep(delay)
        if raises is not None:
            raise raises
        return result if result is not None else {"status": "ok", "echo": args}

    return Tool(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        handler=handler or default_handler,
        is_write=False,
    )


def write_tool(name="save", delay=0.0, result=None, emit_on_done=True):
    async def handler(args, ctx):
        if delay:
            await asyncio.sleep(delay)
        out = result if result is not None else {"status": "created", "id": 7}
        if emit_on_done:
            await ctx.emit({"type": "artifact.created", "artifact": {"id": 7}})
        return out

    return Tool(
        name=name,
        description="test write tool",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        is_write=True,
    )


def make_loop(scripts, tools=(), current_turn=2):
    """A loop with real recorders (never started — the NFR-10 fake-queue
    half: enqueue paths touch no database) and stubbed frame plumbing."""
    recorder = UsageRecorder("s-1", "uid-a")
    recorder.current_turn = current_turn
    traces = TraceRecorder("s-1", "uid-a")
    emitted: list = []

    async def emit(data):
        emitted.append(data)

    provider = ContextProvider("base prompt", session_id="s-1")
    loop = AgentLoopProcessor(
        context=provider,
        llm=FakeLLM(scripts),
        registry=list(tools),
        session_id="s-1",
        user_id="uid-a",
        model_name="test-model",
        recorder=recorder,
        traces=traces,
        emit=emit,
        write_registry=set(),
    )
    pushed: list = []

    async def push_frame(frame, direction=None):
        pushed.append(frame)

    loop.push_frame = push_frame
    loop.create_task = asyncio.create_task

    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    loop.cancel_task = cancel_task
    return SimpleNamespace(
        loop=loop,
        llm=loop._llm,
        ctx=provider,
        recorder=recorder,
        traces=traces,
        emitted=emitted,
        pushed=pushed,
    )


def _pushed_text(h) -> str:
    return "".join(f.text for f in h.pushed if isinstance(f, LLMTextFrame))


def _queued(recorder_or_traces, model):
    rows = []
    q = recorder_or_traces._writer._queue
    while not q.empty():
        rows.append(q.get_nowait())
    for r in rows:
        q.put_nowait(r)  # non-destructive peek
    return [r for r in rows if isinstance(r, model)]


# ---------------- FR-42 ----------------


def test_a_cap_terminates_with_forced_text_only_final():
    """(a) a model that keeps calling tools terminates at the cap with a
    spoken, text-only turn — never silence, never an unbounded chain."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id=f"c{i}"), _done()]
        for i in range(loop_mod.MAX_STEPS)
    ]
    scripts.append([LLMTextDelta("Here is where we landed."), _done()])
    h = make_loop(scripts, tools=[read_tool()])

    asyncio.run(h.loop._run_turn("go"))

    assert len(h.llm.calls) == loop_mod.MAX_STEPS + 1  # the true ceiling
    final = h.llm.calls[-1]
    assert final["tool_choice"] == "none"  # selection forbidden, tools declared
    assert final["tools"]  # still declared (the portable form)
    assert any(
        m["role"] == "system" and m["content"] == WRAP_UP_INSTRUCTION
        for m in final["messages"]
    )
    assert "Here is where we landed." in _pushed_text(h)
    # the wrap-up instruction was ephemeral — never appended to the context
    assert not any(
        m.get("content") == WRAP_UP_INSTRUCTION for m in h.ctx.build()
    )


def test_b_hanging_tool_times_out_into_a_result_and_turn_speaks(monkeypatch):
    """(b) a handler hanging past the timeout yields a timeout result the
    next step sees, and the turn still speaks."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_S", 0.05)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("That took too long, sorry."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(delay=60)])

    asyncio.run(h.loop._run_turn("go"))

    step2_messages = h.llm.calls[1]["messages"]
    tool_result = next(m for m in step2_messages if m["role"] == "tool")
    assert tool_result["content"]["status"] == "timeout"
    assert "That took too long" in _pushed_text(h)


def test_c_raising_handler_degrades_to_fallback_and_next_turn_works():
    """(c) a handler that raises → spoken fallback, clean end of turn, and
    the session takes the next turn normally (the primary error path)."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Back to normal."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(raises=RuntimeError("boom"))])

    async def run():
        await h.loop._run_turn("first")
        assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
        await h.loop._run_turn("second")

    asyncio.run(run())
    assert "Back to normal." in _pushed_text(h)
    assert len(h.llm.calls) == 2


def test_d_recorder_calls_pass_the_fake_queue_no_database_split():
    """(d) NFR-10: the loop's direct recorder calls only ENQUEUE — no
    database exists in this test and nothing touches one."""
    usage = LLMUsage(prompt_tokens=50, completion_tokens=9)
    h = make_loop([[LLMTextDelta("Hi."), _done(usage=usage)]])
    asyncio.run(h.loop._run_turn("hello"))

    assert {(e.unit, e.turn_id) for e in _queued(h.recorder, UsageEvent)} == {
        ("tokens_in", 2),
        ("tokens_out", 2),
    }
    traces = _queued(h.traces, LLMTrace)
    assert len(traces) == 1
    assert traces[0].purpose == "turn"


def test_e_greeting_runs_one_toolfree_step_and_speaks():
    """(e) client connect with no user turn appended → one step runs,
    speech is produced, and NO tool call is made (FR-7's guard)."""
    handler_ran = []

    async def spy_handler(args, ctx):
        handler_ran.append(True)
        return {}

    h = make_loop(
        [[LLMTextDelta("Hey! What's on your mind?"), _done()]],
        tools=[read_tool(handler=spy_handler)],
        current_turn=1,
    )
    asyncio.run(h.loop._run_turn(None))

    assert len(h.llm.calls) == 1
    assert h.llm.calls[0]["tool_choice"] == "none"
    assert h.llm.calls[0]["tools"]  # declared, selection forbidden
    # the ephemeral trigger rides the CALL (Gemini rejects an empty
    # contents list) but never enters the context
    assert h.llm.calls[0]["messages"][-1] == {
        "role": "user",
        "content": GREETING_TRIGGER,
    }
    assert "What's on your mind?" in _pushed_text(h)
    assert handler_ran == []
    built = h.ctx.build()
    assert [m["role"] for m in built] == ["system", "assistant"]  # no user turn
    assert not any(m.get("content") == GREETING_TRIGGER for m in built)
    assert _queued(h.traces, LLMTrace)[0].purpose == "greeting"


def test_failed_greeting_falls_back_to_a_greeting_never_the_snag_line():
    """A greeting that errors or comes back empty must still SOUND like a
    greeting — "where were we" at session open reads as a resumed-session
    assumption (the exact live-test bug this pins)."""

    class ExplodingLLM:
        def __init__(self):
            self.calls = []

        async def stream(self, messages, tools=None, tool_choice="auto"):
            self.calls.append({"messages": messages})
            raise ValueError("contents are required.")
            yield  # pragma: no cover

    h = make_loop([])
    h.loop._llm = ExplodingLLM()
    asyncio.run(h.loop._run_turn(None))
    text = _pushed_text(h)
    assert any(line in text for line in FALLBACK_GREETING_LINES)
    assert not any(line in text for line in FALLBACK_LINES)

    # the empty-step path (blocked generation) takes the same greeting line
    h2 = make_loop([[_done("blocked")]])
    asyncio.run(h2.loop._run_turn(None))
    text2 = _pushed_text(h2)
    assert any(line in text2 for line in FALLBACK_GREETING_LINES)
    assert not any(line in text2 for line in FALLBACK_LINES)


def test_f_empty_step_speaks_fallback_appends_nothing_from_model():
    """(f) a blocked/empty response → the fallback line is spoken, nothing
    from the model is appended, and the session takes the next turn."""
    scripts = [
        [_done("blocked")],
        [LLMTextDelta("Recovered."), _done()],
    ]
    h = make_loop(scripts)

    async def run():
        await h.loop._run_turn("first")
        roles = [m["role"] for m in h.ctx.build()]
        assert roles == ["system", "user"]  # nothing appended for the turn
        assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
        await h.loop._run_turn("second")

    asyncio.run(run())
    assert "Recovered." in _pushed_text(h)


# ---------------- FR-43 ----------------


def test_filler_fires_at_call_arrival_before_any_handler():
    """Atomic branch: a silent tool round gets a FILLER_LINES entry at the
    call's arrival, before any handler runs; it prefixes the step's
    context entry and stamps step.N.filler."""
    order: list[str] = []

    async def slow_handler(args, ctx):
        order.append("handler")
        return {"status": "ok"}

    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(handler=slow_handler)])

    real_push = h.loop.push_frame

    async def tracking_push(frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            order.append(f"push:{frame.text}")
        await real_push(frame, direction)

    h.loop.push_frame = tracking_push

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("write it up"))
        await asyncio.sleep(0.02)
        h.recorder.record_measurement(700)  # first audio: the filler itself
        await turn

    asyncio.run(run())

    filler_pushes = [o for o in order if o.startswith("push:") and any(
        f in o for f in FILLER_LINES
    )]
    assert filler_pushes, "no filler was spoken on a silent tool round"
    assert order.index(filler_pushes[0]) < order.index("handler")
    # prefixes the step's assistant entry — one message, never a separate append
    step_msg = next(m for m in h.ctx.build() if m.get("tool_calls"))
    assert any(step_msg["content"].startswith(f) for f in FILLER_LINES)
    # the filler-led turn is marked, never laundered as healthy (FR-36/37)
    rows = _queued(h.recorder, TurnMetric)
    assert rows and "step.1.filler" in rows[0].stages_ms


def test_no_filler_when_speech_already_queued_or_playing():
    scripts = [
        [LLMTextDelta("Sure - "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("go"))
    text = _pushed_text(h)
    assert not any(f in text for f in FILLER_LINES)

    # and when the bot is mid-playback from an earlier step
    scripts2 = [
        [LLMToolCall(name="echo", arguments={}, id="c2"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h2 = make_loop(scripts2, tools=[read_tool()])
    h2.loop.note_bot_speaking(True)
    asyncio.run(h2.loop._run_turn("go"))
    assert not any(f in _pushed_text(h2) for f in FILLER_LINES)


def test_plain_slow_turn_never_gets_filler():
    """The no-tool turn — the overwhelming common case — behaves exactly
    as today: no blind deadline, no filler on a slow plain answer."""
    h = make_loop([[0.15, LLMTextDelta("Slow but plain."), _done()]])
    asyncio.run(h.loop._run_turn("hmm"))
    text = _pushed_text(h)
    assert not any(f in text for f in FILLER_LINES)
    assert "Slow but plain." in text


# ---------------- FR-44 (iii)/(iv) ----------------


def _context_frame(user_text=None):
    from pipecat.processors.aggregators.llm_context import LLMContext

    ctx = LLMContext()
    if user_text is not None:
        ctx.add_message({"role": "user", "content": user_text})
    return SimpleNamespace(context=ctx), ctx


def test_straggler_boundary_and_scratch_reset():
    """(iii) a final arriving before the next user-speech-start is dropped
    and logged; one arriving after it becomes the next turn's content.
    (iv) the scratch context holds nothing after a consumed turn."""
    h = make_loop(
        [
            [LLMTextDelta("One."), _done()],
            [LLMTextDelta("Two."), _done()],
        ]
    )

    async def run():
        frame1, scratch1 = _context_frame("real turn")
        await h.loop._on_context_frame(frame1)
        await asyncio.gather(h.loop._turn_task, return_exceptions=True)
        assert scratch1.get_messages() == []  # (iv) reset after consumption

        # straggler: no user-speech-start since the consumed turn
        frame2, scratch2 = _context_frame("Actually, forget the pricing part")
        await h.loop._on_context_frame(frame2)
        assert scratch2.get_messages() == []  # reset even when dropped
        assert len(h.llm.calls) == 1  # dropped: no second LLM call

        # after Flux's next user-speech-start: a new turn is a new turn
        h.loop.note_user_speech_start()
        frame3, _ = _context_frame("next real turn")
        await h.loop._on_context_frame(frame3)
        await asyncio.gather(h.loop._turn_task, return_exceptions=True)
        assert len(h.llm.calls) == 2

    asyncio.run(run())
    user_turns = [m["content"] for m in h.ctx.build() if m["role"] == "user"]
    assert user_turns == ["real turn", "next real turn"]


# ---------------- FR-46 ----------------


def test_interrupt_mid_stream_records_spoken_prefix_with_sentinel():
    h = make_loop([[LLMTextDelta("First sentence. "), 60]])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("talk"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)
        # the observer feeds the synthesis stream; simulate its callback
        h.loop.note_spoken_sentence("First sentence.")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    last = h.ctx.build()[-1]
    assert last["role"] == "assistant"
    assert last["content"] == "First sentence." + INTERRUPTED_SUFFIX
    trace = _queued(h.traces, LLMTrace)[0]
    assert trace.finish_reason == "interrupted"
    assert "First sentence." in trace.output


def test_interrupt_mid_chain_write_finishes_reads_abandoned_no_new_step():
    """The mandated mid-chain interrupt: the write completes and updates
    its in_progress placeholder IN PLACE, cancelled reads get terminal
    results, and no further LLM call is issued."""
    write = write_tool(delay=0.3)
    read = read_tool(name="lookup", delay=60)
    scripts = [
        [
            LLMToolCall(name="save", arguments={}, id="w1"),
            LLMToolCall(name="lookup", arguments={}, id="r1"),
            _done(),
        ],
        [LLMTextDelta("never reached"), _done()],
    ]
    h = make_loop(scripts, tools=[write, read])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("do both"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)  # handlers launched, neither finished
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

        built = h.ctx.build()
        results = {m["tool_call_id"]: m["content"] for m in built if m["role"] == "tool"}
        assert results["w1"] == {"status": "in_progress"}
        assert results["r1"] == {"status": "cancelled"}

        await asyncio.sleep(0.4)  # the write outlives the turn and lands

    asyncio.run(run())
    results = {
        m["tool_call_id"]: m["content"] for m in h.ctx.build() if m["role"] == "tool"
    }
    assert results["w1"]["status"] == "created"  # updated in place
    assert results["r1"] == {"status": "cancelled"}
    assert len(h.llm.calls) == 1  # no post-interruption step
    assert h.emitted and h.emitted[0]["type"] == "artifact.created"


def test_write_task_survives_loop_task_cancellation():
    """FR-46's ownership rule: the write runs in a task OUTSIDE the turn
    task's cancellation scope — cancelling the turn cannot roll it back."""
    finished = []

    async def handler(args, ctx):
        await asyncio.sleep(0.15)
        finished.append(True)
        return {"status": "created"}

    tool = Tool(
        name="save",
        description="",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        is_write=True,
    )
    h = make_loop(
        [[LLMToolCall(name="save", arguments={}, id="w1"), _done()]], tools=[tool]
    )

    async def run():
        task = asyncio.create_task(h.loop._run_turn("save it"))
        h.loop._turn_task = task
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._finalize_interrupted_turn()
        assert finished == []  # still running, NOT cancelled
        await asyncio.sleep(0.2)
        assert finished == [True]

    asyncio.run(run())


# ---------------- FR-47 ----------------


def test_multi_step_turn_buffers_step_indexed_stages_and_flushes_one_row():
    """(i) at the loop level: 3 steps, a repeated tool, one measurement →
    ONE TurnMetric row whose keys cover every step and both invocations."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMToolCall(name="echo", arguments={}, id="c2"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        await asyncio.sleep(0.05)
        h.recorder.record_measurement(880)  # first audio mid-turn
        await turn

    asyncio.run(run())
    rows = _queued(h.recorder, TurnMetric)
    assert len(rows) == 1
    keys = set(rows[0].stages_ms)
    assert {
        "step.1.ttfb.llm",
        "step.1.tool.echo",
        "step.2.ttfb.llm",
        "step.2.tool.echo",
        "step.3.ttfb.llm",
    } <= keys
    assert rows[0].eot_to_first_audio_ms == 880


def test_usage_lands_exactly_once_per_call():
    """(iv) a multi-step turn records LLM usage once per call — the
    metrics-frame observer no longer records LLM at all."""
    scripts = [
        [
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(usage=LLMUsage(100, 10)),
        ],
        [LLMTextDelta("Done."), _done(usage=LLMUsage(150, 20))],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("go"))

    llm_events = [
        e for e in _queued(h.recorder, UsageEvent) if e.stage == "llm"
    ]
    assert sorted((e.unit, e.quantity) for e in llm_events) == [
        ("tokens_in", 100.0),
        ("tokens_in", 150.0),
        ("tokens_out", 10.0),
        ("tokens_out", 20.0),
    ]


def test_trace_rows_cover_every_call_with_serialized_inputs():
    """FR-49 at the loop level: one trace row per call, inputs serialized
    at enqueue (the step-2 row must show the step-1 round it was sent)."""
    scripts = [
        [LLMToolCall(name="echo", arguments={"q": 1}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("go"))

    traces = _queued(h.traces, LLMTrace)
    assert [t.step for t in traces] == [1, 2]
    step2_inputs = json.loads(traces[1].input_messages)
    assert any(m.get("role") == "tool" for m in step2_inputs)
    assert "[tool_calls]" in traces[0].output


# ---------------- review-round fixes (PR #18) ----------------


def test_tool_round_barge_in_does_not_double_instrument():
    """Blocking 1: the step's LLM call was already instrumented when its
    stream closed; a barge-in during the TOOL ROUND must not re-record
    usage or write a second trace row (FR-47 iv on interrupted turns)."""
    scripts = [
        [
            LLMToolCall(name="save", arguments={}, id="w1"),
            _done(usage=LLMUsage(100, 10)),
        ]
    ]
    h = make_loop(scripts, tools=[write_tool(delay=60, emit_on_done=False)])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("save it"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)  # stream closed + instrumented, round hung
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    traces = _queued(h.traces, LLMTrace)
    assert len(traces) == 1  # the closed call's row only, no interrupted twin
    tokens_in = [
        e for e in _queued(h.recorder, UsageEvent) if e.unit == "tokens_in"
    ]
    assert len(tokens_in) == 1


def test_interrupted_step_prefix_excludes_earlier_steps_sentences():
    """Blocking 2: the spoken capture is per STEP — step 1's appended
    acknowledgment must not be duplicated into interrupted step 2's entry
    (the common barge-in shape: "say an acknowledgment, then call")."""
    scripts = [
        [
            LLMTextDelta("Let me write that up. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ],
        [LLMToolCall(name="lookup", arguments={}, id="r1"), 60, _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(), read_tool(name="lookup", delay=60)])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("go"))
        h.loop._turn_task = task
        await asyncio.sleep(0.02)
        h.loop.note_spoken_sentence("Let me write that up.")  # step 1's audio
        await asyncio.sleep(0.15)  # step 2 issued its call and hung
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    assistants = [
        m["content"] for m in h.ctx.build() if m["role"] == "assistant"
    ]
    joined = " | ".join(assistants)
    assert joined.count("Let me write that up.") == 1  # once, in step 1's entry


def test_new_turn_mid_tool_round_disposes_like_a_barge_in():
    """Should-fix 5: a new turn while the previous still runs (silent tool
    round → no interruption broadcast) must get the full FR-46 disposal:
    placeholder for the running write, no further step for turn 1."""
    scripts = [
        [LLMToolCall(name="save", arguments={}, id="w1"), _done()],
        [LLMTextDelta("Second turn answer."), _done()],
    ]
    h = make_loop(scripts, tools=[write_tool(delay=60, emit_on_done=False)])

    async def run():
        frame1, _ = _context_frame("first turn")
        await h.loop._on_context_frame(frame1)
        await asyncio.sleep(0.1)  # turn 1 hung in its write round
        h.loop.note_user_speech_start()
        frame2, _ = _context_frame("second turn")
        await h.loop._on_context_frame(frame2)
        await asyncio.gather(h.loop._turn_task, return_exceptions=True)

    asyncio.run(run())
    built = h.ctx.build()
    tool_results = {
        m["tool_call_id"]: m["content"] for m in built if m["role"] == "tool"
    }
    assert tool_results["w1"] == {"status": "in_progress"}
    assert len(h.llm.calls) == 2  # turn 1's single call + turn 2's — no extra step
    assert "Second turn answer." in _pushed_text(h)


def test_raised_write_lands_terminal_error_in_its_placeholder():
    """Should-fix 7: a write that finishes by RAISING must not leave an
    in_progress placeholder forever — a terminal error result lands in
    place, through the same machinery as a successful late write."""

    async def failing_write(args, ctx):
        await asyncio.sleep(0.15)
        raise RuntimeError("db exploded")

    tool = Tool(
        name="save",
        description="",
        parameters={"type": "object", "properties": {}},
        handler=failing_write,
        is_write=True,
    )
    h = make_loop(
        [[LLMToolCall(name="save", arguments={}, id="w1"), _done()]], tools=[tool]
    )

    async def run():
        task = asyncio.create_task(h.loop._run_turn("save it"))
        h.loop._turn_task = task
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()
        results = {
            m["tool_call_id"]: m["content"]
            for m in h.ctx.build()
            if m["role"] == "tool"
        }
        assert results["w1"] == {"status": "in_progress"}
        await asyncio.sleep(0.2)  # the write fails after the barge-in

    asyncio.run(run())
    results = {
        m["tool_call_id"]: m["content"] for m in h.ctx.build() if m["role"] == "tool"
    }
    assert results["w1"]["status"] == "error"


def test_no_text_frame_after_greeting_never_regreets():
    """Should-fix 4: the greeting happens once; a later no-text context
    frame (whitespace-only aggregation, stray LLMRunFrame) is a logged
    drop, never a mid-session re-greet with tool_choice none."""
    h = make_loop([[LLMTextDelta("Hello!"), _done()]])

    async def run():
        frame1, _ = _context_frame()  # the greeting
        await h.loop._on_context_frame(frame1)
        await asyncio.gather(h.loop._turn_task, return_exceptions=True)
        frame2, _ = _context_frame()  # stray empty frame mid-session
        await h.loop._on_context_frame(frame2)

    asyncio.run(run())
    assert len(h.llm.calls) == 1


def test_same_tool_twice_in_one_step_keeps_both_stage_entries():
    """Should-fix 6: duplicate tool names in one step must not overwrite
    each other's durations in the stage dict."""
    scripts = [
        [
            LLMToolCall(name="echo", arguments={"n": 1}, id="c1"),
            LLMToolCall(name="echo", arguments={"n": 2}, id="c2"),
            _done(),
        ],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        await asyncio.sleep(0.05)
        h.recorder.record_measurement(800)
        await turn

    asyncio.run(run())
    rows = _queued(h.recorder, TurnMetric)
    keys = set(rows[0].stages_ms)
    assert {"step.1.tool.echo.1", "step.1.tool.echo.2"} <= keys


def test_fallback_keeps_model_text_the_user_already_heard():
    """Nit (FR-43's own principle): a handler crash AFTER step text
    streamed must not vanish the heard sentence from the context."""
    scripts = [
        [
            LLMTextDelta("Here's the thing. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ]
    ]
    h = make_loop(scripts, tools=[read_tool(raises=RuntimeError("boom"))])
    asyncio.run(h.loop._run_turn("go"))
    assistants = [m["content"] for m in h.ctx.build() if m["role"] == "assistant"]
    assert assistants == ["Here's the thing."]
