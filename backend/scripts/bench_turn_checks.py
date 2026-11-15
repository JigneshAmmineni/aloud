"""FR-71's bench for the §4.15 turn checks. Local-only diagnostic, never
imported by runtime code; it goes through the provider factory, the real
questions, the real evidence builder and the real outcome rules.

Two modes, because accuracy does not depend on where it is measured and
response time does:

  # accuracy, on the development machine (the set never leaves it):
  docker compose run --rm -v "${PWD}/local-evals/turn-checks:/set" backend \
      python scripts/bench_turn_checks.py --accuracy /set

  # response time, from wherever it runs (the VM: production's figures).
  # Sends only the scripted sentences below — no recorded turn:
  docker compose run --rm backend python scripts/bench_turn_checks.py --latency

The labelled set is two files in one folder: labelled.json (per row: n,
text, reply, unanswered) and labels.csv (per row: n, done, write, speak).
Labels: done and write are y / n; speak is n (nothing), a (affirm) or
y (answer); ? leaves the row out of that count. It holds transcript text,
so the folder is git-ignored and is never copied to the server.
"""

import asyncio
import csv
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.checks import (  # noqa: E402
    DONE,
    SILENT_MID_THOUGHT,
    SILENT_NOTHING_ASKED,
    WRITE,
    CheckSettings,
    build_evidence,
    decide,
    questions_for,
)
from agent.context import ContextProvider  # noqa: E402
from agent.providers import CHECK_ANSWERED, make_turn_checker  # noqa: E402
from app.config import load_settings  # noqa: E402

# The live script's shapes (docs/evals/2026-11-13-decision-gates.md,
# part 3): what a response-time run sends instead of recorded turns.
_SCRIPTED = [
    (None, "What's a good temperature rating for a sleeping bag in spring?"),
    (None, "So I'm thinking about a camping trip next month, maybe two nights, probably"),
    (None, "Create a new document called Camping Trip Plan with what I've said so far."),
    ("Done.", "Add that we need to book the campsite by Friday."),
    ("Done.", "Give me a quick summary of the plan."),
    ("It covers gear, food and the schedule.", "What am I missing?"),
    ("It covers gear, food and the schedule.", "Okay."),
    ("So gear first, then food. Is that right?", "Yeah."),
    ("Done.", "Split the list into client side and server side, and give me your ideas for each."),
    ("Done.", "Explain the schedule section in detail."),
    ("Done.", "Read me the gear section."),
    ("Done.", "Can you put the"),
]

_ALL_ON = dict(done_on=True, mode_on=True)


def _evidence(reply: str | None, unanswered: list[str], text: str) -> str:
    """The evidence FR-68 would send for this turn, via the real builder."""
    ctx = ContextProvider("system")
    if reply:
        ctx.append_assistant(reply)
    for earlier in unanswered:
        ctx.append_user(earlier)
    ctx.append_user(text)
    return build_evidence(ctx)


def _pct(values: list[int], p: float) -> int:
    values = sorted(values)
    return values[min(len(values) - 1, round(p * (len(values) - 1)))]


async def _ask(checker, cfg, evidence):
    t0 = time.monotonic()
    try:
        answers, _usage = await checker.ask(evidence, questions_for(cfg))
    except Exception as exc:  # noqa: BLE001 — a bench records every failure
        return None, round((time.monotonic() - t0) * 1000), type(exc).__name__
    return answers, round((time.monotonic() - t0) * 1000), None


def _times(ms: list[int], errors: int) -> None:
    print(
        f"response time ms: p50 {_pct(ms, .5)}  p95 {_pct(ms, .95)}  max {max(ms)}"
        f"  over 1000: {sum(m > 1000 for m in ms)}  errors: {errors}"
    )


async def latency(checker, cfg, repeat: int = 3) -> int:
    ms, errors = [], 0
    for reply, text in _SCRIPTED * repeat:
        answers, took, error = await _ask(checker, cfg, _evidence(reply, [], text))
        ms.append(took)
        errors += error is not None
    print(f"{len(ms)} scripted requests to {cfg.model}")
    _times(ms, errors)
    return 0 if not errors else 1


def _expected(row: dict) -> str | None:
    """The outcome FR-63 should select for a row's labels."""
    if row["done"] == "n":
        return SILENT_MID_THOUGHT
    if "?" in (row["done"], row["write"], row["speak"]):
        return None
    if row["write"] == "y":
        return "edit_and_speak" if row["speak"] == "y" else "affirm"
    return {"n": SILENT_NOTHING_ASKED, "a": "talk_only+affirm", "y": "talk_only"}[
        row["speak"]
    ]


