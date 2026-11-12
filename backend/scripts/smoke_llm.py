"""Live smoke test for the configured LLM provider. Spends a few tokens.

The unit suite fakes the provider SDK, so it cannot tell whether the live
API accepts the requests our clients build (key, model ids, thinking and
fallback fields). This makes one real call per client — the loop model
with a tool declared, then the FR-60 judge — through the same factories
the agent uses, and prints what came back.

    docker compose run --rm backend python scripts/smoke_llm.py

Exit code 0 means both calls streamed to a normal stop.
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

_MESSAGES = [
    {"role": "system", "content": "You are a voice assistant. Answer in one short sentence."},
    {"role": "user", "content": "Say hello, then tell me what two plus two is."},
]


async def main() -> int:
    settings = load_settings()
    print(f"provider={settings.llm_provider} loop={settings.llm_model} judge={settings.judge_model}")

    t0 = time.monotonic()
    ttfb_ms = None
    text = ""
    done = None
    async for event in make_loop_llm(settings).stream(_MESSAGES, tools=_TOOLS):
        if ttfb_ms is None:
            ttfb_ms = round((time.monotonic() - t0) * 1000)
        if isinstance(event, LLMTextDelta):
            text += event.text
        elif isinstance(event, LLMToolCall):
            print(f"  tool call: {event.name}({event.arguments})")
        elif isinstance(event, LLMDone):
            done = event
    print(f"loop:  ttfb={ttfb_ms}ms finish={done.finish_reason} usage={done.usage}")
    print(f"       {text!r}")

    t0 = time.monotonic()
    allowed, usage, verdict, finish = await judge_allows(
        make_judge_llm(settings),
        judge_messages("create_document", {"title": "Launch plan"}, ["start a new document for the launch plan"]),
    )
    judge_ms = round((time.monotonic() - t0) * 1000)
    print(f"judge: {judge_ms}ms finish={finish} verdict={verdict!r} allowed={allowed} usage={usage}")

    ok = done.finish_reason == FINISH_STOP and bool(text) and finish == FINISH_STOP and allowed
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
