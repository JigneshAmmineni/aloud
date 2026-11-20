"""§4.15 turn checks, at every level: the outcome rules (FR-63), the
evidence (FR-68), the provider seam (FR-69), the loop's use of them
(FR-62, FR-64 to FR-67, FR-70), the prompt (FR-72, FR-73), the pause
between spoken steps (FR-43 as amended) and the first-sentence cut
(FR-61 as amended)."""

import asyncio
import json
import time

import httpx
import pytest
from loguru import logger
from pipecat.frames.frames import LLMTextFrame

import agent.loop as loop_mod
from agent.checks import (
    AFFIRM,
    ANSWER,
    DONE,
    NOTHING,
    SILENT_MID_THOUGHT,
    SILENT_NOTHING_ASKED,
    SPEAK,
    WRITE,
    CheckSettings,
    build_evidence,
    decide,
    questions_for,
)
from agent.context import INTERRUPTED_SUFFIX, ContextProvider
from agent.prompts import (
    AFFIRM_INSTRUCTION,
    ANSWER_INSTRUCTION,
    EDIT_AND_SPEAK_INSTRUCTION,
    FALLBACK_LINES,
    WRAP_UP_INSTRUCTION,
    build_system_prompt,
)
from agent.providers import (
    CHECK_ANSWERED,
    CHECK_ERROR,
    CheckAnswer,
    JevTurnChecker,
    LLMTextDelta,
    LLMToolCall,
    make_turn_checker,
)
from db.models import LLMTrace, TurnMetric, UsageEvent
from tests.test_agent_loop import (
    _done,
    _narrated_tool_turn,
    _pushed_text,
    _queued,
    make_loop,
    read_tool,
    write_tool,
)

ON = CheckSettings(done_on=True, mode_on=True, model="jev-test")


def answers(done=0.99, write=0.0, nothing=0.0, affirm=0.0, answer=1.0):
    return {
        DONE: CheckAnswer(CHECK_ANSWERED, p_yes=done),
        WRITE: CheckAnswer(CHECK_ANSWERED, p_yes=write),
        SPEAK: CheckAnswer(
            CHECK_ANSWERED,
            probabilities={NOTHING: nothing, AFFIRM: affirm, ANSWER: answer},
        ),
    }


class FakeChecker:
    def __init__(self, result=None, *, delay=0.0, raises=None):
        self.result = result if result is not None else answers()
        self.delay = delay
        self.raises = raises
        self.calls: list = []

    async def ask(self, evidence, questions):
        self.calls.append((evidence, [q.id for q in questions]))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return {q.id: self.result[q.id] for q in questions if q.id in self.result}, None


def checked_loop(scripts, result=None, *, tools=(), cfg=ON, **checker_kwargs):
    checker = FakeChecker(result, **checker_kwargs)
    h = make_loop(scripts, tools=tools, checker=checker, checks=cfg)
    h.checker = checker
    return h


# ---------------- FR-63: what the answers decide ----------------


def test_mid_thought_wins_over_everything_else():
    plan = decide(answers(done=0.2, write=0.9, answer=1.0), ON)
    assert plan.silent == SILENT_MID_THOUGHT and plan.detail == "silent_mid_thought"


@pytest.mark.parametrize("p_done, silent", [(0.4, True), (0.41, False)])
def test_the_mid_thought_threshold_on_both_sides(p_done, silent):
    assert (decide(answers(done=p_done), ON).silent is not None) is silent


@pytest.mark.parametrize("p_write, silent", [(0.2, True), (0.21, False)])
def test_the_no_edit_threshold_gates_only_the_nothing_asked_silence(p_write, silent):
    """WRITE never touches the tools. Being sure no edit was asked is what
    lets a turn that expects nothing be silent; short of that it closes
    with a few words, because an edit may have been asked for."""
    plan = decide(answers(write=p_write, nothing=0.9, answer=0.1), ON)
    assert (plan.silent == SILENT_NOTHING_ASKED) is silent
    assert plan.speech == (None if silent else AFFIRM)

def test_nothing_asked_is_silence_only_when_no_edit_can_have_been_asked():
    quiet = answers(write=0.05, nothing=0.9, answer=0.1)
    assert decide(quiet, ON).silent == SILENT_NOTHING_ASKED
    # the same choice on a turn that keeps its write tools becomes affirm:
    # an edit may have been asked for, and work is always confirmed
    kept = decide(answers(write=0.5, nothing=0.9, answer=0.1), ON)
    assert kept.silent is None and kept.speech == AFFIRM


@pytest.mark.parametrize("p, speech", [(0.5, AFFIRM), (0.49, ANSWER)])
def test_the_affirm_threshold_on_both_sides(p, speech):
    plan = decide(answers(write=0.05, affirm=p, answer=1 - p), ON)
    assert plan.speech == speech


def test_a_plain_edit_request_gets_the_affirm_instruction_with_its_tools():
    plan = decide(answers(write=0.9, affirm=0.95, answer=0.05), ON)
    assert plan.detail == "affirm" and plan.instruction == AFFIRM_INSTRUCTION


@pytest.mark.parametrize("p_write, speech", [(0.6, "edit_and_speak"), (0.59, ANSWER)])
def test_edit_and_speak_needs_an_answer_and_enough_confidence_in_the_edit(
    p_write, speech
):
    plan = decide(answers(write=p_write, answer=0.97, affirm=0.02), ON)
    assert plan.speech == speech


