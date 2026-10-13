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
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

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

    async def stream(
        self, messages, tools=None, tool_choice="auto", max_output_tokens=None
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "max_output_tokens": max_output_tokens,
            }
        )
        script = self._scripts.pop(0) if self._scripts else [_done()]
        for event in script:
            if isinstance(event, (int, float)):
                await asyncio.sleep(event)
            elif isinstance(event, BaseException):
                raise event  # a provider drop mid-stream
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
        greeting_trigger=GREETING_TRIGGER,
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
    broadcasts: list = []

    async def broadcast_interruption():
        broadcasts.append(True)

    loop.broadcast_interruption = broadcast_interruption
    return SimpleNamespace(
        loop=loop,
        llm=loop._llm,
        ctx=provider,
        recorder=recorder,
        traces=traces,
        emitted=emitted,
        pushed=pushed,
        broadcasts=broadcasts,
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


def test_filler_fires_at_call_arrival_before_any_handler(monkeypatch):
    """Atomic branch, threshold crossed: a silent tool round gets a
    FILLER_LINES entry at the call's arrival, before any handler runs; it
    prefixes the step's context entry and stamps step.N.filler."""
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 0)
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
    # round-3: this branch exists BECAUSE no interruption broadcast fired —
    # the loop must fire one itself, or the abandoned step's queued filler
    # plays over the new turn's answer
    assert h.broadcasts == [True]


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
    # since round 4 the crashed round is appended via append_step (calls
    # and terminal results included), text unstripped
    assert [a.strip() for a in assistants] == ["Here's the thing."]


def test_teardown_cleanup_finalizes_the_cut_turn():
    """Round-2 blocking 3: End-tap/disconnect is the third interruption
    path — the pipeline's cleanup() must run the same finalize as a
    barge-in, so the cut step gets its interrupted trace (FR-49: the
    cut-off turns are precisely the ones a trace is for)."""
    h = make_loop([[LLMTextDelta("Halfway th"), 60]])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("talk"))
        h.loop._turn_task = task
        await asyncio.sleep(0.05)
        h.loop.note_spoken_sentence("Halfway th")
        await h.loop.cleanup()  # what pipeline teardown calls

    asyncio.run(run())
    traces = _queued(h.traces, LLMTrace)
    assert len(traces) == 1
    assert traces[0].finish_reason == "interrupted"


def test_finished_read_with_empty_result_is_not_relabeled_cancelled():
    """Round-2 nit: results are checked with `is not None`, never
    truthiness — a finished read returning {} must keep its result."""
    write = write_tool(delay=60, emit_on_done=False)
    empty_read = read_tool(name="probe", result={})
    scripts = [
        [
            LLMToolCall(name="probe", arguments={}, id="r1"),
            LLMToolCall(name="save", arguments={}, id="w1"),
            _done(),
        ]
    ]
    h = make_loop(scripts, tools=[write, empty_read])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("go"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)  # read finished (empty dict), write hung
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    results = {
        m["tool_call_id"]: m["content"] for m in h.ctx.build() if m["role"] == "tool"
    }
    assert results["r1"] == {}  # kept, not {"status": "cancelled"}
    assert results["w1"] == {"status": "in_progress"}


# ---------------- review round 3 ----------------


def test_provider_error_mid_stream_still_writes_a_trace_row():
    """Round-3 blocking 1: a call that RAISES mid-stream (provider drop
    after deltas) writes its trace row before the loop degrades — FR-49's
    'every call writes one', and this is the call a trace is most for."""
    scripts = [
        [LLMTextDelta("I was sayi"), RuntimeError("connection dropped")],
        [LLMTextDelta("Recovered."), _done()],
    ]
    h = make_loop(scripts)

    async def run():
        await h.loop._run_turn("first")
        await h.loop._run_turn("second")

    asyncio.run(run())
    traces = _queued(h.traces, LLMTrace)
    assert [t.finish_reason for t in traces] == ["error", "stop"]
    assert "I was sayi" in traces[0].output
    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
    assert "Recovered." in _pushed_text(h)


