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

### Part 1 — smoke: PASS (2026-11-12)

`provider=anthropic loop=claude-sonnet-5-5 judge=claude-haiku-5-5`, `OK`
on two consecutive runs. The API accepted both request shapes: thinking
`between_tools` plus the server-side fallback beta on Sonnet, thinking
`disabled` on Haiku.

| Run | Loop TTFB | Loop usage (in / out) | Judge duration | Judge usage (in / out) | Verdict |
|---|---|---|---|---|---|
| 1 | 1748 ms | 407 / 14 | 701 ms | 343 / 4 | YES |
| 2 | 2043 ms | 407 / 14 | 815 ms | 343 / 4 | YES |

For comparison, the same script on Gemini 2.5 Flash an hour earlier:
loop TTFB 531 ms, judge 574 ms. Every smoke run is a new process, so
these numbers include opening the connection and are a worst case; a
session reuses one client, and Part 2's `step.1.ttfb.llm` is the figure
that counts. They do say the first-token time is the thing to watch.

### Parts 2 and 3 — first live session (owner, 2026-11-12)

Session `c0ced259…`, 10 turns, about 6 minutes, one unrelated document
in the account. Not the scripted rows; an ordinary working session.
Owner verdict: noticeably smarter than Gemini, latency not a noticeable
problem, and it knew when to keep listening and when to answer.

**Latency (Part 2).**

| Measure | Recorded |
|---|---|
| `step.1.ttfb.llm`, plain turns (n=5) | 1432–1504 ms |
| end-of-speech → first audio, plain turns | 2096–2321 ms |
| end-of-speech → first audio, tool turns (n=4) | 3604–4496 ms as recorded |
| `step.N.gate.create_document` (n=4) | 685, 976, 1230, 1884 ms — two over the ~1s bar |
| `llm.thinking_discarded` | 0 — no between-tool-call note was dropped |

First-token time is about 0.15–0.2s over the Gemini baseline; plain turns
stay under the soft 3s target, tool turns do not.

**Tokens.** Sonnet: 79,764 in / 2,308 out over 17 calls. Haiku: 2,046 in /
16 out over 4 calls. About $0.18 for the session, nearly all of it input:
every call resends a context that starts at 3,470 tokens (system prompt
plus tool definitions) and reached 7,036.

**Finding 1 — the gate refused a confirmed create. FIXED in this PR.**
The agent asked "No existing document fits this. Do you want a new
document for this?", the owner said "Yeah.", and the Haiku judge answered
NO twice in that turn. The judge's evidence listed the assistant's reply
AHEAD of the user turns, so nothing tied the bare "Yeah." to the offer.
Fix: the reply now follows the user turns and is described as what the
latest turn responds to, and the judge's instructions name a plain yes to
a new-document offer as a YES. Replayed live against Haiku, 12 cases x 3
runs: before 33/36 (the owner's exact turn failed 3/3), after 36/36,
including yes-to-an-EDIT-offer, a declined offer, and a stale offer,
which must stay NO.

**Finding 2 — tool work is narrated aloud. Carried to the next PR.**
Sonnet writes a short sentence before most tool calls and the loop speaks
it: "I'll write it up as a cleaned-up idea document.", "I'll add that to
the document.", and, after the refusals above, "The gate refused again,
even after the user's yes. Per the instructions, I'll check for an
existing document to write to." The prompt's "work silently" rule does
not hold on this model. These stayed `text` (short), which is why nothing
was discarded as a thinking block.

**Finding 3 — answers run long. Carried to the next PR.** "What are
options for getting the suppliers?" got 344 tokens: a good first two
sentences, then every option with its details. The prompt says "keep
replies brief" next to "answer directly and completely", with no concrete
bound; Sonnet resolves that toward complete.

Part 3's scripted rows (A–C and the two 2026-10-11 scripts) are still
unrun.
