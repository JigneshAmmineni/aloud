# Eval: turn checks and answer modes (§4.15)

**Written before any run (2026-11-13), with the spec.** This is the
record FR-71 points to. It has three parts, in the order they run:

1. an early bench of the decision model on recorded turns, before the
   spec's review rounds are spent;
2. the full bench from the production VM, once §4.15 is built;
3. the live voice run.

A check is switched on in production only after parts 2 and 3 and the
privacy-policy update, on the owner's decision. No pass mark is set in
advance: the numbers below are recorded and read, not gated.

## What is being tested

- The three questions asked at each end of turn (FR-62): DONE (has the
  user finished, or are they mid-thought?), WRITE (did they ask for
  something to be written or changed?), SPEAK (do they expect something
  spoken back?).
- The five outcomes those answers select (FR-63): silent (mid-thought),
  silent (nothing asked), talk only, edit and speak, plain.
- The answer modes (FR-72): 1 silent execution, 2 summary in one or two
  sentences, 3 explain or recite on an explicit ask. "Hmm." is gone from
  the prompt (FR-73).
- The first-sentence cut (FR-61 amended).

## Part 1 — early bench on recorded turns (owner's own turns only)

Purpose: the design rests on two things nobody has measured from here,
that the decision model answers in about a tenth of a second and that it
judges this product's kind of turn well. If either is false the spec
changes, so this runs first.

- **The set.** About 100 turns picked from the owner's recorded
  sessions in the local database, each with the evidence FR-68 would
  have sent (the turn, unanswered turns before it, the agent's last
  reply). Picked to cover: trailing thoughts, one-word answers to a
  question the agent asked, listening sounds ("Okay.", "Hmm."), plain
  edits, plain questions, edit-plus-ideas turns, explicit "explain" or
  "read me" asks, and speech-recognition garble.
- **The labels.** For each turn, the right answer to DONE, WRITE and
  SPEAK. Drafted by Claude, then reviewed once by the owner; the
  owner's judgement is the reference. A turn the owner cannot label
  either way is marked unsure and left out of the mistake counts.
- **Where it lives.** The set holds transcript text, so it stays in a
  gitignored local folder and is never committed.
- **The run.** A throwaway script sends each turn's three questions to
  the decision model (pinned version) and records each probability and
  response time. The same turns go through the FR-60 judge model
  (Haiku) as a reference column.
- **What is recorded here afterwards:** per question, response time
  p50 / p95 / max; per question, a table of thresholds against the
  count of each mistake (wrongly silent, wrongly replying, wrongly
  withheld, wrongly instructed); the reference column's counts; and the
  turns the model got wrong at the chosen thresholds, by category, with
  no transcript text (row numbers into the local set only).

Needs from the owner: a TypeSafe API key in the local `.env`
(`TYPESAFE_API_KEY`), and one review pass over the labels.

## Part 2 — full bench, once §4.15 is built

`backend/scripts/bench_turn_checks.py` (FR-71), which goes through the
provider factory rather than calling the vendor directly, in its two
modes:

- **Accuracy, on the development machine:** the same labelled set as
  part 1. The recorded turns never leave that machine. Record the same
  mistake tables as part 1, and the thresholds chosen from them.
- **Response time, from the production VM:** the scripted sentences of
  part 3 below, not recorded turns, so no transcript text is copied to
  the server. Record p50, p95 and max per question: these are the
  figures production would see.

## Part 3 — live voice run (owner, local dev server, then the live site)

Fresh session, no documents attached, both switches on. Topic is free;
the turns below are shapes, not exact words. Every row names the
outcome the checks should select and the answer mode the reply should
take. Duration class: modes 1 and 2 finish in seconds; mode 3 takes as
long as its output. A row that lands in a slower class fails.

Core rows (the standing regression set, unchanged from the 2026-11-12
run 2 script except for the expected columns):