def test_an_expected_answer_carries_mode_two_s_bound_whatever_the_edit_answer():
    """First live run: the prompt alone did not hold answers to one or two
    sentences, so an answer turn carries the bound itself. The edit answer
    changes nothing about that instruction: sure or unsure that no edit
    was asked, it is the same line, and it never mentions editing."""
    for p_write in (0.05, 0.4):
        plan = decide(answers(write=p_write, answer=0.9), ON)
        assert plan.detail == "answer" and plan.instruction == ANSWER_INSTRUCTION
    assert "edit" not in ANSWER_INSTRUCTION.lower()
    assert "document" not in ANSWER_INSTRUCTION.lower()

def test_no_verdict_is_today_s_behaviour():
    assert decide({}, CheckSettings()).detail == "plain"
    unanswered = decide({}, ON)
    assert unanswered.detail == "plain+bypass" and unanswered.instruction is None


def test_the_answer_instruction_leaves_explaining_and_reading_out_reachable():
    text = ANSWER_INSTRUCTION.lower()
    assert "answer in one sentence" in text and "add a second only if" in text
    assert "explain in detail" in text and "read something out" in text
    assert "word for word" in text


def test_each_switch_off_skips_its_decisions():
    done_only = CheckSettings(done_on=True)
    assert [q.id for q in questions_for(done_only)] == [DONE]
    assert decide(answers(done=0.1), done_only).silent == SILENT_MID_THOUGHT
    assert decide(answers(write=0.0, nothing=1.0), done_only).detail == "plain"
    mode_only = CheckSettings(mode_on=True)
    assert [q.id for q in questions_for(mode_only)] == [WRITE, SPEAK]
    assert decide(answers(done=0.0), mode_only).silent is None


def test_a_confirming_yeah_gets_the_affirm_instruction_and_nothing_about_edits():
    plan = decide(answers(write=0.0, affirm=0.9, answer=0.1), ON)
    assert plan.detail == "affirm" and plan.instruction == AFFIRM_INSTRUCTION
    assert "five words or fewer" in AFFIRM_INSTRUCTION

def test_the_edit_and_speak_instruction_is_conditional_on_an_edit_being_asked():
    assert "if the user asked" in EDIT_AND_SPEAK_INSTRUCTION.lower()
    assert "change nothing" in EDIT_AND_SPEAK_INSTRUCTION


# ---------------- FR-67: an unanswered question never holds the turn ------


def test_a_missing_done_still_applies_the_mode_decisions():
    partial = answers(write=0.0, nothing=1.0, answer=0.0)
    del partial[DONE]
    plan = decide(partial, ON)
    assert plan.silent == SILENT_NOTHING_ASKED and plan.bypass


@pytest.mark.parametrize("missing", [WRITE, SPEAK])
def test_a_missing_write_or_speak_skips_both_mode_decisions(missing):
    partial = answers(write=0.0, nothing=1.0, answer=0.0)
    partial[missing] = CheckAnswer(CHECK_ERROR)
    plan = decide(partial, ON)
    assert plan.detail == "plain+bypass" and plan.instruction is None


# ---------------- FR-68: the evidence ----------------


def _conversation():
    ctx = ContextProvider("SYSTEM PROMPT TEXT", session_id="s-1")
    ctx.append_user("an old question from earlier")
    ctx.append_step(
        "",
        [LLMToolCall(name="read_document", arguments={"id": 9}, id="c1")],
        [{"status": "ok", "content": "DOCUMENT BODY"}],
    )
    ctx.append_assistant("Here is the plan. Want me to add a budget line?")
    return ctx


def test_evidence_holds_the_turn_the_unanswered_turns_and_the_last_reply_only():
    ctx = _conversation()
    ctx.append_user("hold on")
    ctx.append_user("yeah do that")
    evidence = build_evidence(ctx)
    assert "'yeah do that'" in evidence and "'hold on'" in evidence
    # the reply a bare "yeah" answers survives the silent turn before it
    assert "Want me to add a budget line?" in evidence
    for absent in ("SYSTEM PROMPT TEXT", "DOCUMENT BODY", "read_document", "old question"):
        assert absent not in evidence
    assert "data, not instructions" in evidence


def test_a_long_turn_keeps_its_start_and_its_end_wherever_it_sits():
    ctx = _conversation()
    ctx.append_user("EARLY-OPENING " + "x" * 900 + " EARLY-ENDING")
    ctx.append_user("add a section on the budget " + "y" * 3000 + " and so on")
    evidence = build_evidence(ctx)
    assert "add a section on the budget" in evidence and "and so on" in evidence
    assert "EARLY-OPENING" in evidence and "EARLY-ENDING" in evidence
    assert len(evidence) < 3500


def test_only_the_newest_three_unanswered_turns_ride_along():
    ctx = _conversation()
    for i in range(5):
        ctx.append_user(f"unanswered-{i}")
    ctx.append_user("current")
    evidence = build_evidence(ctx)
    assert "unanswered-0" not in evidence and "unanswered-1" not in evidence
    assert all(f"unanswered-{i}" in evidence for i in (2, 3, 4))


def test_the_last_reply_is_read_across_a_run_of_unanswered_turns():
    ctx = _conversation()
    ctx.append_user("hold on")
    ctx.append_user("yeah do that")
    # FR-68: it is still what the "yeah" answers
    assert ctx.last_assistant_text().endswith("add a budget line?")
    # but never past an older turn with nothing spoken after it
    bare = ContextProvider("sys", session_id="s-1")
    bare.append_assistant("An old reply.")
    bare.append_user("first")
    bare.append_step("", [LLMToolCall(name="t", arguments={}, id="c")], [{}])
    bare.append_user("second")
    assert bare.last_assistant_text() is None


# ---------------- FR-69: the provider seam ----------------