def test_wrap_up_call_returning_tools_never_executes_them():
    """Round-3 should-fix 4: tool_choice none is a request — a wrap-up
    that comes back with tool calls must not run them or end silently."""
    executed = []

    async def spy(args, ctx):
        executed.append(True)
        return {}

    scripts = [
        [LLMToolCall(name="echo", arguments={}, id=f"c{i}"), _done()]
        for i in range(loop_mod.MAX_STEPS)
    ]
    # the forced final call misbehaves: tool call, no text
    scripts.append([LLMToolCall(name="echo", arguments={}, id="cx"), _done()])
    h = make_loop(scripts, tools=[read_tool(handler=spy)])

    asyncio.run(h.loop._run_turn("go"))

    assert len(executed) == loop_mod.MAX_STEPS  # the cap held; not one more
    assert len(h.llm.calls) == loop_mod.MAX_STEPS + 1
    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)  # never silence


def test_audio_pending_does_not_latch_across_turns(monkeypatch):
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 0)
    """Round-3 should-fix 3: a barge-in after text was forwarded but
    before playback started leaves no BotStoppedSpeakingFrame — the next
    turn's silent tool round must still get its filler."""
    scripts = [
        [LLMTextDelta("Queued but never played. "), 60],
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("first"))
        h.loop._turn_task = task
        await asyncio.sleep(0.05)  # text forwarded, no BotStarted/Stopped
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()
        await h.loop._run_turn("second")  # silent tool round

    asyncio.run(run())
    assert any(f in _pushed_text(h) for f in FILLER_LINES)