| # | Spoken turn | Outcome | Mode | Required result |
|---|---|---|---|---|
| 1 | (connect) | not checked | — | a few words of greeting |
| 2 | a plain factual question | plain or talk only | 2 | one or two sentences, no document |
| 3 | thinking aloud, trailing off ("…maybe two nights? Probably…") | silent (mid-thought) | — | nothing spoken; the indicator returns to Listening |
| 4 | "Create a new document called … with what I've said so far." | plain | 1 | one document; "Done." |
| 5 | "Add that we need to book the campsite by Friday." | plain | 1 | edit without asking; "Done." |
| 6 | "Give me a quick summary of the plan." | talk only | 2 | spoken, one or two sentences, nothing written |
| 7 | "Organize the plan into three sections: …" | plain | 1 | restructured; "Done." |
| 8 | "What am I missing?" | talk only | 2 | spoken, no write; a short list is allowed here |
| 9 | "Poke holes in this plan." | talk only | 2 | spoken critique, no write |
| 10 | "Rename the document to …" | plain | 1 | one rename; "Done." |
| 11 | "Which of my documents mention a budget?" | talk only | 2 | silent search (a read tool on a talk-only turn), then the answer |
| 12 | "Change the line about X to Y." (no such line) | plain | 2 | one short sentence that it could not; no tool names |
| 13 | "Actually, never mind. Let's go back to the trip." | silent (nothing asked) or plain | 2 | no tool; silence is acceptable |
| 14 | "Walk me through the whole plan in detail." / "Hold on." / "Okay. Continue." | talk only, then silent or plain, then plain | 3 | long answer; resumes from what was heard |
| 15 | "Stop." mid-reply | silent (nothing asked) | — | the agent stops and stays stopped |
| 16 | "Can you put the…" | silent (mid-thought) | — | nothing spoken; then finish the sentence and it acts on the whole thought |
| 17 | "That's all for now." | silent (nothing asked) or plain | 2 | silence or one short line |

Rows for this change:

| # | Spoken turn | Outcome | Mode | Required result |
|---|---|---|---|---|
| 18 | "I think we should probably…" (pause five seconds or more) "…go with the cheaper campsite." | silent (mid-thought), then plain or talk only | 2 | silence through the pause, however long; then one reply to the whole thought |
| 19 | "Okay." / "Right, right." / "Hmm." after a reply | silent (nothing asked) | — | nothing spoken, each time |
| 20 | after the agent asks a yes/no question: "Yeah." | plain | 1 | it acts on the yes; not judged mid-thought or nothing-asked |
| 21 | "Split the list into client side and server side, and give me your ideas for each." | edit and speak | 1 then 2 | the edit lands AND the ideas are spoken |
| 22 | "What do you think of that split?" | talk only | 2 | spoken opinion, one or two sentences, no write |
| 23 | "Should we add a budget section? What would go in it?" | talk only | 2 | spoken answer; nothing written, and it does not claim to have written anything |
| 24 | "Add a budget section with those." | plain | 1 | the edit lands; "Done." |
| 25 | "Explain the schedule section in detail." | talk only | 3 | a full explanation |
| 26 | "Read me the gear section." | talk only | 3 | the section read word for word |
| 27 | "Give me five things I should double-check before leaving." | talk only | 2 | a spoken list: at most five points, one clause each, no bullet words |
| 28 | a question, then barge in inside the reply's first second with a different question | plain | 2 | the second answer does not treat the first reply as heard |
| 29 | say "mm-hmm" while the agent is mid-reply | silent (nothing asked) | — | recorded, not gated: the agent stops and does not resume (accepted limit); note how it feels |
| 30 | a garbled or half-heard instruction | plain | 2 | "say that again?" in some form; never "Hmm." |
| 31 | a real request, immediately after row 19's silence | plain or talk only | per row | the reply uses the earlier turn too if it was part of the thought, and ignores it if not |

Also run once with each switch off in turn (rows 3, 6, 19 and 21 are
enough): with a switch off, its outcomes must not occur and the agent
must behave as it did before.

## Acceptance bar

- **Wrongly silent** (a finished request that got nothing) and
  **wrongly replying** (a reply into a pause): counted per run from the
  `turn.check` log lines and the owner's notes. The owner prefers the
  first mistake to the second; the thresholds are tuned until the
  balance feels right, and the final values are recorded here.
- **Wrongly withheld** (an edit request that could not edit): the agent
  must have said the change was not made, never that it was.
- Rows 3, 16, 18 and 19: no audio from the agent, and the indicator is
  not left on Thinking.
- Row 21: both halves, every time it is tried.
- No reply to a mode-2 row runs past two sentences, the list rows
  excepted. Record the sentence count per row.
- No turn's transcript contains "Hmm." or "Mm-hmm." from the agent
  (both switches on; with a switch off, today's rule is back by
  design, FR-73).
- Row 28: the context after the cut holds only the reply's first
  sentence, marked as cut (`context.playback_cut` in the logs).
