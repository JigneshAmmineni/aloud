"""Replay recorded model calls ("frames") against the current prompt.
Local-only diagnostic, never imported by runtime code. Spends a few
cents per run.

A frame is one turn of a real session, saved up to the moment the main
model was called: the conversation as it stood (speech-recognition
errors corrected by hand), a label naming the behaviour under test, and
what the FIRST call of that turn must do. Replaying it is one model call
through the same factory the agent uses, with today's system prompt,
one-turn instruction and tool descriptions in place of the recorded
ones. So a change to any of those can be checked in a minute, before
anyone has the same conversation with the agent again.

    docker compose run --rm -v "${PWD}/local-evals/frames:/set" backend \
        python scripts/replay_frames.py /set [--runs 3] [--only 1.07,summary] [--show]

It checks the first decision of a turn (which tool, which document, what
is said). It cannot check multi-step work, speech recognition, the turn
checks (scripts/bench_turn_checks.py replays those) or audio. One call
can go either way by chance, so every frame runs --runs times and the
count is what is reported.

A frame is one JSON file: id, label, source, note, messages, expect.
In `messages`, a system message whose content is "@SYSTEM_PROMPT",
"@AFFIRM", "@ANSWER" or "@EDIT_AND_SPEAK" is filled in from
agent/prompts.py at replay time. `expect` may hold:

    no_calls         true: the call uses no tool
    calls_must       tool names that must all be called
    calls_any        tool names of which at least one must be called
    calls_none       tool names that must not be called
    target_document  every edit_document call must carry this document_id
    quiet_work       true: a call that uses a tool says nothing with it
    max_sentences    bound on the spoken text
    max_words        bound on the spoken text
    min_words        the spoken text is at least this long (explain, recite)
    must_say         phrases the spoken text must contain
    must_not_say     phrases the spoken text must not contain

Frames hold transcript text, so the folder is git-ignored and is never
copied to the server.
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.prompts import (  # noqa: E402
    AFFIRM_INSTRUCTION,
    ANSWER_INSTRUCTION,
    EDIT_AND_SPEAK_INSTRUCTION,
    build_system_prompt,
)
from agent.providers import LLMTextDelta, LLMToolCall, make_loop_llm  # noqa: E402
from agent.tools import build_registry, registry_schemas  # noqa: E402
from app.config import load_settings  # noqa: E402

# Every key `failures` reads. A key outside this set is a typo, and a
# typo would otherwise read as a bound that was met.
EXPECT_KEYS = {
    "no_calls",
    "calls_must",
    "calls_any",
    "calls_none",
    "target_document",
    "quiet_work",
    "max_sentences",
    "max_words",
    "min_words",
    "must_say",
    "must_not_say",
}


def fill(messages: list[dict]) -> list[dict]:
    """The recorded conversation with today's prompt texts in place."""
    current = {
        "@SYSTEM_PROMPT": build_system_prompt(checks_on=True),
        "@AFFIRM": AFFIRM_INSTRUCTION,
        "@ANSWER": ANSWER_INSTRUCTION,
        "@EDIT_AND_SPEAK": EDIT_AND_SPEAK_INSTRUCTION,
    }
    return [
        {**m, "content": current[m["content"]]}
        if m["role"] == "system" and m["content"] in current
        else m
        for m in messages
    ]


def failures(expect: dict, text: str, calls: list[LLMToolCall]) -> list[str]:
    """Why one reply misses the frame's expectation; empty when it passes."""
    names = [c.name for c in calls]
    out = []
    if expect.get("no_calls") and names:
        out.append(f"called {names}, expected no tool")
    for name in expect.get("calls_must", []):
        if name not in names:
            out.append(f"did not call {name}")
    any_of = expect.get("calls_any")
    if any_of and not set(any_of) & set(names):
        out.append(f"called {names or 'nothing'}, expected one of {any_of}")
    for name in expect.get("calls_none", []):
        if name in names:
            out.append(f"called {name}")
    target = expect.get("target_document")
    if target is not None:
        for c in calls:
            if c.name == "edit_document" and c.arguments.get("document_id") != target:
                out.append(
                    f"edited document {c.arguments.get('document_id')}, expected {target}"
                )
    if expect.get("quiet_work") and names and text:
        out.append("spoke while calling a tool")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
    if len(sentences) > expect.get("max_sentences", len(sentences)):
        out.append(f"{len(sentences)} sentences, at most {expect['max_sentences']}")
    words = len(text.split())
    if words > expect.get("max_words", words):
        out.append(f"{words} words, at most {expect['max_words']}")
    if words < expect.get("min_words", 0):
        out.append(f"{words} words, at least {expect['min_words']}")
    for phrase in expect.get("must_say", []):
        if phrase.lower() not in text.lower():
            out.append(f"did not say {phrase!r}")
    for phrase in expect.get("must_not_say", []):
        if phrase.lower() in text.lower():
            out.append(f"said {phrase!r}")
    return out


async def one_call(llm, schemas, messages) -> tuple[str, list[LLMToolCall]]:
    text, calls = "", []
    async for event in llm.stream(messages, tools=schemas):
        if isinstance(event, LLMTextDelta):
            text += event.text
        elif isinstance(event, LLMToolCall):
            calls.append(event)
    return text.strip(), calls


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--only", default="", help="replay frames whose id or label contains one of these (comma-separated)"
    )
    parser.add_argument("--show", action="store_true", help="print every reply, not only the misses")
    args = parser.parse_args()

    frames = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(Path(args.folder).glob("*.json"))
    ]
    # Checked over the WHOLE folder, before --only narrows it: a typo in a
    # frame this run skips is still a typo, and --only is the usual mode.
    mistyped = {
        f["id"]: sorted(set(f.get("expect") or {}) - EXPECT_KEYS)
        for f in frames
        if set(f.get("expect") or {}) - EXPECT_KEYS
    }
    if mistyped:
        for frame_id, keys in mistyped.items():
            print(f"{frame_id}: unknown expect key(s) {keys}")
        return 2
    wanted = [t for t in args.only.split(",") if t] or [""]
    frames = [f for f in frames if any(t in f["id"] + " " + f["label"] for t in wanted)]
    if not frames:
        print(f"no frames in {args.folder}")
        return 1

    llm = make_loop_llm(load_settings())
    schemas = registry_schemas(build_registry())
    failed_frames = 0
    for frame in frames:
        messages = fill(frame["messages"])
        replies = await asyncio.gather(
            *(one_call(llm, schemas, messages) for _ in range(args.runs))
        )
        verdicts = [failures(frame.get("expect", {}), text, calls) for text, calls in replies]
        passed = sum(1 for v in verdicts if not v)
        gated = bool(frame.get("expect"))
        status = f"{passed}/{args.runs}" if gated else "recorded"
        if gated and passed < args.runs:
            failed_frames += 1
        words = [len(text.split()) for text, _ in replies]
        print(f"{status:>8}  {frame['id']:<6} {frame['label']:<42} words spoken {words}")
        for (text, calls), verdict in zip(replies, verdicts):
            if verdict or not gated or args.show:
                called = ", ".join(c.name for c in calls) or "no tool"
                print(f"            [{called}] {text[:300]!r}")
                for reason in verdict:
                    print(f"              - {reason}")
    gated_frames = sum(1 for f in frames if f.get("expect"))
    print(
        f"\n{gated_frames - failed_frames} of {gated_frames} gated frames passed "
        f"every run ({len(frames) - gated_frames} recorded only, not counted)"
    )
    return 1 if failed_frames else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