def test_short_spoken_sentence_survives_the_earlier_step_filter():
    """Round-3 nit 8: a genuinely-spoken short 'Okay.' must not be eaten
    by containment against an earlier step's text that happens to include
    the same word."""
    scripts = [
        [
            LLMTextDelta("Okay. Let me check that for you. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ],
        [LLMTextDelta("Okay. "), 60],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("go"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)  # step 2 streaming its own "Okay. "
        h.loop.note_spoken_sentence("Okay.")  # step 2's audio, genuinely new
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    last = h.ctx.build()[-1]
    assert last["role"] == "assistant"
    assert last["content"].startswith("Okay.")  # kept, sentinel-marked


def test_raising_read_does_not_discard_a_concurrent_writes_result():
    """Round-4 blocking: gather doesn't cancel siblings — a read that
    raises while a write commits must still leave the round in the
    context (the write's row is committed AND announced; a blank context
    means the next turn re-creates it)."""
    scripts = [
        [
            LLMToolCall(name="save", arguments={}, id="w1"),
            LLMToolCall(name="lookup", arguments={}, id="r1"),
            _done(),
        ],
        [LLMTextDelta("Next turn."), _done()],
    ]
    h = make_loop(
        scripts,
        tools=[
            write_tool(delay=0.05),
            read_tool(name="lookup", raises=RuntimeError("db blip")),
        ],
    )

    async def run():
        await h.loop._run_turn("do both")
        await asyncio.sleep(0.15)  # the write lands in its placeholder
        await h.loop._run_turn("next")

    asyncio.run(run())
    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
    results = {
        m["tool_call_id"]: m["content"] for m in h.ctx.build() if m["role"] == "tool"
    }
    assert results["w1"]["status"] == "created"  # recorded, not vanished
    assert results["r1"]["status"] == "error"
    # and the NEXT turn's call could see the round
    step2_input = h.llm.calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in step2_input)


def test_cleanup_cancels_wedged_abandoned_reads():
    """Round-4 nit 8: outstanding reads are cancelled at cleanup — never
    destroyed-task noise after the session."""
    read = read_tool(name="lookup", delay=60)
    scripts = [[LLMToolCall(name="lookup", arguments={}, id="r1"), _done()]]
    h = make_loop(scripts, tools=[read])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("go"))
        h.loop._turn_task = task
        await asyncio.sleep(0.05)
        assert len(h.loop._live_reads) == 1
        await h.loop.cleanup()
        await asyncio.sleep(0)  # let the cancellations land
        assert all(t.cancelled() or t.done() for t in list(h.loop._live_reads) + [])
        assert not h.loop._live_reads or all(
            t.cancelling() or t.cancelled() for t in h.loop._live_reads
        )

    asyncio.run(run())


# ---------------- feature 3.1 ride-alongs (round-5 findings) ----------------


def test_greeting_refuses_tool_calls_despite_mistranslation():
    """Round-5 finding 1: the greeting call (tool_choice none, FR-7's
    guard) refuses to execute tool calls exactly like the wrap-up does —
    a session must never open by running list_artifacts."""
    executed = []

    async def spy(args, ctx):
        executed.append(True)
        return {"artifacts": []}

    scripts = [
        [LLMToolCall(name="list_artifacts", arguments={}, id="g1"), _done()]
    ]
    h = make_loop(scripts, tools=[read_tool(name="list_artifacts", handler=spy)])
    asyncio.run(h.loop._run_turn(None))  # the greeting

    assert executed == []  # never ran
    assert len(h.llm.calls) == 1
    text = _pushed_text(h)
    assert any(line in text for line in FALLBACK_GREETING_LINES)  # still greets


def test_whitespace_only_generation_is_an_empty_step():
    """Round-5 finding 2: a "\n"-only response takes the empty-step
    fallback — never a silent turn, never an effectively-empty assistant
    message (the shape that 400s the Claude swap)."""
    h = make_loop([[LLMTextDelta(" \n  "), _done()]])
    asyncio.run(h.loop._run_turn("go"))

    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
    roles = [m["role"] for m in h.ctx.build()]
    assert roles == ["system", "user"]  # nothing appended for the turn


# ---------------- PR #19 review round 1 ----------------


def test_whitespace_text_with_forbidden_tool_call_takes_the_fallback():
    """Review blocking 3: the refusal branch strips too — whitespace text
    plus a forbidden call must not open mute with an empty append."""
    h = make_loop(
        [[LLMTextDelta(" \n"), LLMToolCall(name="echo", arguments={}, id="g1"), _done()]],
        tools=[read_tool()],
    )
    asyncio.run(h.loop._run_turn(None))  # the greeting
    assert any(line in _pushed_text(h) for line in FALLBACK_GREETING_LINES)
    roles = [m["role"] for m in h.ctx.build()]
    assert "assistant" not in roles  # nothing empty appended


def test_no_filler_on_tool_choice_none_calls():
    """Review: a refused call never runs a tool round, so the FR-43
    backstop must not speak "One moment." for it — a mistranslated
    greeting opens with the greeting fallback alone."""
    h = make_loop(
        [[LLMToolCall(name="echo", arguments={}, id="g1"), _done()]],
        tools=[read_tool()],
    )
    asyncio.run(h.loop._run_turn(None))
    text = _pushed_text(h)
    assert not any(f in text for f in FILLER_LINES)
    assert any(line in text for line in FALLBACK_GREETING_LINES)


# ---------------- FR-60: the create gate ----------------


def _create_tool(ran):
    async def handler(args, ctx):
        ran.append(args)
        return {"status": "created", "id": 1}

    return Tool(
        name="create_document",
        description="t",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        is_write=True,
    )


def test_gated_create_refused_when_judge_says_no():
    """FR-60: on the judge's NO the handler never runs, and the model sees
    the steering refusal as the tool result — the turn still speaks."""
    ran: list = []
    scripts = [
        [
            LLMToolCall(name="create_document", arguments={"title": "x"}, id="c1"),
            _done(),
        ],
        [LLMTextDelta("NO"), _done()],  # the judge's call
        [LLMTextDelta("I added it to your notes instead."), _done()],
    ]
    h = make_loop(scripts, tools=[_create_tool(ran)])

    asyncio.run(h.loop._run_turn("note that down"))

    assert ran == []  # nothing was written
    final_messages = h.llm.calls[2]["messages"]
    tool_result = next(m for m in final_messages if m["role"] == "tool")
    assert tool_result["content"]["status"] == "refused"
    assert "edit_document" in tool_result["content"]["error"]
    assert h.llm.calls[1]["tools"] is None  # the judge call declares none
    assert "notes instead" in _pushed_text(h)


def test_gated_create_runs_on_yes_and_judge_usage_lands_on_the_turn():
    ran: list = []
    scripts = [
        [
            LLMToolCall(name="create_document", arguments={"title": "x"}, id="c1"),
            _done(),
        ],
        [LLMTextDelta("YES"), _done(usage=LLMUsage(40, 1))],
        [LLMTextDelta("Done, it's on your screen."), _done()],
    ]
    h = make_loop(scripts, tools=[_create_tool(ran)])

    asyncio.run(h.loop._run_turn("make a new doc called x"))

    assert len(ran) == 1
    # FR-45: the judge's tokens are attributed like any LLM call
    assert any(
        e.stage == "llm" and e.quantity == 40
        for e in _queued(h.recorder, UsageEvent)
    )
    # FR-49: the judge call writes its own trace row (purpose "gate")
    gate_rows = [t for t in _queued(h.traces, LLMTrace) if t.purpose == "gate"]
    assert len(gate_rows) == 1
    assert "YES" in gate_rows[0].output


def test_gate_fails_closed_on_a_judge_error():
    ran: list = []
    scripts = [
        [LLMToolCall(name="create_document", arguments={}, id="c1"), _done()],
        [RuntimeError("judge provider drop")],
        [LLMTextDelta("Want me to start a new doc for this?"), _done()],
    ]
    h = make_loop(scripts, tools=[_create_tool(ran)])

    asyncio.run(h.loop._run_turn("note it"))

    assert ran == []  # a judge failure must never become an unwanted write
    final_messages = h.llm.calls[2]["messages"]
    tool_result = next(m for m in final_messages if m["role"] == "tool")
    assert tool_result["content"]["status"] == "refused"


def test_ungated_tools_never_consult_the_judge():
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("ok"), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    asyncio.run(h.loop._run_turn("go"))

    # exactly two LLM calls: the step and the final — no judge between
    assert len(h.llm.calls) == 2


# ---------------- FR-43 amended: the filler latency threshold ----------------


def test_no_filler_for_an_instant_tool_round(monkeypatch):
    """2026-10-11 live sessions: an instant edit spoke "One moment." welded
    to "Done". Under the threshold, a round that finishes and speaks before
    the delay elapses gets NO filler at all."""
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 120)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        await h.loop._run_turn("remove that line")
        # sit past the delay: the armed task must see the ended turn /
        # queued audio and stay silent, not fire late into the next turn
        await asyncio.sleep(0.2)

    asyncio.run(run())
    text = _pushed_text(h)
    assert "Done." in text
    assert not any(f in text for f in FILLER_LINES)
    rows = _queued(h.recorder, TurnMetric)
    assert not any("filler" in k for r in rows for k in r.stages_ms)