def _jev(handler):
    return JevTurnChecker(
        "key-123",
        "jev-test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_one_request_carries_every_question_and_answers_come_back_typed():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "done": {"type": "noul", "noul": 0.93},
                    "write": {"type": "noul", "noul": 0.1},
                    "speak": {
                        "type": "choice",
                        "choice": "answer",
                        "probabilities": {"nothing": 0.1, "affirm": 0.2, "answer": 0.7},
                    },
                },
                "usage": {"input_tokens": 440, "output_tokens": 50},
            },
        )

    result, usage = asyncio.run(_jev(handler).ask("EVIDENCE", questions_for(ON)))
    assert seen["auth"] == "Bearer key-123"
    assert seen["body"]["state"] == "EVIDENCE" and seen["body"]["model"] == "jev-test"
    assert seen["body"]["questions"]["done"]["type"] == "noul"
    assert seen["body"]["questions"]["speak"]["type"] == "choice"
    assert set(seen["body"]["questions"]["speak"]["criteria"]) == {NOTHING, AFFIRM, ANSWER}
    assert result[DONE].p_yes == 0.93 and result[WRITE].p_yes == 0.1
    assert result[SPEAK].probabilities[ANSWER] == 0.7
    assert usage.prompt_tokens == 440


def test_an_answer_that_is_missing_or_unreadable_is_an_error_for_that_question_only():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "write": {"noul": "high"},
                    "speak": {"probabilities": {"nothing": 0.5, "affirm": 0.5, "answer": 0.0}},
                }
            },
        )

    result, usage = asyncio.run(_jev(handler).ask("E", questions_for(ON)))
    assert result[DONE].status == CHECK_ERROR and result[WRITE].status == CHECK_ERROR
    assert result[SPEAK].status == CHECK_ANSWERED and usage is None


def test_a_failed_request_raises_for_the_loop_to_bypass():
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_jev(lambda r: httpx.Response(500)).ask("E", questions_for(ON)))


def test_the_factory_is_gated_on_the_key_not_the_switches(make_settings):
    assert make_turn_checker(make_settings()) is None
    built = make_turn_checker(make_settings(typesafe_api_key="k"))
    assert isinstance(built, JevTurnChecker)  # both switches are off here


# ---------------- config (FR-69, FR-71) ----------------


def test_the_checks_are_on_by_default_and_need_their_key(monkeypatch):
    from app.config import load_settings

    monkeypatch.delenv("TURN_CHECK_DONE", raising=False)
    monkeypatch.delenv("TURN_CHECK_MODE", raising=False)
    settings = load_settings()
    assert settings.turn_check_done and settings.turn_check_mode
    assert settings.check_mid_thought_min == 0.6  # silent only at p_done <= 0.4
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        load_settings()
    monkeypatch.setenv("TURN_CHECK_DONE", "0")
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        load_settings()  # one switch on is enough to need it
    monkeypatch.setenv("TURN_CHECK_MODE", "0")
    assert load_settings().typesafe_api_key == ""  # both off: loads without


# ---------------- the prompt (FR-72, FR-73) ----------------


def test_with_both_checks_on_the_prompt_names_no_listening_sound():
    prompt = build_system_prompt(checks_on=True)
    assert "Hmm" not in prompt and "Mm-hmm" not in prompt
    # the case the removed rule covered moves to mode 1, by name
    assert "thinking aloud" in prompt and "never commentary" in prompt


def test_the_prompt_says_to_answer_the_newest_message_when_one_went_unanswered():
    """First live run: a "Hold on." met with silence was answered a turn
    later, as "Okay, holding", in reply to a different message."""
    assert "reply to the newest" in build_system_prompt(checks_on=True)


def test_no_instruction_restricts_editing_or_talks_about_it():
    """Owner decision after the first live run: the checks steer, they
    never restrict. A wrong no-edit verdict used to take the edit tools
    away and make the model say "Document editing is off for this reply";
    there is no such instruction, and no refusal for it, any more."""
    import agent.prompts as prompts_mod

    assert not hasattr(prompts_mod, "TALK_ONLY_INSTRUCTION")
    assert not hasattr(loop_mod, "WITHHELD_REFUSAL")
    for text in (AFFIRM_INSTRUCTION, ANSWER_INSTRUCTION):
        lowered = text.lower()
        assert "cannot" not in lowered and "do not create or edit" not in lowered

def test_agreeing_to_an_offered_change_counts_as_asking_for_it():
    """First live run: "Yes." to 'Did you mean put the first aid kit under
    gear?' scored 0.06 on the narrower wording and lost its edit tools."""
    write = next(q for q in questions_for(ON) if q.id == WRITE)
    assert "agrees to a change the assistant has just offered" in write.instructions


def test_with_a_check_off_the_prompt_keeps_today_s_rule_and_only_that_rule():
    prompt = build_system_prompt(checks_on=False)
    assert 'your entire reply is "Hmm." or "Mm-hmm."' in prompt
    assert 'Never reply "Hmm." twice in a row' in prompt
    assert "thinking aloud" not in prompt  # one rule per case, never two


@pytest.mark.parametrize("checks_on", [True, False])
def test_the_three_answer_modes_are_in_every_assembly(checks_on):
    prompt = build_system_prompt(checks_on=checks_on)
    assert "five words or fewer" in prompt
    assert "One sentence. Add a second only when" in prompt
    # the two prompts must not disagree: the turn's instruction forbids an
    # unasked offer, so the system prompt may not invite one
    assert "offering to go deeper" not in prompt
    assert "only when the user explicitly asks" in prompt
    assert "word for word" in prompt
    assert "and completely" not in prompt and "Keep replies brief" not in prompt
    assert "never read a document's content aloud" not in prompt


# ---------------- the loop: FR-62, FR-64 ----------------


