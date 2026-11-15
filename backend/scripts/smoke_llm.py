"""Live smoke test for the configured LLM provider. Spends a few tokens.

The unit suite fakes the provider SDK, so it cannot tell whether the live
API accepts the requests our clients build (key, model ids, thinking and
fallback fields, and every message shape the translation emits). This
makes a few real calls through the same factories the agent uses — the
loop model on each shape below, then the FR-60 judge — and prints what
came back.

    docker compose run --rm backend python scripts/smoke_llm.py

Exit code 0 means every call streamed to a normal stop.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.checks import CheckSettings, questions_for  # noqa: E402
from agent.gate import judge_allows, judge_messages  # noqa: E402
from agent.prompts import TALK_ONLY_INSTRUCTION  # noqa: E402
from agent.providers import (  # noqa: E402
    CHECK_ANSWERED,
    FINISH_STOP,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
    make_judge_llm,
    make_loop_llm,
    make_turn_checker,
)
from app.config import load_settings  # noqa: E402

_TOOLS = [
    {
        "name": "read_document",
        "description": "Read one of the user's documents by id.",
        "parameters": {
            "type": "object",
            "properties": {"document_id": {"type": "integer"}},
            "required": ["document_id"],
        },
    }
]

_SYSTEM = {"role": "system", "content": "You are a voice assistant. Answer in one short sentence."}
_TOOL_ROUND = [
    {"role": "user", "content": "Read document 1."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "toolu_smoke1", "name": "read_document", "arguments": {"document_id": 1}}],
    },
    {"role": "tool", "tool_call_id": "toolu_smoke1", "name": "read_document", "content": {"status": "cancelled"}},
]
_ASK = {"role": "user", "content": "Say hello, then tell me what two plus two is."}
# FR-65: a talk-only turn declares only the read tools, while the history
# still holds an earlier call to a write tool that is NOT declared now
_EDIT_ROUND = [
    {"role": "user", "content": "Add milk to the list."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "toolu_smoke2", "name": "edit_document", "arguments": {"document_id": 1, "append": "milk"}}],
    },
    {"role": "tool", "tool_call_id": "toolu_smoke2", "name": "edit_document", "content": {"status": "ok"}},
    {"role": "assistant", "content": "Done."},
]

# (label, messages, tool_choice): the shapes the context really produces
_CASES = [
    ("plain turn", [_SYSTEM, _ASK], "auto"),
    # a turn cut before it spoke appends nothing: the next turn follows it
    ("two user turns in a row", [_SYSTEM, {"role": "user", "content": "Hold on."}, _ASK], "auto"),
    # barge-in during a tool round: the results message, then the new turn
    ("tool round, then a user turn", [_SYSTEM, *_TOOL_ROUND, _ASK], "auto"),
    # from turn two on the stored conversation opens with the greeting
    ("conversation opening with the assistant", [_SYSTEM, {"role": "assistant", "content": "Hi there."}, _ASK], "auto"),
    # the greeting and the forced wrap-up: tools declared, selection forbidden
    ("tool_choice none", [_SYSTEM, _ASK], "none"),
    # FR-65's probe: the history calls a tool the request does not declare,
    # and the turn's one instruction rides as a trailing system message
    (
        "talk-only turn after an edit (undeclared tool in history)",
        [_SYSTEM, *_EDIT_ROUND, _ASK, {"role": "system", "content": TALK_ONLY_INSTRUCTION}],
        "auto",
    ),
]


async def main() -> int:
    settings = load_settings()
    print(f"provider={settings.llm_provider} loop={settings.llm_model} judge={settings.judge_model}")

    loop_llm = make_loop_llm(settings)
    ok = True
    for label, messages, tool_choice in _CASES:
        t0 = time.monotonic()
        ttfb_ms = None
        text = ""
        saw_call = False
        done = None
        try:
            async for event in loop_llm.stream(messages, tools=_TOOLS, tool_choice=tool_choice):
                if ttfb_ms is None and isinstance(event, (LLMTextDelta, LLMToolCall)):
                    ttfb_ms = round((time.monotonic() - t0) * 1000)
                if isinstance(event, LLMTextDelta):
                    text += event.text
                elif isinstance(event, LLMToolCall):
                    saw_call = True
                    print(f"  tool call: {event.name}({event.arguments})")
                elif isinstance(event, LLMDone):
                    done = event
        except Exception as exc:  # noqa: BLE001 — the rejection is the finding
            print(f"loop [{label}]: REJECTED {type(exc).__name__}: {str(exc)[:200]}")
            ok = False
            continue
        if done is None:
            print(f"loop [{label}]: FAILED, the stream ended without an LLMDone")
            ok = False
            continue
        print(f"loop [{label}]: ttfb={ttfb_ms}ms finish={done.finish_reason} usage={done.usage}")
        print(f"       {text!r}")
        # the question is whether the API accepted the request, not
        # whether the model chose words over a tool call
        ok = ok and done.finish_reason == FINISH_STOP and (bool(text) or saw_call)

    t0 = time.monotonic()
    allowed, usage, verdict, finish = await judge_allows(
        make_judge_llm(settings),
        judge_messages("create_document", {"title": "Launch plan"}, ["start a new document for the launch plan"]),
    )
    judge_ms = round((time.monotonic() - t0) * 1000)
    print(f"judge: {judge_ms}ms finish={finish} verdict={verdict!r} allowed={allowed} usage={usage}")

    ok = ok and finish == FINISH_STOP and allowed

    # FR-69: one request through the factory, the same fields back
    checker = make_turn_checker(settings)
    if checker is None:
        print("check: skipped, no TYPESAFE_API_KEY")
    else:
        questions = questions_for(CheckSettings(done_on=True, mode_on=True))
        t0 = time.monotonic()
        answers, usage = await checker.ask(
            "A voice conversation between a user and a work assistant. "
            "The user's latest turn: 'Add milk to the shopping list.'",
            questions,
        )
        check_ms = round((time.monotonic() - t0) * 1000)
        print(f"check: {check_ms}ms model={settings.check_model} usage={usage}")
        for q in questions:
            a = answers[q.id]
            print(f"       {q.id}: {a.status} p_yes={a.p_yes} probabilities={a.probabilities}")
        ok = ok and all(answers[q.id].status == CHECK_ANSWERED for q in questions)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