def test_filler_fires_once_dead_air_crosses_the_threshold(monkeypatch):
    """The other half: a round still silent past the delay gets the filler
    (the backstop still exists — the threshold only prices it)."""
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 50)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Here it is."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(delay=0.25)])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("find that"))
        await asyncio.sleep(0.12)  # past the 50ms delay: filler spoken
        h.recorder.record_measurement(700)  # first audio: the filler itself
        await turn

    asyncio.run(run())
    text = _pushed_text(h)
    assert any(f in text for f in FILLER_LINES)
    # and the filler-led turn is still marked, never laundered as healthy
    rows = _queued(h.recorder, TurnMetric)
    assert rows and any("filler" in k for k in rows[0].stages_ms)


def test_filler_dies_with_its_tool_round(monkeypatch):
    """2026-10-11 live (turn 5): the timer armed during an instant tool
    round survived into step 2 and spoke "One moment." AFTER the edit was
    already on screen, welded to "Done.". A filler armed for a round must
    not fire once that round is over — the only remaining wait is speech."""
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 50)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [0.2, LLMTextDelta("Done."), _done()],  # slow closing words
    ]
    h = make_loop(scripts, tools=[read_tool()])  # instant tool

    asyncio.run(h.loop._run_turn("make the two lists"))

    text = _pushed_text(h)
    assert "Done." in text
    assert not any(f in text for f in FILLER_LINES)


# ---------------- FR-61: playback-aware interruption context ----------------


def test_post_turn_barge_in_amends_unheard_tail():
    """FR-61: the turn COMPLETED, playback was cut after one sentence — the
    context entry truncates to the uttered prefix with the cut-off mark."""
    scripts = [[LLMTextDelta("The first point. And the second point."), _done()]]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    # playback delivered only the first sentence before the barge-in
    h.loop.note_uttered_sentence("The first point.")

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert last["role"] == "assistant"
    assert last["content"] == "The first point." + INTERRUPTED_SUFFIX
    assert "second" not in last["content"]


def test_fully_heard_playback_is_never_amended():
    """Pipecat broadcasts an interruption on EVERY user speech start, so the
    everything-played case (every ordinary turn) must be a strict no-op."""
    scripts = [[LLMTextDelta("Short answer."), _done()]]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    h.loop.note_uttered_sentence("Short answer.")

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert last["content"] == "Short answer."
    assert INTERRUPTED_SUFFIX not in last["content"]


