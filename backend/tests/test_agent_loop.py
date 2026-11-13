"""AgentLoopProcessor: the §4.10 mandated tests at the loop level.

FR-42 (a)-(f): cap termination, tool timeout as a result, handler crash →
spoken fallback, NFR-10 recorder split, tool-free greeting, empty-step
fallback. FR-43: a silent tool round stays silent — the loop speaks no
line of its own. FR-44 (iii)/(iv): straggler boundary, scratch reset.
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

import pytest
from pipecat.frames.frames import LLMTextFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

import agent.loop as loop_mod
from agent.context import INTERRUPTED_SUFFIX, ContextProvider
from agent.loop import AgentLoopProcessor
from agent.prompts import (
    FALLBACK_GREETING_LINES,
    FALLBACK_LINES,
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


def make_loop(scripts, tools=(), current_turn=2, **loop_kwargs):
    """A loop with real recorders (never started — the NFR-10 fake-queue
    half: enqueue paths touch no database) and stubbed frame plumbing."""
    recorder = UsageRecorder("s-1", "uid-a")
    recorder.current_turn = current_turn
    traces = TraceRecorder("s-1", "uid-a")
    emitted: list = []

    async def emit(data):
        emitted.append(data)

    provider = ContextProvider("base prompt", session_id="s-1")
    llm = FakeLLM(scripts)
    # one scripted client serves both roles unless a test splits them
    loop_kwargs.setdefault("judge_llm", llm)
    loop_kwargs.setdefault("judge_model_name", "test-model")
    loop = AgentLoopProcessor(
        context=provider,
        llm=llm,
        registry=list(tools),
        session_id="s-1",
        user_id="uid-a",
        model_name="test-model",
        recorder=recorder,
        traces=traces,
        emit=emit,
        write_registry=set(),
        greeting_trigger=GREETING_TRIGGER,
        **loop_kwargs,
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


def test_a_silent_tool_round_stays_silent():
    """The canned filler line is gone (owner decision, 2026-11-12): a step
    that goes straight to a tool call speaks nothing of the loop's own,
    however long the round takes. Only model text is ever pushed, the
    step's context entry carries no invented words, and no filler stage is
    stamped on the turn."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    # 0.9s: longer than the 800ms of dead air the removed filler waited for
    h = make_loop(scripts, tools=[read_tool(delay=0.9)])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("write it up"))
        await asyncio.sleep(0.05)
        h.recorder.record_measurement(700)  # FR-47: no measurement, no row
        await turn

    asyncio.run(run())

    assert _pushed_text(h) == "Done."
    step_msg = next(m for m in h.ctx.build() if m.get("tool_calls"))
    assert step_msg["content"] == ""
    rows = _queued(h.recorder, TurnMetric)
    # exactly what the two steps measured, plus the marker that makes
    # NFR-1's known exception findable — no other stage of the loop's own
    assert rows and set(rows[0].stages_ms) == {
        "step.1.ttfb.llm",
        "step.1.tool_first",
        "step.1.tool.echo",
        "step.2.ttfb.llm",
    }


def _narrated_tool_turn(closing):
    """A step that says what it is doing and calls a tool, then `closing`."""
    return [
        [
            LLMTextDelta("I'll add that to the document. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ],
        [*closing, _done()],
    ]


def test_done_is_not_spoken_after_a_sentence_that_said_what_was_being_done():
    """2026-11-12 live session: every narrated tool turn ended "...to the
    document.Done." One acknowledgment per piece of work: the closing
    bare confirmation is not spoken and not recorded, no fallback line
    covers the quiet ending, and the trace keeps what the model wrote."""
    h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])

    asyncio.run(h.loop._run_turn("note that down"))

    text = _pushed_text(h)
    assert text == "I'll add that to the document. "
    assert not any(line in text for line in FALLBACK_LINES)
    assert not any("Done" in (m.get("content") or "") for m in h.ctx.build())
    assert [t.output for t in _queued(h.traces, LLMTrace)][-1] == "Done."