- Check response time on every turn, read from the `turn.check` log
  lines (silent turns write those too; the `check.*` stage keys exist
  only on turns that spoke): record p50 and the worst; count
  `turn.check_bypassed` lines. A bypass is not a failure of the run,
  but its count is recorded.
- End-of-speech → first audio on the plain and talk-only rows,
  compared with the 2026-11-12 run 2 figures: the checks should add
  about their own response time and no more.

## Results

### Part 1, run 1 — 2026-11-14, owner's development machine

102 turns, `jev-1.14.0`, all three questions in one request per turn (the
API takes several questions per request). No errors. **The labels are
Claude's draft: the owner has not reviewed them yet**, so every count
below is against the draft and will move.

**Response time** (one request, three answers; from the development
machine, not the VM): p50 120 ms, p95 152 ms, mean 135 ms. Two requests
were slow: the first of the run (383 ms) and one outlier at 1,136 ms,
which FR-67's one-second ceiling would have bypassed. About 440 input
tokens per request. The Haiku reference took p50 930 ms, p95 1,921 ms
for the same three questions asked in parallel.

**DONE (is the user finished?).** The 13 mid-thought turns separate
well: 12 of them score 0.36 or lower. Split by what the turn was:

| Stay silent when 1 − p_done ≥ | real requests silenced (of 52) | nothing-asked turns silenced (of 12) | mid-thought turns answered (of 13) |
|---|---|---|---|
| 0.4 | 7 | 6 | 0 |
| 0.5 | 6 | 4 | 1 |
| 0.6 | 5 | 3 | 1 |
| 0.7 | 4 | 3 | 4 |
| 0.8 | 1 | 1 | 5 |

The real requests it silences (rows 33, 34, 40, 54, 77, 86 of the local
set) are all rough speech: a filler word or a restart inside the
sentence, a garbled ending, or no closing punctuation from the
recogniser. The one mid-thought turn it answers (row 35) is arguably a
complete question.

**WRITE (was an edit asked for?).** Works as worded. At the spec's
starting threshold (withhold when 1 − p_write ≥ 0.8) no edit request
of 16 is wrongly withheld, and the tools are withheld on 59 of the 63
turns that asked for no edit. The lowest-scoring real edit requests are
a garbled one (0.45) and the two edit-plus-ideas turns (0.54, 0.66).

**SPEAK (is something spoken expected back?).** Never wrongly quiet:
all 38 turns that wanted a spoken answer score 0.76 or higher. But the
question as worded does not pick out the turns that want nothing:

- listening sounds and one-word acknowledgements score 0.25 to 0.70;
- plain edit requests score 0.44 to 0.85, because a confirmation counts
  as "something said back";
- thinking-aloud statements score 0.84 to 0.90.

Two consequences at the starting thresholds: the nothing-asked silence
would fire on fewer than half of the listening sounds and on none of the
thinking-aloud statements, and the edit-and-speak instruction would be
added to plain edit requests (11 of them at a 0.5 threshold; at 0.7 it
misses both real edit-plus-ideas turns, whose p_write is 0.54 and 0.66).

**Haiku reference column: not usable yet.** It answered "no edit asked"
on plain edit requests (row 44, for one), which points at the reference
harness (its prompt or its one-word parsing), not at Haiku. Its timing
stands; its accuracy figures are discarded until the raw replies are
inspected.

**Findings.**

1. The speed the design rests on holds from here: about an eighth of a
   second, with one request in 102 over the ceiling. The VM measurement
   is still owed (part 2).
2. WRITE can be used as it is.
3. DONE catches mid-thought turns, at the cost of silencing about one
   real request in ten at 0.6. The misses share a cause the wording can
   address: say that the text is speech-recogniser output with unreliable
   punctuation, and that a filler word or a restart inside a complete
   request is not a pause. Re-run before choosing a threshold.
4. SPEAK needs rewording to "substance beyond a brief confirmation",
   and one labelling question is the owner's: does a thinking-aloud
   statement, or an answer to the agent's own question, expect something
   said back? 36 turns are marked unsure on exactly that.
5. Only 13 mid-thought and 2 edit-plus-ideas examples exist in the
   recordings. Both results are rough; the live run (part 3) is where
   they get tested properly.

Next: the owner's label review, then run 2 with the reworded DONE and
SPEAK questions and a fixed reference column.