def test_nothing_heard_keeps_the_text_and_adds_the_mark():
    """Round-1 review: the no-match fallback must NEVER discard the text —
    a single-sentence reply cut mid-play (empty uttered stream) keeps its
    content, annotated, so "what did you say?" still has an answer."""
    scripts = [[LLMTextDelta("Never played at all."), _done()]]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    # no uttered sentences: the barge-in landed before any audio

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert "Never played at all." in last["content"]
    assert last["content"].endswith(INTERRUPTED_SUFFIX)


def test_uttered_buffer_resets_at_the_next_turn():
    """The buffer spans one turn plus its playback tail — sentences from an
    older turn must not vouch for a newer entry."""
    scripts = [
        [LLMTextDelta("Old turn text."), _done()],
        [LLMTextDelta("New turn text."), _done()],
    ]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("one"))
    h.loop.note_uttered_sentence("Old turn text.")
    asyncio.run(h.loop._run_turn("two"))  # resets the buffer
    # nothing of the new turn played yet
    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert "New turn text." in last["content"]  # text kept, never erased
    assert last["content"].endswith(INTERRUPTED_SUFFIX)


def test_mid_generation_interruption_path_is_untouched():
    """A LIVE turn still takes the FR-46 finalize path (spoken prefix +
    suffix), never the post-turn amend."""
    scripts = [[LLMTextDelta("Started saying this "), 30.0, LLMTextDelta("x"), _done()]]
    h = make_loop(scripts)

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        h.loop._turn_task = turn  # as _on_context_frame registers it
        await asyncio.sleep(0.05)
        h.loop.note_spoken_sentence("Started saying this")
        await h.loop._start_interruption()
        await asyncio.gather(turn, return_exceptions=True)

    asyncio.run(run())
    last = h.ctx.build()[-1]
    assert last["content"].endswith(INTERRUPTED_SUFFIX)


def test_second_round_still_gets_its_filler(monkeypatch):
    """Round-1 review: a stale pending filler task must not starve the
    NEXT round — step 1's instant tool left a sleeping task, and step 2's
    slow tool then got six seconds of silence."""
    monkeypatch.setattr(loop_mod, "FILLER_DELAY_MS", 60)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMToolCall(name="slow", arguments={}, id="c2"), _done()],
        [LLMTextDelta("Found it."), _done()],
    ]
    h = make_loop(
        scripts, tools=[read_tool(), read_tool(name="slow", delay=0.3)]
    )

    asyncio.run(h.loop._run_turn("find that"))

    text = _pushed_text(h)
    assert any(f in text for f in FILLER_LINES)


def test_observer_feeds_only_transport_released_tts_text():
    """Round-1 review: FR-61's signal rests on the frame contract — a
    TTSTextFrame counts ONLY when the output transport releases it (its
    audio played); the same frame on its earlier TTS-side hops must not,
    and the release itself is deduped across re-observation."""
    from unittest.mock import MagicMock

    from pipecat.frames.frames import TTSTextFrame as _TTSText
    from pipecat.transports.base_output import BaseOutputTransport

    class _StubOutput(BaseOutputTransport):
        pass

    transport = object.__new__(_StubOutput)  # isinstance without __init__
    h = make_loop([])
    observer = loop_mod.AgentLoopObserver(loop=h.loop)
    frame = _TTSText(text="Played sentence.", aggregated_by="sentence")

    def pushed(source, direction=FrameDirection.DOWNSTREAM):
        return FramePushed(
            source=source,
            destination=MagicMock(),
            frame=frame,
            direction=direction,
            timestamp=0,
        )

    async def run():
        # the TTS->transport hop (non-transport source): ignored, unmarked
        await observer.on_push_frame(pushed(MagicMock()))
        assert h.loop._uttered == []
        # the transport's release: counts exactly once
        await observer.on_push_frame(pushed(transport))
        await observer.on_push_frame(pushed(transport))  # re-observed hop
        # upstream direction from the transport: ignored
        await observer.on_push_frame(
            pushed(transport, FrameDirection.UPSTREAM)
        )

    asyncio.run(run())
    assert h.loop._uttered == ["Played sentence."]