def test_a_lone_done_is_still_spoken_when_the_turn_said_nothing_else():
    """The confirmation is the only acknowledgment of silent work: kept."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("note that down"))
    assert _pushed_text(h) == "Done."
    assert h.ctx.build()[-1]["content"] == "Done."


@pytest.mark.parametrize(
    ("closing", "spoken"),
    [
        # a confirmation leading real content: only the confirmation goes
        ([LLMTextDelta("Done."), LLMTextDelta(" I split it into two lists.")], "I split it into two lists."),
        ([LLMTextDelta("Done. I split it into two lists.")], "I split it into two lists."),
        # not a confirmation: a sentence that starts with the same word
        ([LLMTextDelta("Done"), LLMTextDelta(" with the first list. Want the second?")], "Done with the first list. Want the second?"),
        # nothing like a confirmation: streams as before
        ([LLMTextDelta("Added it "), LLMTextDelta("under Timeline.")], "Added it under Timeline."),
    ],
)
def test_only_a_leading_bare_confirmation_is_dropped_from_a_closing_step(closing, spoken):
    h = make_loop(_narrated_tool_turn(closing), tools=[read_tool()])
    asyncio.run(h.loop._run_turn("note that down"))
    assert _pushed_text(h) == "I'll add that to the document. " + spoken
    assert h.ctx.build()[-1]["content"] == spoken  # the record is what was heard


# ---- PR #25 round 2: where the confirmation rule must NOT fire ----


def test_whitespace_before_a_tool_call_is_not_speech_so_done_is_spoken():
    """Round-2 blocker: Claude often emits a bare newline before a
    tool_use block. Counting that as speech dropped the closing "Done."
    and the whole turn pushed nothing audible — a silent turn that the
    metrics showed as healthy."""
    scripts = [
        [LLMTextDelta("\n"), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("note that down"))
    assert _pushed_text(h).strip() == "Done."


def test_chained_work_says_done_once_after_the_last_step_not_in_the_middle():
    """Round-2 blocker: narrate+edit, then "Done."+another edit, then
    "Done.". The middle one claimed completion while work was still
    running and was spoken; the final, true one was dropped. Now the
    middle one is dropped and the one that closes the work is spoken."""
    order: list[str] = []

    async def handler(args, ctx):
        order.append(f"tool:{args['n']}")
        return {"status": "ok"}

    scripts = [
        [LLMTextDelta("I'll add that. "), LLMToolCall(name="echo", arguments={"n": 1}, id="c1"), _done()],
        [LLMTextDelta("Done."), LLMToolCall(name="echo", arguments={"n": 2}, id="c2"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(handler=handler)])
    real_push = h.loop.push_frame

    async def tracking_push(frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            order.append(f"say:{frame.text}")
        await real_push(frame, direction)

    h.loop.push_frame = tracking_push
    asyncio.run(h.loop._run_turn("add both"))

    assert order == ["say:I'll add that. ", "tool:1", "tool:2", "say:Done."]
    # the step whose confirmation was dropped recorded no words
    steps = [m for m in h.ctx.build() if m.get("tool_calls")]
    assert [m["content"] for m in steps] == ["I'll add that. ", ""]


def test_done_after_silent_steps_in_between_is_spoken():
    """The rule is about a confirmation running into the sentence right
    before it. With silent work in between it is the acknowledgment."""
    scripts = [
        [LLMTextDelta("Let me do that. "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMToolCall(name="echo", arguments={}, id="c2"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("do both"))
    assert _pushed_text(h) == "Let me do that. Done."
    # and a tool-only step pushes no frame at all, not an empty one
    assert all(f.text for f in h.pushed if isinstance(f, LLMTextFrame))


@pytest.mark.parametrize("narrate_last_step", [False, True])
def test_the_forced_wrap_up_line_is_never_trimmed(narrate_last_step):
    """FR-42 (a): the cap's forced call exists so the turn ends spoken.
    Its "Done." is kept even directly after a step that spoke."""
    scripts = [[LLMTextDelta("Let me do that. "), LLMToolCall(name="echo", arguments={}, id="c0"), _done()]]
    for i in range(1, loop_mod.MAX_STEPS):
        last = i == loop_mod.MAX_STEPS - 1
        text = [LLMTextDelta("One more. ")] if (last and narrate_last_step) else []
        scripts.append([*text, LLMToolCall(name="echo", arguments={}, id=f"c{i}"), _done()])
    scripts.append([LLMTextDelta("Done."), _done()])  # the forced wrap-up call
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("go"))
    assert len(h.llm.calls) == loop_mod.MAX_STEPS + 1
    assert _pushed_text(h).endswith("Done.")


def test_barge_in_while_a_confirmation_is_held_records_nothing_unheard():
    """Held text was never sent for synthesis: an interruption at that
    moment must not put it in the context as something the user heard."""
    scripts = [
        [LLMTextDelta("I'll add that. "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done"), 60],  # held, then the user speaks
    ]
    h = make_loop(scripts, tools=[read_tool()])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("note that down"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    assert _pushed_text(h) == "I'll add that. "
    assert not any("Done" in (m.get("content") or "") for m in h.ctx.build())


def test_provider_drop_while_a_confirmation_is_held_records_nothing_unheard():
    scripts = [
        [LLMTextDelta("I'll add that. "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), RuntimeError("provider drop")],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    asyncio.run(h.loop._run_turn("note that down"))
    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)  # it failed: say so
    assert not any("Done" in (m.get("content") or "") for m in h.ctx.build())


def test_the_tool_first_marker_raises_no_slow_stage_advisory_of_its_own():
    """Round 2: the marker's value is the same wait as the step's TTFB,
    which is already advised when slow. Two WARNs for one wait on every
    tool-first turn would bury the signal the marker exists to explain."""
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    seen: dict[str, bool] = {}
    real = h.recorder.record_step_stage

    def spy(turn_id, key, ms, *, advisory=True):
        seen[key] = advisory
        real(turn_id, key, ms, advisory=advisory)

    h.recorder.record_step_stage = spy
    asyncio.run(h.loop._run_turn("note that down"))
    assert seen["step.1.tool_first"] is False
    assert seen["step.1.ttfb.llm"] is True


def test_done_as_a_plain_answer_is_spoken():
    """No earlier speech in the turn: nothing is held or dropped."""
    h = make_loop([[LLMTextDelta("Done."), _done()]])
    asyncio.run(h.loop._run_turn("are you done?"))
    assert _pushed_text(h) == "Done."


def test_text_before_a_tool_call_is_spoken_and_kept_with_the_step():
    """Speak-while-working, the half that remains: what the model says in
    a step that also calls a tool streams out at once and is recorded on
    that step's entry, word for word."""
    scripts = [
        [LLMTextDelta("Sure - "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Here it is."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool(delay=0.05)])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        await asyncio.sleep(0.02)
        h.recorder.record_measurement(700)  # FR-47: no measurement, no row
        await turn

    asyncio.run(run())
    assert _pushed_text(h) == "Sure - Here it is."
    step_msg = next(m for m in h.ctx.build() if m.get("tool_calls"))
    assert step_msg["content"] == "Sure - "
    # it spoke first: not the tool-first exception
    rows = _queued(h.recorder, TurnMetric)
    assert rows and not any("tool_first" in k for k in rows[0].stages_ms)


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


def test_interrupted_call_still_records_the_input_it_was_billed_for():
    """PR #24 round 2: Claude reports input tokens before the first
    output token. A barge-in used to drop them (usage only ever left the
    client on the final event), so the usage record under-counted exactly
    the turns that were cut."""
    h = make_loop([[LLMUsage(6172, 0), LLMTextDelta("First sentence. "), 60]])

    async def run():
        task = asyncio.create_task(h.loop._run_turn("talk"))
        h.loop._turn_task = task
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())
    trace = _queued(h.traces, LLMTrace)[0]
    assert trace.finish_reason == "interrupted"
    assert (trace.prompt_tokens, trace.completion_tokens) == (6172, 0)
    assert any(
        e.stage == "llm" and e.quantity == 6172 for e in _queued(h.recorder, UsageEvent)
    )


def test_early_usage_does_not_count_as_the_first_token():
    """The early usage event arrives with the response headers; time to
    first token must still be measured to real output."""
    h = make_loop([[LLMUsage(100, 0), 0.15, LLMTextDelta("Hello."), _done()]])

    asyncio.run(h.loop._run_turn("hi"))

    trace = _queued(h.traces, LLMTrace)[0]
    assert trace.ttfb_ms >= 140
    # LLMDone carried no usage here: the early count is kept, not erased
    assert trace.prompt_tokens == 100


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
    # the loop must fire one itself, or the abandoned step's queued speech
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
    """FR-42 (heard words never vanish): a handler crash AFTER step text
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


@pytest.mark.parametrize("finish", ["truncated", "blocked"])
def test_tool_calls_from_an_incomplete_generation_are_not_run(finish):
    """PR #24 round 1: a length-cut or declined generation can carry a
    half-written tool input, or one the provider says to discard. The
    write must not run; the turn says so and ends."""
    executed = []

    async def spy(args, ctx):
        executed.append(args)
        return {"status": "created"}

    tool = Tool(
        name="save",
        description="test write tool",
        parameters={"type": "object", "properties": {}},
        handler=spy,
        is_write=True,
    )
    scripts = [
        [
            LLMTextDelta("Writing that up."),
            LLMToolCall(name="save", arguments={"content": "half a docu"}, id="c1"),
            LLMDone(finish, None),
        ],
        [LLMTextDelta("Recovered."), _done()],
    ]
    h = make_loop(scripts, tools=[tool])

    async def run():
        await h.loop._run_turn("write it up")
        await h.loop._run_turn("again")

    asyncio.run(run())

    assert executed == []  # nothing was written
    assert any(line in _pushed_text(h) for line in FALLBACK_LINES)
    built = h.ctx.build()
    # no tool round entered the context; the words already spoken did
    assert not any(m["role"] == "tool" for m in built)
    assert any(
        m["role"] == "assistant" and "Writing that up." in m["content"] for m in built
    )
    assert "Recovered." in _pushed_text(h)  # the session carries on


@pytest.mark.parametrize(
    ("turn_text", "script", "fallback_lines"),
    [
        # a length-cut step carrying a call (the incomplete-step branch)
        (
            "write it up",
            [
                LLMTextDelta("Writing that up. "),
                LLMToolCall(name="save", arguments={"content": "half"}, id="c1"),
                LLMDone("truncated", None),
            ],
            FALLBACK_LINES,
        ),
        # the greeting returning a call despite tool_choice none
        (
            None,
            [LLMToolCall(name="save", arguments={}, id="g1"), _done()],
            FALLBACK_GREETING_LINES,
        ),
    ],
)
def test_barge_in_during_the_fallback_line_records_no_phantom_tool_round(
    turn_text, script, fallback_lines
):
    """PR #24 round 3: the skipped calls stayed on the turn state, so a
    barge-in while the fallback line was being pushed disposed them as a
    tool round that had been cut — an assistant message carrying a
    tool_use the model never got to run, plus a 'cancelled' result."""
    h = make_loop([script], tools=[write_tool()])
    reached = asyncio.Event()

    async def push_frame(frame, direction=None):
        h.pushed.append(frame)
        if isinstance(frame, LLMTextFrame) and frame.text in fallback_lines:
            reached.set()
            await asyncio.sleep(60)  # the barge-in lands here

    h.loop.push_frame = push_frame

    async def run():
        task = asyncio.create_task(h.loop._run_turn(turn_text))
        h.loop._turn_task = task
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        h.loop._turn_task = None
        h.loop._finalize_interrupted_turn()

    asyncio.run(run())

    built = h.ctx.build()
    assert not any(m["role"] == "tool" for m in built)
    assert not any(m.get("tool_calls") for m in built)


def test_loop_cannot_be_built_without_its_judge_client():
    """PR #24 round 1: with a default of 'the loop's own client', a
    dropped kwarg would run every verdict on the loop model under a
    green suite."""
    with pytest.raises(TypeError, match="judge_llm"):
        AgentLoopProcessor(
            context=ContextProvider("base", session_id="s-1"),
            llm=FakeLLM([]),
            registry=[],
            session_id="s-1",
            user_id="uid-a",
            model_name="test-model",
            recorder=UsageRecorder("s-1", "uid-a"),
            traces=TraceRecorder("s-1", "uid-a"),
            emit=None,
            write_registry=set(),
            greeting_trigger=GREETING_TRIGGER,
        )


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


def test_judge_cut_by_its_timeout_still_records_the_input_it_was_billed_for(
    monkeypatch,
):
    """PR #24 round 3: usage left the judge only on its final event, so a
    judge call past its timeout — the slow calls, the ones worth seeing —
    wrote a trace row with no tokens and no usage row at all."""
    import agent.gate as gate_mod

    monkeypatch.setattr(gate_mod, "JUDGE_TIMEOUT_S", 0.05)
    ran: list = []
    judge = FakeLLM([[LLMUsage(340, 0), 60]])
    scripts = [
        [LLMToolCall(name="create_document", arguments={"title": "x"}, id="c1"), _done()],
        [LLMTextDelta("Want me to start a new document?"), _done()],
    ]
    h = make_loop(scripts, tools=[_create_tool(ran)], judge_llm=judge)

    asyncio.run(h.loop._run_turn("note it"))

    assert ran == []  # still fails closed
    gate_row = next(t for t in _queued(h.traces, LLMTrace) if t.purpose == "gate")
    assert (gate_row.finish_reason, gate_row.prompt_tokens) == ("error", 340)
    assert [
        e.quantity for e in _queued(h.recorder, UsageEvent) if e.quantity == 340
    ] == [340]  # counted once


def test_gate_judge_runs_on_its_own_client_and_model():
    """The judge may be a smaller model than the loop's: its call goes
    through the judge client and its trace row names the judge model,
    while the turn's own steps stay on the loop client."""
    ran: list = []
    judge = FakeLLM([[LLMTextDelta("YES"), _done()]])
    scripts = [
        [
            LLMToolCall(name="create_document", arguments={"title": "x"}, id="c1"),
            _done(),
        ],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(
        scripts,
        tools=[_create_tool(ran)],
        judge_llm=judge,
        judge_model_name="judge-model",
    )

    asyncio.run(h.loop._run_turn("make a new doc called x"))

    assert len(ran) == 1
    assert len(judge.calls) == 1
    assert judge.calls[0]["tools"] is None
    assert len(h.llm.calls) == 2  # the loop client never saw the judge call
    rows = _queued(h.traces, LLMTrace)
    assert [t.model for t in rows if t.purpose == "gate"] == ["judge-model"]
    assert {t.model for t in rows if t.purpose != "gate"} == {"test-model"}


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
    h.loop.note_bot_spoke()

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
    """Round-2 nit (floor now 15 CHARS, round 3): the uttered stream
    ending with the reply's first word is coincidence territory — the
    character floor keeps a mostly-unheard reply from collapsing to one
    word (old code truncated to 'point.')."""
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
    # non-audio frames are held behind their preceding audio (the
    # structural half only — a docstring reword must not red CI)...
    assert "handle_sync_frame" in src
    # ...and an interruption discards what never played
    assert "_audio_queue.reset()" in src

    from pipecat.processors import frame_processor

    fsrc = inspect.getsource(frame_processor)
    # FramePushed carries the pushing processor as source — the
    # observer's isinstance(source, BaseOutputTransport) check needs it
    assert "FramePushed(" in fsrc
    assert "source=self" in fsrc


def test_an_earlier_steps_audio_never_vouches_for_the_unheard_closing_reply():
    """Round-3 blocker: an earlier step's sentence played, the closing
    reply never did. The entry-scoped slice means the reply
    annotates (intact + mark) instead of hiding behind the mismatch
    valve as "heard"."""
    monkey_delay = 0.2

    scripts = [
        [LLMTextDelta("Let me look. "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("You have the launch plan and the packing list."), _done()],
    ]

    async def slow_handler(args, ctx):
        await asyncio.sleep(monkey_delay)
        return {"status": "ok"}

    h = make_loop(scripts, tools=[read_tool(handler=slow_handler)])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        await asyncio.sleep(0.1)
        # step 1's sentence (an EARLIER entry's audio) is released
        # mid-round, BEFORE the closing reply is appended
        h.loop.note_uttered_sentence("Let me look.")
        await turn

    asyncio.run(run())
    # the closing reply's own slice is empty: nothing of IT played
    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert "launch plan and the packing list" in last["content"]
    assert last["content"].endswith(INTERRUPTED_SUFFIX)


@pytest.mark.parametrize("step_text", ["", "Checking what you have. "])
def test_last_assistant_text_ignores_the_current_turns_own_steps(step_text):
    """What the agent says while working on the user's latest turn is
    not what that turn responded to: a list_documents round before the
    create retry must not replace the offer as the judge's referent."""
    provider = ContextProvider("base", session_id="s-t")
    provider.append_user("put that somewhere separate")
    provider.append_assistant("Do you want a new document for this?")
    provider.append_user("yes")
    provider.append_step(
        step_text,  # silent, as a tool step usually is, or narrated
        [LLMToolCall(name="list_documents", arguments={}, id="c9")],
        [{"status": "ok"}],
    )
    assert (
        provider.last_assistant_text()
        == "Do you want a new document for this?"
    )


def test_last_assistant_text_is_none_after_a_turn_that_only_worked_silently():
    """A tool step with no speech is now the ordinary shape of a step
    entry (content ""). A previous turn that left only such an entry said
    nothing the user could be answering: the judge must get None — never
    "", and never whatever was said before that turn."""
    provider = ContextProvider("base", session_id="s-t")
    provider.append_user("want a new doc for the hiring plan?")
    provider.append_assistant("Do you want a new document for this?")
    provider.append_user("no, add milk to the shopping list")
    provider.append_step(
        "",
        [LLMToolCall(name="edit_document", arguments={"document_id": 1}, id="c1")],
        [{"status": "in_progress"}],
        interrupted=True,  # cut before the closing "Done."
    )
    provider.append_user("yeah")
    assert provider.last_assistant_text() is None


def test_last_assistant_text_keeps_an_offer_spoken_in_a_tool_call_step():
    """PR #24 round 4 blocker: Sonnet speaks before tool calls, so the
    offer can sit in the same step entry as a call. The user answers over
    the tool round; skipping the entry left the judge with no offer and
    the bare 'Yeah.' with no referent — the refused-after-yes bug again."""
    provider = ContextProvider("base", session_id="s-t")
    provider.append_user("note that down")
    provider.append_step(
        "",
        [LLMToolCall(name="create_document", arguments={"title": "x"}, id="c1")],
        [{"status": "refused"}],
    )
    provider.append_step(
        "Nothing here fits. Do you want a new document for this?",
        [LLMToolCall(name="list_documents", arguments={}, id="c2")],
        [{"status": "cancelled"}],
        interrupted=True,
    )
    provider.append_user("Yeah.")
    assert "Do you want a new document for this?" in provider.last_assistant_text()


def test_last_assistant_text_never_returns_an_offer_from_before_an_earlier_turn():
    """PR #24 round 2 blocker: the judge is told this text is what the
    latest turn responds to. An offer the user DECLINED two turns ago,
    followed by a turn that left no spoken reply (barge-in during the
    tool round), must not be paired with a later unrelated 'yeah'."""
    provider = ContextProvider("base", session_id="s-t")
    provider.append_user("capture the hiring plan")
    provider.append_assistant("No existing document fits. Do you want a new document for this?")
    provider.append_user("no, add it to the launch plan")
    provider.append_step(
        "",
        [LLMToolCall(name="edit_document", arguments={"document_id": 1}, id="c1")],
        [{"status": "in_progress"}],
        interrupted=True,
    )
    provider.append_user("yeah, that's fine")
    assert provider.last_assistant_text() is None
    # and still None once the current turn's own tool step is in
    provider.append_step(
        "Checking. ",
        [LLMToolCall(name="list_documents", arguments={}, id="c2")],
        [{"status": "ok"}],
    )
    assert provider.last_assistant_text() is None


def test_last_assistant_text_keeps_the_end_of_a_long_reply():
    """PR #24 round 1: a reply explains first and asks last. Capped from
    the head, a long one reached the judge without its offer — the same
    refusal the evidence reordering fixed."""
    provider = ContextProvider("base", session_id="s-t")
    provider.append_user("capture the supplier research")
    reply = "None of your documents cover this. " * 12 + "Do you want a new document for this?"
    assert len(reply) > 300
    provider.append_assistant(reply)
    provider.append_user("yeah")
    text = provider.last_assistant_text()
    assert len(text) == 300
    assert text.endswith("Do you want a new document for this?")


def test_entry_sentences_released_before_append_still_vouch_and_truncate():
    """Round-4 blocker: playback of a reply's OWN first sentences starts
    before the stream ends, so an append-time mark put them below the
    mark and the mismatch valve ate the amendment on exactly the
    multi-sentence replies FR-61 exists for. The mark is stamped at STEP
    start instead; this test was proven discriminating against the
    append-time mark (it fails there: mismatch no-op, no suffix)."""
    scripts = [
        [
            LLMTextDelta("First point here. "),
            0.1,
            LLMTextDelta("Second part arrives. And the third never plays."),
            _done(),
        ]
    ]
    h = make_loop(scripts)

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("go"))
        await asyncio.sleep(0.05)
        # the entry's own first sentence releases MID-STREAM, before any
        # append could stamp a mark
        h.loop.note_uttered_sentence("First point here.")
        await turn
        # the second lands during the playback tail; the third never does
        h.loop.note_uttered_sentence("Second part arrives.")

    asyncio.run(run())
    asyncio.run(h.loop._start_interruption())

    last = h.ctx.build()[-1]
    assert last["content"] == (
        "First point here. Second part arrives." + INTERRUPTED_SUFFIX
    )
