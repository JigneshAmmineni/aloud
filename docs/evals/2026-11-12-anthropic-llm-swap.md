# Eval: the LLM swap — Claude Sonnet 5.5 loop, Claude Haiku 5.5 judge

**Written before the run (2026-11-12).** The unit suite pins the
translation layer and the stream contract against a fake SDK; it cannot
show that the live API accepts our requests, how fast the first token
arrives, or whether the agent decides better. This eval covers those
three, in that order — each part gates the next.

## What changed

- The agent loop runs on `claude-sonnet-5-5`, the FR-60 create-gate judge
  on `claude-haiku-5-5`, both through `ClaudeLoopClient` behind the FR-48
  seam. Thinking is off on both (CURRENT-ARCHITECTURE §2, "LLM models and
  thinking").
- Prompts are UNCHANGED from the Gemini-tuned text on purpose: this run
  is the baseline that says which prompt rules the new model still needs.
- Gemini stays selectable (`LLM_PROVIDER=google`) for A/B and rollback.

## Part 1 — smoke (blocks everything else)

```
docker compose run --rm backend python scripts/smoke_llm.py
```

Pass: prints `OK`. The loop call streams text to finish `stop`; the judge
answers `YES` to an explicit new-document request. A 400 here names the
request field the API rejected (the thinking setting, the fallback beta,
or the model id) — fix before any voice session.

Record: loop TTFB, judge duration, both usage lines.

## Part 2 — latency (soft target, recorded not gated)

One ordinary 10-turn voice session. From the structured logs / admin:

| Measure | Gemini baseline | Target |
|---|---|---|
| `step.1.ttfb.llm` p50 / p95 | ~1.3s typical | record; prefer ≤ baseline |
| end-of-speech → first audio p50 / p95 | ~1–1.5s typical | under ~3s (soft) |
| `step.N.gate.create_document` | under ~1s | under ~1s |

Also grep the session's logs for `llm.thinking_discarded`: each hit is a
between-tool-call note the model wrote that was not spoken. Record the
count and character sizes.

## Part 3 — decisions (owner, live voice session)

Re-run both open scripts unchanged, since they are the behaviors the swap
is meant to fix:

- `2026-10-11-create-gate-judge.md` (rows 1–5, 2b)
- `2026-10-11-voice-behavior-fixes.md` (all rows)

Plus the turn that motivated the swap, with one document attached and
under discussion:

| # | Spoken turn | Required outcome |
|---|---|---|
| A | "we need to separate the app into server side and client side. create two lists. And give me your ideas for what could be client side and server side." | BOTH halves: the lists land in the attached document AND the ideas are spoken. Gemini wrote silently and said "Done." |
| B | "what do you think of that split?" (right after A) | a spoken opinion, no write |
| C | a long instruction followed by a barge-in mid-reply, then "continue" | resumes from what was actually heard (FR-61 still holds on the new client) |

## Acceptance bar

- Part 1 passes.
- Part 3: row A delivers both halves; the two re-run scripts meet their
  own acceptance bars at least as well as the Gemini results recorded in
  their files.
- No turn is lost to a discarded between-tool-call note — if row A's
  ideas show up in `llm.thinking_discarded` instead of in speech, that is
  a failure of this swap's plumbing, not of the model, and the fix is in
  `ClaudeLoopClient` (speak those blocks) rather than in the prompt.
- Latency is recorded. Over the soft target is a finding to write down,
  not a failure.

## Results

_(append after the owner's run — needs `ANTHROPIC_API_KEY` in `.env`)_