def test_a_silent_outcome_calls_no_model_speaks_nothing_and_tells_the_client():
    h = checked_loop([[LLMTextDelta("must not run"), _done()]], answers(done=0.05))
    asyncio.run(h.loop._run_turn("I think we should probably"))
    assert h.llm.calls == [] and _pushed_text(h) == ""
    assert h.emitted == [{"type": "turn.silent", "reason": "silent_mid_thought"}]
    built = h.ctx.build()
    assert built[-1] == {"role": "user", "content": "I think we should probably"}
    assert not any(m["role"] == "assistant" for m in built)
    # asked once, about this turn, before anything else
    assert len(h.checker.calls) == 1
    assert "I think we should probably" in h.checker.calls[0][0]


def test_after_a_silent_turn_the_next_request_carries_both_user_turns():
    h = checked_loop([[LLMTextDelta("The cheaper one, then."), _done()]])
    h.checker.result = answers(done=0.05)
    asyncio.run(h.loop._run_turn("I think we should probably"))
    h.checker.result = answers()
    asyncio.run(h.loop._run_turn("go with the cheaper campsite"))
    users = [m["content"] for m in h.llm.calls[0]["messages"] if m["role"] == "user"]
    assert users == ["I think we should probably", "go with the cheaper campsite"]
    assert _pushed_text(h) == "The cheaper one, then."
    # and the second check saw the unanswered turn as evidence
    assert "I think we should probably" in h.checker.calls[1][0]


def test_a_silent_turn_writes_its_usage_event_and_trace_and_no_metrics_row():
    h = checked_loop([], answers(write=0.0, nothing=0.95, answer=0.05))

    async def run():
        await h.loop._run_turn("okay")

    asyncio.run(run())
    events = [e for e in _queued(h.recorder, UsageEvent) if e.stage == "check"]
    assert [(e.unit, e.quantity, e.detail, e.turn_id) for e in events] == [
        ("count", 3.0, "silent_nothing_asked", 2)
    ]
    assert _queued(h.recorder, TurnMetric) == []
    (trace,) = _queued(h.traces, LLMTrace)
    assert (trace.purpose, trace.step, trace.model, trace.finish_reason) == (
        "check", 0, "jev-test", "stop",
    )
    output = json.loads(trace.output)
    assert output["outcome"] == "silent_nothing_asked"
    assert output["questions"]["speak"]["status"] == CHECK_ANSWERED
    assert "okay" in trace.input_messages  # the evidence: the trace is the one place with text
    assert "okay" not in events[0].detail


def test_the_greeting_and_a_loop_with_the_switches_off_are_never_checked():
    h = checked_loop([[LLMTextDelta("Hey."), _done()], [LLMTextDelta("Sure."), _done()]])
    asyncio.run(h.loop._run_turn(None))
    assert h.checker.calls == []
    off = checked_loop([[LLMTextDelta("Sure."), _done()]], cfg=CheckSettings())
    asyncio.run(off.loop._run_turn("do it"))
    assert off.checker.calls == [] and _pushed_text(off) == "Sure."
    assert not [e for e in _queued(off.recorder, UsageEvent) if e.stage == "check"]


# ---------------- the loop: FR-65, FR-66 ----------------


def test_a_no_edit_verdict_never_touches_the_tools_and_only_bounds_the_answer():
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("It mentions pricing twice."), _done()],
    ]
    h = checked_loop(scripts, answers(write=0.02), tools=[read_tool(), write_tool()])
    asyncio.run(h.loop._run_turn("which document mentions pricing?"))
    assert len(h.llm.calls) == 2
    for call in h.llm.calls:
        assert [t["name"] for t in call["tools"]] == ["echo", "save"]  # all of them
        assert call["messages"][-1] == {"role": "system", "content": ANSWER_INSTRUCTION}
    assert not any(ANSWER_INSTRUCTION in str(m) for m in h.ctx.build())

