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

from agent.gate import judge_allows, judge_messages  # noqa: E402
from agent.providers import (  # noqa: E402
    FINISH_STOP,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
    make_judge_llm,
    make_loop_llm,
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
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