async def accuracy(checker, cfg, folder: Path) -> int:
    rows = {r["n"]: r for r in json.loads((folder / "labelled.json").read_text("utf-8"))}
    with open(folder / "labels.csv", encoding="utf-8-sig", newline="") as f:
        for label in csv.DictReader(f):
            for q in ("done", "write", "speak"):
                rows[int(label["n"])][q] = label[q].strip().lower()[:1] or "?"
    results, ms, errors = [], [], 0
    for n in sorted(rows):
        row = rows[n]
        answers, took, error = await _ask(
            checker, cfg, _evidence(row.get("reply"), row.get("unanswered") or [], row["text"])
        )
        ms.append(took)
        if error or any(a.status != CHECK_ANSWERED for a in answers.values()):
            errors += 1
            continue
        results.append((row, answers))
    print(f"{len(rows)} labelled turns to {cfg.model}; {len(results)} fully answered")
    _times(ms, errors)

    def asked(row):
        return row["write"] == "y" or row["speak"] in ("a", "y")

    real = [(r, a) for r, a in results if r["done"] == "y" and asked(r)]
    mid = [(r, a) for r, a in results if r["done"] == "n"]
    print(f"\nDONE  (real requests: {len(real)}, mid-thought turns: {len(mid)})")
    print("  stay silent when 1 - p_done >= | wrongly silent | wrongly replying")
    for th in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        silent = [r["n"] for r, a in real if 1 - a[DONE].p_yes >= th]
        replied = [r["n"] for r, a in mid if 1 - a[DONE].p_yes < th]
        print(f"     {th:.1f}  | {len(silent):>2} {silent} | {len(replied):>2} {replied}")

    edits = [(r, a) for r, a in results if r["write"] == "y"]
    no_edits = [(r, a) for r, a in results if r["write"] == "n"]
    print(f"\nWRITE (edit asked: {len(edits)}, not asked: {len(no_edits)})")
    for th in (0.5, 0.6, 0.7, 0.8, 0.9):
        withheld = [r["n"] for r, a in edits if 1 - a[WRITE].p_yes >= th]
        kept = sum(1 for r, a in no_edits if 1 - a[WRITE].p_yes >= th)
        print(
            f"  withhold when 1 - p_write >= {th:.1f}: wrongly withheld "
            f"{len(withheld)} {withheld} | correctly withheld {kept} of {len(no_edits)}"
        )

    scored = [(r, decide(a, cfg).detail, _expected(r)) for r, a in results]
    scored = [(r, got, want) for r, got, want in scored if want]
    right = sum(got == want for _, got, want in scored)
    print(
        f"\nOUTCOME at the configured thresholds (mid-thought {cfg.mid_thought_min}, "
        f"withhold {cfg.no_write_min}, nothing {cfg.nothing_min}, affirm "
        f"{cfg.affirm_min}, edit-and-speak {cfg.both_min}): "
        f"{right} of {len(scored)} labelled turns get the outcome their labels call for"
    )
    misses: dict = {}
    for row, got, want in scored:
        if got != want:
            misses.setdefault((want, got), []).append(row["n"])
    for (want, got), ns in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        print(f"  expected {want:<22} got {got:<22} x{len(ns)}  rows {ns}")

    def count(pred):
        return [r["n"] for r, got, want in scored if pred(r, got, want)]

    print("\nBy kind of mistake (row numbers in labels.csv, never text):")
    print("  wrongly silent:    ", count(lambda r, g, w: g.startswith("silent") and not w.startswith("silent")))
    print("  wrongly replying:  ", count(lambda r, g, w: w.startswith("silent") and not g.startswith("silent")))
    print("  wrongly withheld:  ", count(lambda r, g, w: r["write"] == "y" and "talk_only" in g))
    print("  wrongly instructed:", count(lambda r, g, w: g == "edit_and_speak" and w != "edit_and_speak"))
    return 0


async def main() -> int:
    settings = load_settings()
    checker = make_turn_checker(settings)
    if checker is None:
        print("TYPESAFE_API_KEY is not set: nothing to bench.")
        return 1
    # every question is asked, whatever the switches say: the bench is how
    # the owner decides what to switch on
    configured = CheckSettings.from_settings(settings)
    cfg = dataclasses.replace(configured, **_ALL_ON)
    if "--latency" in sys.argv:
        return await latency(checker, cfg)
    if "--accuracy" in sys.argv:
        at = sys.argv.index("--accuracy") + 1
        if at >= len(sys.argv) or not Path(sys.argv[at]).is_dir():
            print("--accuracy needs the folder that holds labelled.json and labels.csv")
            return 2
        return await accuracy(checker, cfg, Path(sys.argv[at]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