def test_an_edit_the_checks_did_not_expect_still_runs():
    """The verdict steers; it never restricts. When the checks were sure no
    edit was asked and the model edits anyway — because the user did ask —
    the edit happens."""
    ran = []

    async def handler(args, ctx):
        ran.append(args)
        return {"status": "ok"}

    save = write_tool()
    save = type(save)(**{**save.__dict__, "handler": handler})
    scripts = [
        [LLMToolCall(name="save", arguments={"text": "first aid kit"}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = checked_loop(scripts, answers(write=0.02, affirm=0.9, answer=0.1), tools=[save])
    asyncio.run(h.loop._run_turn("yes"))
    assert ran == [{"text": "first aid kit"}]
    result = next(m for m in h.ctx.build() if m["role"] == "tool")["content"]
    assert result == {"status": "ok"}

def test_the_forced_wrap_up_still_carries_the_turn_s_instruction_and_every_tool():
    scripts = [[LLMToolCall(name="echo", arguments={}, id=f"c{i}"), _done()] for i in range(5)]
    scripts.append([LLMTextDelta("Here is where things stand."), _done()])
    h = checked_loop(scripts, answers(write=0.02), tools=[read_tool(), write_tool()])
    asyncio.run(h.loop._run_turn("look into it"))
    last = h.llm.calls[-1]
    assert last["tool_choice"] == "none"
    assert [t["name"] for t in last["tools"]] == ["echo", "save"]
    contents = [m["content"] for m in last["messages"] if m["role"] == "system"]
    assert ANSWER_INSTRUCTION in contents and WRAP_UP_INSTRUCTION in contents

@pytest.mark.parametrize(
    "result, instruction",
    [
        (answers(write=0.9, affirm=0.95, answer=0.05), AFFIRM_INSTRUCTION),
        (answers(write=0.7, answer=0.97), EDIT_AND_SPEAK_INSTRUCTION),
        (answers(write=0.4, answer=0.97), ANSWER_INSTRUCTION),
    ],
)
def test_each_instruction_rides_its_own_outcome_and_never_reaches_the_context(
    result, instruction
):
    h = checked_loop([[LLMTextDelta("Done."), _done()]], result, tools=[write_tool()])
    asyncio.run(h.loop._run_turn("add that"))
    sent = [m["content"] for m in h.llm.calls[0]["messages"] if m["role"] == "system"]
    every = (
        AFFIRM_INSTRUCTION,
        ANSWER_INSTRUCTION,
        EDIT_AND_SPEAK_INSTRUCTION,
    )
    assert [i for i in every if i in sent] == [instruction]
    assert [t["name"] for t in h.llm.calls[0]["tools"]] == ["save"]  # tools kept
    assert not any(i in str(h.ctx.build()) for i in every)


# ---------------- the loop: FR-67, FR-70 ----------------


def _events(name):
    seen: list = []
    sink = logger.add(
        lambda m: seen.append(m.record) if m.record["extra"].get("event") == name else None
    )
    return seen, sink


def test_a_checker_that_raises_runs_the_turn_plain_and_warns_without_text():
    seen, sink = _events("turn.check_bypassed")
    try:
        h = checked_loop(
            [[LLMTextDelta("Sure."), _done()]],
            tools=[write_tool()],
            raises=RuntimeError("secret user words"),
        )
        asyncio.run(h.loop._run_turn("add it"))
    finally:
        logger.remove(sink)
    assert _pushed_text(h) == "Sure."
    assert [t["name"] for t in h.llm.calls[0]["tools"]] == ["save"]
    assert len(h.llm.calls[0]["messages"]) == 2  # system + user: no instruction
    assert len(seen) == 1 and seen[0]["level"].name == "WARNING"
    assert seen[0]["extra"]["reason"] == "RuntimeError"
    assert "secret user words" not in seen[0]["message"]
    (event,) = [e for e in _queued(h.recorder, UsageEvent) if e.stage == "check"]
    assert event.detail == "plain+bypass"
    output = json.loads(_queued(h.traces, LLMTrace)[0].output)
    assert {q["status"] for q in output["questions"].values()} == {"error"}


def test_a_checker_that_never_answers_lets_the_turn_start_at_the_ceiling():
    cfg = CheckSettings(done_on=True, mode_on=True, model="jev-test", timeout_s=0.05)
    h = checked_loop([[LLMTextDelta("Sure."), _done()]], cfg=cfg, delay=5.0)
    start = time.monotonic()
    asyncio.run(h.loop._run_turn("add it"))
    assert time.monotonic() - start < 1.0 and _pushed_text(h) == "Sure."
    output = json.loads(_queued(h.traces, LLMTrace)[0].output)
    assert {q["status"] for q in output["questions"].values()} == {"timeout"}
    assert output["outcome"] == "plain+bypass"


def test_a_response_missing_one_answer_bypasses_only_what_needs_it():
    partial = answers(write=0.0, nothing=1.0, answer=0.0)
    del partial[DONE]
    h = checked_loop([], partial)
    asyncio.run(h.loop._run_turn("okay"))
    assert h.llm.calls == []  # decisions 2 and 3 still applied: silence
    output = json.loads(_queued(h.traces, LLMTrace)[0].output)
    assert output["questions"]["done"]["status"] == CHECK_ERROR
    assert output["questions"]["speak"]["status"] == CHECK_ANSWERED
    assert output["outcome"] == "silent_nothing_asked+bypass"


def test_a_barge_in_while_the_answers_are_pending_records_an_interrupted_check():
    h = checked_loop([[LLMTextDelta("never"), _done()]], delay=1.0)

    async def run():
        h.loop._turn_task = asyncio.create_task(h.loop._run_turn("so I was thinking"))
        await asyncio.sleep(0.05)
        await h.loop._start_interruption()

    async def no_super(*a, **k):
        return None

    import pipecat.processors.frame_processor as fp

    original = fp.FrameProcessor._start_interruption
    fp.FrameProcessor._start_interruption = no_super
    try:
        asyncio.run(run())
    finally:
        fp.FrameProcessor._start_interruption = original
    assert h.llm.calls == [] and _pushed_text(h) == ""
    (event,) = [e for e in _queued(h.recorder, UsageEvent) if e.stage == "check"]
    assert event.detail == "interrupted"
    (trace,) = _queued(h.traces, LLMTrace)
    assert trace.purpose == "check" and trace.finish_reason == "interrupted"
    # the turn's text stays in the context as an unanswered user turn
    assert h.ctx.build()[-1] == {"role": "user", "content": "so I was thinking"}


def test_the_check_s_time_joins_the_metrics_row_of_a_turn_that_spoke():
    h = checked_loop([[LLMTextDelta("Sure."), _done()]])

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("what do you think?"))
        await asyncio.sleep(0.02)
        h.recorder.record_measurement(700)
        await turn

    asyncio.run(run())
    (row,) = _queued(h.recorder, TurnMetric)
    assert "check" in row.stages_ms and "step.1.ttfb.llm" in row.stages_ms


def test_the_loop_cannot_be_built_without_its_checker_arguments():
    with pytest.raises(TypeError, match="checker"):
        loop_mod.AgentLoopProcessor(
            context=ContextProvider("base", session_id="s-1"),
            llm=None,
            registry=[],
            session_id="s-1",
            user_id="uid-a",
            model_name="m",
            recorder=None,
            traces=None,
            emit=None,
            write_registry=set(),
            greeting_trigger="g",
        )


# ---------------- FR-43: the pause between two spoken steps ----------------


def _timed(h):
    """Record when each text frame is pushed."""
    times: list = []
    real = h.loop.push_frame

    async def push(frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            times.append((frame.text, time.monotonic()))
        await real(frame, direction)

    h.loop.push_frame = push
    return times


def _run_with_playback(h, user_text, playback):
    """`playback`: (delay, 'start' | 'stop') pairs applied in order."""
    marks: dict = {}

    async def run():
        turn = asyncio.create_task(h.loop._run_turn(user_text))
        for delay, what in playback:
            await asyncio.sleep(delay)
            if what == "start":
                h.loop.note_bot_spoke()
            else:
                h.loop.note_bot_stopped()
                marks["stopped"] = time.monotonic()
        await turn

    asyncio.run(run())
    return marks


def _pause_setup(monkeypatch, gap=0.2, slack=5.0, cps=100000.0, start=5.0, cap=60.0):
    monkeypatch.setattr(loop_mod, "STEP_SPEECH_GAP_S", gap)
    monkeypatch.setattr(loop_mod, "STEP_SPEECH_HOLD_SLACK_S", slack)
    monkeypatch.setattr(loop_mod, "STEP_SPEECH_SLOWEST_CPS", cps)
    monkeypatch.setattr(loop_mod, "STEP_SPEECH_START_MAX_S", start)
    monkeypatch.setattr(loop_mod, "STEP_SPEECH_HOLD_MAX_S", cap)


def test_the_closing_word_waits_for_the_gap_after_the_narration_played(monkeypatch):
    _pause_setup(monkeypatch)
    h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
    times = _timed(h)
    marks = _run_with_playback(h, "add it", [(0.02, "start"), (0.08, "stop")])
    # nothing is dropped: the narration and the confirmation are both spoken
    assert [t for t, _ in times] == ["I'll add that to the document. ", "Done."]
    assert times[1][1] - marks["stopped"] >= 0.19
    assert "Done." in [m["content"] for m in h.ctx.build() if m["role"] == "assistant"]


def test_a_stop_between_two_sentences_does_not_start_the_gap_early(monkeypatch):
    _pause_setup(monkeypatch)
    h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
    times = _timed(h)
    marks = _run_with_playback(
        h, "add it", [(0.02, "start"), (0.05, "stop"), (0.08, "start"), (0.1, "stop")]
    )
    assert times[1][1] - marks["stopped"] >= 0.19  # measured from the LAST stop


def test_a_step_that_speaks_first_or_after_silent_steps_is_not_delayed(monkeypatch):
    _pause_setup(monkeypatch, gap=5.0)
    scripts = [
        [LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    start = time.monotonic()
    asyncio.run(h.loop._run_turn("add it"))
    assert time.monotonic() - start < 1.0 and _pushed_text(h) == "Done."


def test_a_long_playback_keeps_its_pause_and_logs_nothing(monkeypatch):
    # ceiling = 31 chars / 31 cps + 0.1s slack = 1.1s; the narration plays
    # for 0.4s, four times the slack, and still gets its gap
    _pause_setup(monkeypatch, gap=0.1, slack=0.1, cps=31.0)
    seen, sink = _events("speech_gap.signal_missing")
    try:
        h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
        times = _timed(h)
        marks = _run_with_playback(h, "add it", [(0.02, "start"), (0.4, "stop")])
    finally:
        logger.remove(sink)
    assert times[1][1] - marks["stopped"] >= 0.09 and seen == []


def test_a_signal_that_never_comes_releases_the_words_at_the_ceiling_with_a_warning(
    monkeypatch,
):
    _pause_setup(monkeypatch, gap=0.1, slack=0.15)
    seen, sink = _events("speech_gap.signal_missing")
    try:
        h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
        start = time.monotonic()
        asyncio.run(h.loop._run_turn("add it"))
        elapsed = time.monotonic() - start
    finally:
        logger.remove(sink)
    assert _pushed_text(h) == "I'll add that to the document. Done."
    assert 0.14 <= elapsed < 1.0
    assert len(seen) == 1 and seen[0]["level"].name == "WARNING"


# ---------------- FR-61: the cut inside the first sentence ----------------


def _ended_turn(reply):
    h = make_loop([[LLMTextDelta(reply), _done()]])
    asyncio.run(h.loop._run_turn("what should we cook?"))
    return h


def _interrupt_with_no_live_turn(h):
    import pipecat.processors.frame_processor as fp

    async def no_super(*a, **k):
        return None

    original = fp.FrameProcessor._start_interruption
    fp.FrameProcessor._start_interruption = no_super
    try:
        asyncio.run(h.loop._start_interruption())
    finally:
        fp.FrameProcessor._start_interruption = original


REPLY = "Foil packets are easiest. Chop at home. Bring a 3.5 quart pot. Done by dark."


def test_a_reply_cut_before_any_of_it_played_keeps_only_its_first_sentence():
    h = _ended_turn(REPLY)
    h.loop._release_seen = True  # an earlier reply's words were released
    h.loop.note_bot_spoke()  # audio began, and the user spoke over it at once
    _interrupt_with_no_live_turn(h)
    assert h.ctx.build()[-1]["content"] == "Foil packets are easiest." + INTERRUPTED_SUFFIX


def test_the_same_cut_with_earlier_audio_in_the_turn_is_treated_alike():
    h = _ended_turn(REPLY)
    h.loop._release_seen = True
    h.loop.note_bot_spoke()
    # an earlier step's words were released; none of this entry's own
    h.loop._uttered = ["I'll", "check."]
    h.loop._uttered_mark = 2
    _interrupt_with_no_live_turn(h)
    assert h.ctx.build()[-1]["content"] == "Foil packets are easiest." + INTERRUPTED_SUFFIX


def test_a_cut_a_few_words_in_is_the_same_first_sentence_cut():
    h = _ended_turn(REPLY)
    h.loop.note_bot_spoke()
    for word in ("Foil", "packets"):
        h.loop.note_uttered_sentence(word)
    _interrupt_with_no_live_turn(h)
    assert h.ctx.build()[-1]["content"] == "Foil packets are easiest." + INTERRUPTED_SUFFIX


def test_a_session_s_first_reply_cut_that_early_changes_nothing_and_warns_once():
    seen, sink = _events("fr61.signal_suspect")
    try:
        h = _ended_turn(REPLY)
        h.loop.note_bot_spoke()  # no release has ever been seen this session
        _interrupt_with_no_live_turn(h)
        _interrupt_with_no_live_turn(h)
    finally:
        logger.remove(sink)
    assert h.ctx.build()[-1]["content"] == REPLY
    assert len(seen) == 1


def test_an_interruption_before_any_audio_keeps_the_entry_intact_with_the_mark():
    h = _ended_turn(REPLY)
    h.loop._release_seen = True
    _interrupt_with_no_live_turn(h)  # no audio played this turn at all
    assert h.ctx.build()[-1]["content"] == REPLY + INTERRUPTED_SUFFIX


def test_a_fully_heard_reply_is_left_alone():
    h = _ended_turn(REPLY)
    h.loop.note_bot_spoke()
    for word in REPLY.split():
        h.loop.note_uttered_sentence(word)
    _interrupt_with_no_live_turn(h)
    assert h.ctx.build()[-1]["content"] == REPLY


def test_the_first_sentence_rule_survives_decimals_and_single_sentences():
    ctx = ContextProvider("sys", session_id="s-1")
    ctx.append_assistant("Use the 3.5 quart pot from notes.md. Then stir.")
    assert ctx.cut_to_first_sentence()
    assert ctx.build()[-1]["content"] == (
        "Use the 3.5 quart pot from notes.md." + INTERRUPTED_SUFFIX
    )
    assert not ctx.cut_to_first_sentence()  # already marked
    one = ContextProvider("sys", session_id="s-1")
    one.append_assistant("Okay")
    assert one.cut_to_first_sentence()
    assert one.build()[-1]["content"] == "Okay" + INTERRUPTED_SUFFIX


# ---------------- PR #28 review, round 1 ----------------


def test_a_reply_after_narration_and_a_tool_call_is_still_cut_to_its_first_sentence(
    monkeypatch,
):
    """The transport releases played text word by word, so the narration's
    words land AFTER the closing step has started — during its LLM call
    and the pause. They must not be counted as the closing reply's own:
    with the entry scope stamped at step start they were, and a reply the
    user never heard stayed in the context whole."""
    _pause_setup(monkeypatch, gap=0.05)
    h = make_loop(
        _narrated_tool_turn([LLMTextDelta(REPLY)]), tools=[read_tool(delay=0.05)]
    )

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("what should we cook?"))
        await asyncio.sleep(0.15)  # the closing step is waiting out the pause
        h.loop.note_bot_spoke()
        for word in "I'll add that to the document.".split():
            h.loop.note_uttered_sentence(word)
        h.loop.note_bot_stopped()
        await turn

    asyncio.run(run())
    h.loop.note_bot_spoke()  # the reply's audio began; the user spoke over it at once
    _interrupt_with_no_live_turn(h)
    assert h.ctx.build()[-1]["content"] == "Foil packets are easiest." + INTERRUPTED_SUFFIX


def test_the_fallback_line_waits_for_the_gap_too(monkeypatch):
    """The one line the loop speaks itself must not run into the narration
    of the step before the one that failed."""
    _pause_setup(monkeypatch)
    scripts = [
        [
            LLMTextDelta("I'll add that to the document. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ],
        [_done()],  # the closing step comes back empty
    ]
    h = make_loop(scripts, tools=[read_tool()])
    times = _timed(h)
    marks = _run_with_playback(h, "add it", [(0.02, "start"), (0.08, "stop")])
    assert times[1][0] in FALLBACK_LINES
    assert times[1][1] - marks["stopped"] >= 0.19


def test_earlier_text_that_never_becomes_audio_is_not_waited_for(monkeypatch):
    """Filters can reduce a step's text to nothing, and synthesis can
    fail: with no playback to wait for, the wait ends after its short
    start window — as its own event, not the missing-signal warning."""
    _pause_setup(monkeypatch, gap=5.0, slack=5.0, start=0.1)
    warned, warn_sink = _events("speech_gap.signal_missing")
    noted, note_sink = _events("speech_gap.no_playback")
    try:
        h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
        start = time.monotonic()
        asyncio.run(h.loop._run_turn("add it"))
        elapsed = time.monotonic() - start
    finally:
        logger.remove(warn_sink)
        logger.remove(note_sink)
    assert _pushed_text(h) == "I'll add that to the document. Done."
    assert 0.09 <= elapsed < 1.0
    assert warned == [] and len(noted) == 1


def test_the_ceiling_is_capped_however_long_the_earlier_speech_was(monkeypatch):
    # 31 chars at 1 char/s plus 50s of slack would wait 81s for a stop
    # that never comes; the cap ends it
    _pause_setup(monkeypatch, gap=0.05, slack=50.0, cps=1.0, cap=0.2)
    warned, sink = _events("speech_gap.signal_missing")
    try:
        h = make_loop(_narrated_tool_turn([LLMTextDelta("Done.")]), tools=[read_tool()])
        start = time.monotonic()
        _run_with_playback(h, "add it", [(0.02, "start")])  # never stops
        elapsed = time.monotonic() - start
    finally:
        logger.remove(sink)
    assert 0.19 <= elapsed < 1.5 and len(warned) == 1
    assert _pushed_text(h).endswith("Done.")


def test_a_failure_in_the_outcome_rules_is_a_bypass_with_its_records(monkeypatch):
    """FR-67 covers every way of having no verdict, a bug in the rules
    included: the turn runs plain and the check still leaves its log
    line, usage event and trace row."""

    def boom(*args, **kwargs):
        raise KeyError("speak")

    monkeypatch.setattr(loop_mod, "decide", boom)
    seen, sink = _events("turn.check_bypassed")
    try:
        h = checked_loop([[LLMTextDelta("Sure."), _done()]], tools=[write_tool()])
        asyncio.run(h.loop._run_turn("add it"))
    finally:
        logger.remove(sink)
    assert _pushed_text(h) == "Sure."
    assert [t["name"] for t in h.llm.calls[0]["tools"]] == ["save"]
    (event,) = [e for e in _queued(h.recorder, UsageEvent) if e.stage == "check"]
    assert event.detail == "plain+bypass"
    assert [t.purpose for t in _queued(h.traces, LLMTrace)] == ["check", "turn"]
    assert len(seen) == 1 and seen[0]["extra"]["reason"] == "KeyError"


def test_probabilities_outside_zero_to_one_are_an_error_not_an_answer():
    """A percentage-shaped response must be a bypass, never a confident
    'nothing' that silences every turn."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "done": {"noul": 0.9},
                    "write": {"noul": 0.05},
                    "speak": {"probabilities": {"nothing": 70, "affirm": 20, "answer": 10}},
                }
            },
        )

    result, _ = asyncio.run(_jev(handler).ask("E", questions_for(ON)))
    assert result[SPEAK].status == CHECK_ERROR
    assert result[DONE].status == CHECK_ANSWERED
    assert decide(result, ON).detail == "plain+bypass"


def test_a_question_whose_switch_is_off_is_recorded_as_off():
    done_only = CheckSettings(done_on=True, model="jev-test")
    h = checked_loop([[LLMTextDelta("Sure."), _done()]], cfg=done_only)
    asyncio.run(h.loop._run_turn("add it"))
    assert h.checker.calls[0][1] == [DONE]
    (trace,) = [t for t in _queued(h.traces, LLMTrace) if t.purpose == "check"]
    questions = json.loads(trace.output)["questions"]
    assert questions["done"]["status"] == CHECK_ANSWERED
    assert questions["write"]["status"] == questions["speak"]["status"] == "off"
    (event,) = [e for e in _queued(h.recorder, UsageEvent) if e.stage == "check"]
    assert event.quantity == 1.0  # the questions SENT


def test_the_prompt_builder_makes_its_caller_choose_an_assembly():
    with pytest.raises(TypeError):
        build_system_prompt()


# ---------------- PR #28 review, round 2 ----------------


def test_the_fallback_waits_when_the_narrating_step_s_own_tool_raises(monkeypatch):
    """FR-42's primary error path, in its common shape: ONE step narrates
    and calls a tool, and the handler raises. The step's text is cleared
    before the fallback is spoken, so the narration has to be counted as
    earlier speech first — or the canned line starts over it."""
    _pause_setup(monkeypatch)
    scripts = [
        [
            LLMTextDelta("I'll add that to the document. "),
            LLMToolCall(name="echo", arguments={}, id="c1"),
            _done(),
        ],
    ]
    h = make_loop(scripts, tools=[read_tool(raises=RuntimeError("boom"))])
    times = _timed(h)
    marks = _run_with_playback(h, "add it", [(0.02, "start"), (0.08, "stop")])
    assert [t for t, _ in times][0] == "I'll add that to the document. "
    assert times[1][0] in FALLBACK_LINES
    assert times[1][1] - marks["stopped"] >= 0.19


def test_the_wait_is_sized_from_the_step_it_waits_for_not_the_whole_turn(monkeypatch):
    """Step 1 says a lot, step 2 a little, step 3 speaks: the ceiling on
    step 3's wait comes from step 2's few characters."""
    _pause_setup(monkeypatch, gap=0.05, slack=0.1, cps=100.0)
    long_narration = "x" * 400  # 4s at 100 chars/s, if it were counted
    scripts = [
        [LLMTextDelta(long_narration + " "), LLMToolCall(name="echo", arguments={}, id="c1"), _done()],
        [LLMTextDelta("Next. "), LLMToolCall(name="echo", arguments={}, id="c2"), _done()],
        [LLMTextDelta("Done."), _done()],
    ]
    h = make_loop(scripts, tools=[read_tool()])
    times = _timed(h)

    async def run():
        turn = asyncio.create_task(h.loop._run_turn("do both"))
        await asyncio.sleep(0.01)
        h.loop.note_bot_spoke()  # playing from the start, and it never stops
        await turn

    start = time.monotonic()
    asyncio.run(run())
    # two waits, each bounded by its own step: 400/100 + 0.1 for step 2,
    # then 6/100 + 0.1 for step 3 — not 406/100 + 0.1 again
    assert time.monotonic() - start < 4.1 + 0.16 + 1.0
    assert times[-1][1] - times[-2][1] < 1.0


def test_edit_and_speak_follows_the_spec_when_the_no_edit_line_is_tuned_low():
    """FR-63: an answer with p_write >= CHECK_BOTH_MIN is edit-and-speak,
    whatever CHECK_NO_WRITE_MIN is set to."""
    import dataclasses

    low = dataclasses.replace(ON, no_write_min=0.3)
    plan = decide(answers(write=0.65, answer=0.97), low)
    assert plan.speech == "edit_and_speak"


def test_a_check_answer_is_read_only():
    import dataclasses

    answer = CheckAnswer(CHECK_ANSWERED, p_yes=0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.p_yes = 0.9