def test_two_gated_calls_in_one_step_get_indexed_stage_keys():
    """Round-2 review: FR-47's anti-overwrite rule holds for the gate key
    — two create_document calls in one step must not collapse into one
    stage entry ("create two lists" is exactly this turn shape)."""
    scripts = [
        [
            LLMToolCall(name="create_document", arguments={}, id="c1"),
            LLMToolCall(name="create_document", arguments={}, id="c2"),
            _done(),
        ],
        [LLMTextDelta("NO"), _done()],  # judge for one call
        [LLMTextDelta("NO"), _done()],  # judge for the other
        [LLMTextDelta("Added to your notes."), _done()],
    ]
    h = make_loop(scripts, tools=[_create_tool([])])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("create two lists"))
        await asyncio.sleep(0.05)
        h.recorder.record_measurement(700)
        await turn

    asyncio.run(run())
    rows = _queued(h.recorder, TurnMetric)
    assert rows
    keys = set(rows[0].stages_ms)
    assert "step.1.gate.create_document.1" in keys
    assert "step.1.gate.create_document.2" in keys


def test_barged_in_judge_still_writes_its_interrupted_trace():
    """Round-2 review: FR-49 names the cancellation path as the row that
    must exist — a judge cut by barge-in records purpose=gate with
    finish_reason=interrupted before the cancel propagates."""
    scripts = [
        [LLMToolCall(name="create_document", arguments={}, id="c1"), _done()],
        [asyncio.CancelledError()],  # the judge's stream is cut mid-call
    ]
    h = make_loop(scripts, tools=[_create_tool([])])

    async def run():
        try:
            await h.loop._run_turn("note it")
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(run()) is True
    gate_rows = [t for t in _queued(h.traces, LLMTrace) if t.purpose == "gate"]
    assert len(gate_rows) == 1
    assert gate_rows[0].finish_reason == "interrupted"


def test_fr61_valve_skips_amend_when_audio_played_but_nothing_released():
    """Round-2 review: audio demonstrably played this turn but the uttered
    buffer is empty — either the transport signal is broken or the cut
    landed inside the first sentence. Both must be a no-op, never a mark
    on every turn."""
    scripts = [[LLMTextDelta("A full reply."), _done()]]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    h.loop.note_bot_speaking(True)
    h.loop.note_bot_speaking(False)

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert last["content"] == "A full reply."
    assert INTERRUPTED_SUFFIX not in last["content"]


def test_filtered_stream_mismatch_is_a_no_op_not_a_cut_mark():
    """Round-2 blocker: the TTS sanitizer rewrites markdown before
    synthesis, so the raw entry can never prefix-match the spoken stream
    — a fully-heard reply must not be recorded as interrupted."""
    scripts = [[LLMTextDelta("Two things: **scope** and timing."), _done()]]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    h.loop.note_uttered_sentence("Two things: scope and timing.")

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert last["content"] == "Two things: **scope** and timing."


def test_single_word_coincidence_never_truncates():
    """Round-2 nit: the uttered stream ending with the reply's first word
    is coincidence territory — a 2-word floor keeps a mostly-unheard
    reply from collapsing to one word (old code truncated to 'point.')."""
    scripts = [
        [LLMTextDelta("point. And the rest was never played."), _done()]
    ]
    h = make_loop(scripts)
    asyncio.run(h.loop._run_turn("go"))
    h.loop.note_uttered_sentence("First point.")  # ends with 'point.'

    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    # no k>=2 prefix matches and the stream is non-empty: safe no-op
    assert last["content"] == "point. And the rest was never played."


def test_pipecat_contract_for_fr61_still_holds():
    """Round-2 blocker (round-1 finding 2): pin the pipecat facts FR-61
    rests on, so an upgrade that moves the release site breaks HERE, not
    silently in production. (The runtime valve covers the live half.)"""
    import inspect

    from pipecat.transports import base_output

    src = inspect.getsource(base_output)
    # non-audio frames are held behind their preceding audio...
    assert "handle_sync_frame" in src
    assert "preceding audio" in src
    # ...and an interruption discards what never played
    assert "_audio_queue.reset()" in src

    from pipecat.processors import frame_processor

    fsrc = inspect.getsource(frame_processor)
    # FramePushed carries the pushing processor as source — the
    # observer's isinstance(source, BaseOutputTransport) check needs it
    assert "FramePushed(" in fsrc
    assert "source=self" in fsrc
