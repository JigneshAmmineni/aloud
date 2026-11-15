# Eval: turn checks and answer modes (§4.15)

**Written before any run (2026-11-13), with the spec.** This is the
record FR-71 points to. It has three parts, in the order they run:

1. an early bench of the decision model on recorded turns, before the
   spec's review rounds are spent;
2. the full bench from the production VM, once §4.15 is built;
3. the live voice run.

The checks are on by default once built (FR-71). This file is the
acceptance record the owner reads before deploying them; either check
can be switched off afterwards. No pass mark is set in advance: the
numbers below are recorded and read, not gated.

## What is being tested

- The three questions asked at each end of turn (FR-62): DONE (has the
  user finished, or are they mid-thought?), WRITE (did they ask for
  something to be written or changed?), SPEAK (do they expect nothing,
  a few words, or a real answer?).
- What those answers decide (FR-63): silence (mid-thought, or nothing
  asked), the affirm instruction, the answer instruction, the
  edit-and-speak instruction, or a plain turn. They never take a tool
  away.
- The answer modes (FR-72): 1 affirm in five words or fewer, 2 summary
  in one or two sentences, 3 explain or recite on an explicit ask.
  "Hmm." is gone from the prompt while both checks are on (FR-73).
- The one-second pause between two spoken steps of one turn, which
  replaces the dropped "Done." (FR-43 amended).
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

## Part 3 — live voice run (owner, local dev server)

Run on the owner's development server before the deploy, and again on
the live site after it. Fresh session, no documents attached, both
switches on (the default). Topic is free;
the turns below are shapes, not exact words. Every row names the
outcome the checks should select and the answer mode the reply should
take. Duration class: modes 1 and 2 finish in seconds; mode 3 takes as
long as its output. A row that lands in a slower class fails.

Core rows (the standing regression set, unchanged from the 2026-11-12
run 2 script except for the expected columns):

| # | Spoken turn | Outcome | Mode | Required result |
|---|---|---|---|---|
| 1 | (connect) | not checked | — | a few words of greeting |
| 2 | a plain factual question | answer | 2 | one or two sentences, no document |
| 3 | thinking aloud, trailing off ("…maybe two nights? Probably…") | silent (mid-thought) | — | nothing spoken; the indicator returns to Listening |
| 4 | "Create a new document called … with what I've said so far." | affirm | 1 | one document; a few words of confirmation |
| 5 | "Add that we need to book the campsite by Friday." | affirm | 1 | edit without asking; a few words of confirmation |
| 6 | "Give me a quick summary of the plan." | answer | 2 | spoken, one or two sentences, nothing written |
| 7 | "Organize the plan into three sections: …" | affirm | 1 | restructured; a few words of confirmation |
| 8 | "What am I missing?" | answer | 2 | spoken, no write; a short list is allowed here |
| 9 | "Poke holes in this plan." | answer | 2 | spoken critique, no write |
| 10 | "Rename the document to …" | affirm | 1 | one rename; a few words of confirmation |
| 11 | "Which of my documents mention a budget?" | answer | 2 | silent search, then the answer |
| 12 | "Change the line about X to Y." (no such line) | plain | 2 | one short sentence that it could not; no tool names |
| 13 | "Actually, never mind. Let's go back to the trip." | silent (nothing asked) or affirm | 2 | no tool; silence is acceptable |
| 14 | "Walk me through the whole plan in detail." / "Hold on." / "Okay. Continue." | answer, then silent, then answer | 3 | long answer; resumes from what was heard |
| 15 | "Stop." mid-reply | silent (nothing asked) | — | the agent stops and stays stopped |
| 16 | "Can you put the…" | silent (mid-thought) | — | nothing spoken; then finish the sentence and it acts on the whole thought |
| 17 | "That's all for now." | silent (nothing asked) or affirm | 2 | silence or one short line |

Rows for this change:

| # | Spoken turn | Outcome | Mode | Required result |
|---|---|---|---|---|
| 18 | "I think we should probably…" (pause five seconds or more) "…go with the cheaper campsite." | silent (mid-thought), then answer | 2 | silence through the pause, however long; then one reply to the whole thought |
| 19 | "Okay." / "Right, right." / "Hmm." after a reply | silent (nothing asked) | — | nothing spoken, each time |
| 20 | after the agent asks a yes/no question: "Yeah." | affirm | 1 | it acts on the yes; not judged mid-thought or nothing-asked |
| 21 | "Split the list into client side and server side, and give me your ideas for each." | edit and speak | 1 then 2 | the edit lands AND the ideas are spoken |
| 22 | "What do you think of that split?" | answer | 2 | spoken opinion, one or two sentences, no write |
| 23 | "Should we add a budget section? What would go in it?" | answer | 2 | spoken answer; nothing written, and it does not claim to have written anything |
| 24 | "Add a budget section with those." | affirm | 1 | the edit lands; a few words of confirmation |
| 25 | "Explain the schedule section in detail." | answer | 3 | a full explanation |
| 26 | "Read me the gear section." | answer | 3 | the section read word for word |
| 27 | "Give me five things I should double-check before leaving." | answer | 2 | a spoken list: at most five points, one clause each, no bullet words |
| 28 | a question, then barge in inside the reply's first second with a different question | plain | 2 | the second answer does not treat the first reply as heard |
| 29 | say "mm-hmm" while the agent is mid-reply | silent (nothing asked) | — | recorded, not gated: the agent stops and does not resume (accepted limit); note how it feels |
| 30 | a garbled or half-heard instruction | plain | 2 | "say that again?" in some form; never "Hmm." |
| 31 | a real request, immediately after row 19's silence | answer | per row | the reply uses the earlier turn too if it was part of the thought, and ignores it if not |
| 32 | an edit request on which the agent narrates first ("I'll add that…") | affirm | 1 | the narration, a pause of about a second, then the confirmation; neither is dropped |
| 33 | after the agent checks its understanding ("…is that right?"): "Yeah." | affirm | 1 | five words or fewer, not a new paragraph |
| 34 | "Hi." mid-session | affirm | 1 | a short greeting back |
| 35 | thinking aloud about a plan, several sentences, nothing asked | silent (nothing asked) | — | nothing spoken |
| 36 | answer a question the agent asked, with new information | answer | 2 | one or two sentences back |

Also run once with each switch off in turn (rows 3, 6, 19 and 21 are
enough): with a switch off, its outcomes must not occur and the agent
must behave as it did before.

## Acceptance bar

- **Wrongly silent** (a finished request that got nothing) and
  **wrongly replying** (a reply into a pause): counted per run from the
  `turn.check` log lines and the owner's notes. The owner prefers the
  first mistake to the second; the thresholds are tuned until the
  balance feels right, and the final values are recorded here.
- **An edit request always edits.** No outcome takes the tools away,
  so "I can't edit right now" in any form fails the run.
- **Wrongly instructed** (the edit-and-speak instruction on a turn that
  asked for no edit; row 23 is the shape): nothing is written. Any edit
  on a turn that asked for none fails the run.
- Rows 3, 16, 18 and 19: no audio from the agent, and the indicator is
  not left on Thinking.
- Row 21: both halves, every time it is tried.
- No reply to a mode-2 row runs past two sentences, the list rows
  excepted. Record the sentence count per row.
- No closing reply on a mode-1 row runs past five words (narration
  before a tool call is not counted). Record the words.
- A thinking-aloud turn that reaches the model (row 35, when the
  checks miss it) gets a few words, not commentary. Record which
  happened: silence, a few words, or more.
- Wherever two steps of a turn both spoke (row 32 and any other), the
  pause between them is audible, about a second, and nothing the agent
  wrote was left unspoken.
- No turn's transcript contains "Hmm." or "Mm-hmm." from the agent
  (both switches on; with a switch off, today's rule is back by
  design, FR-73).
- Row 28: the context after the cut holds only the reply's first
  sentence, marked as cut (`context.playback_cut` in the logs).
- Check response time on every turn, read from the `turn.check` log
  lines (silent turns write those too; the `check` stage key exists
  only on turns that spoke): record p50 and the worst; count
  `turn.check_bypassed` lines. A bypass is not a failure of the run,
  but its count is recorded, and so is that turn's end-of-speech →
  first audio on its own line: it carries the whole ceiling.
- End-of-speech → first audio on the answer rows,
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

### Label review with the owner — 2026-11-14

Settled one case at a time. The rulings, which FR-62 now carries as the
definition of SPEAK's three options:

- Thinking aloud with nothing asked: nothing expected (silence).
- Answering a question the agent asked, with new information: an
  answer is expected.
- A "yeah" that only confirms the agent understood: a few words
  (affirm), not silence.
- A greeting to the agent: a few words back.
- A plain edit request: a few words of confirmation.
- The six real requests run 1 silenced (rows 33, 34, 40, 54, 77, 86)
  were all finished: the labels stand, the model was wrong.

SPEAK's labels became three values (nothing 15, affirm 18, answer 44,
unsure 25). The same session changed the design: mode 1 is "affirm",
SPEAK is a three-way choice, and a one-second pause between spoken
steps replaces the rule that dropped "Done.".

### Part 1, run 2 — 2026-11-14, same machine, reviewed labels

DONE reworded for speech-recogniser text; SPEAK asked as a three-way
choice; WRITE unchanged. 102 turns, no errors.

**Response time:** p50 115 ms, p95 159 ms, one request of 102 over a
second (1,130 ms).

**DONE** (62 real requests, 13 mid-thought turns):

| Stay silent when 1 − p_done ≥ | real requests silenced | mid-thought turns answered |
|---|---|---|
| 0.4 | 2 | 2 |
| 0.5 | 1 | 3 |
| 0.6 | 1 | 4 |
| 0.7 | 1 | 4 |
| 0.8 | 0 | 5 |

The rewording did what it was for: five of the six rough-speech
requests are no longer silenced (the one left, row 40, has a garbled
ending). It cost some mid-thought catches: the fragments now answered
(rows 24, 30, 35, 100) read as short complete phrases. Run 1's best
point silenced 5 real requests to miss 1 mid-thought turn; run 2 at 0.4
silences 2 and misses 2.

**WRITE:** as in run 1. At 0.8, no edit request of 16 is withheld and
59 of 63 no-edit turns are.

**SPEAK**, the owner's label against the model's choice:

| Labelled | nothing | affirm | answer |
|---|---|---|---|
| nothing (15) | 8 | 0 | 7 |
| affirm (18) | 0 | 14 | 4 |
| answer (44) | 0 | 1 | 43 |

The three-way choice fixes what run 1 could not do: plain edit
requests land on affirm (13 of 14) and listening sounds on nothing (all
8). Two groups are still judged as wanting an answer: every
thinking-aloud statement (7 of 7), and the bare confirming "yeah" (3 of
3), which the option text invites by also listing "answered a question
the assistant asked".

**Whole outcome, at the knob table's starting values** (mid-thought
0.5, withhold 0.8, nothing 0.5, affirm 0.5, edit-and-speak 0.6): 65 of
the 87 labelled turns, the 13 mid-thought turns included, get the
outcome FR-63 would select for their labels. The 22 that do not, by
kind:

- thinking aloud answered instead of left alone: 6 (a normal answer in
  this run's terms; with FR-72's fallback in the prompt, a few words);
- a confirming "yeah" answered at summary length instead of in a few
  words: 3;
- a mid-thought turn answered: 3 of the 13;
- "Hold on.", "I'm thinking." and one statement silenced as mid-thought
  instead of as nothing asked: 3 (the same result for the user);
- a spoken request that kept its write tools: 2 (today's behaviour);
- the edit-and-speak instruction on a turn that did not ask for both:
  2, one plain edit and one spoken request (rows 51, 59). This is the
  one mistake that could cause an unwanted write;
- an edit-plus-ideas turn left plain: 1 of the 2 in the set (row 75,
  p_write 0.54). At 0.5 it would be caught, at the price of one more
  wrong instruction (row 87);
- one real request silenced (row 40); one answer cut to a few words
  (row 99).
- No edit request lost its tools.

(An earlier version of this section gave 57 of 74 at mid-thought 0.6
and edit-and-speak 0.5, and left the mid-thought turns out of the
count. The figures above replace it.)

**Haiku reference: dropped as a yardstick for now.** With its raw
replies kept, it answered NO to "did the user ask for an edit" on plain
edit requests (11 of 16) and silenced 9 real requests, at a median of
1,059 ms. Whether that is Haiku or the harness (three parallel calls
through one client) is not established, and it is not worth
establishing before the build: Jev clears the speed the design needs by
a factor of eight.

**Findings.**

1. The design's two assumptions hold on this set: Jev answers in about
   an eighth of a second, and it is right about the turn's outcome on
   three turns in four with first-draft wording, never taking the edit
   tools from a turn that needed them.
2. The remaining misses are wording, not thresholds: thinking aloud
   read as wanting an answer, and the confirming "yeah". Both go in the
   next wording pass, with the caution that 100 turns is a small set to
   tune against.
3. Mid-thought has 13 examples and edit-plus-ideas has 2. Neither
   threshold should be trusted from this file; the live run sets them.

### Build checks — 2026-11-14, development machine, branch `feature/turn-checks`

Run while §4.15 was being built, against the live providers.

**The request shape FR-65 depends on is accepted.** `scripts/smoke_llm.py`
gained a case whose history calls `edit_document` while the request
declares only a read tool, with the turn's instruction as a trailing
system message: Claude Sonnet 5.5 accepts it.

**That same case caught a wording bug.** With the first wording of the
talk-only instruction the model retracted the earlier, real edit ("I
need to correct my earlier reply… milk has not been added"), on both of
two tries. Scoped to the user's latest message and told that earlier
edits stand, it answered the question and left the edit alone, 3 of 3.

**The one-turn instructions, on the real system prompt** (both checks
on), two tries each, all as intended:

| Case | What the model did |
|---|---|
| talk-only, the latest message asks for an edit | "I can't make that change right now, so eggs haven't been added to the list." No tool call |
| talk-only + affirm, a "Yeah." that confirms | "Okay." |
| affirm, a plain edit request | called `edit_document`, said nothing before it |
| edit-and-speak on a turn that asked for no edit | answered in one sentence; no edit |
| thinking aloud that reached the model | "Sounds good." |

**The checker through the factory** (`make_turn_checker`, the real
questions, evidence builder and outcome rules;
`scripts/bench_turn_checks.py`):

- Response time, 36 scripted requests on one warm connection: p50 74 ms,
  p95 99 ms, max 300 ms, none over a second. (The earlier runs opened a
  connection per request; the app shares one.) From the development
  machine: the VM figure is still owed.
- Accuracy on the labelled set, at the knob table's values: 64 of 87
  turns get the outcome their labels call for (run 2's scratch script
  gave 65; the evidence text differs by a line). Wrongly silent: 1
  (row 40). Wrongly withheld: 0. Wrongly replying: 9 — the two short
  fragments and the thinking-aloud statements, which now close with a
  few words when they reach the model. Wrongly instructed: 5 (rows 33,
  51, 54, 59, 100), three of them plain edit requests in rough speech
  that SPEAK read as wanting an answer; the instruction's own wording
  keeps that from causing an edit nobody asked for.

Part 2's response time from the VM and all of part 3 remain to run.

### Part 3, run 1 — owner, local dev server, 2026-11-14 (session `5cf0c0e2…`)

On commit `add3129`, both checks on. 31 turns, rows 1 to 20 of the
spoken script; it ended there because the voice provider ran out of
credits. 85 trace rows; check response time 111 to 340 ms, no bypass.

| Row | What happened | Cause | Fix |
|---|---|---|---|
| 3 | "…Probably" arrived as its own one-word turn, twice, and got "Got it." and "Okay." | DONE scored the bare word 0.53 and 0.76 | none yet: better than the advice it used to get, not the silence wanted. Carried |
| 8 | "What am I missing?" got four sentences ending in an unasked "Also… worth confirming" | the answer mode's bound lives in the prompt only, and did not hold (also 3 sentences on rows 2 and 20) | FR-66 gains an **answer** instruction on the turn. Re-probed live: two sentences, twice |
| 10 | the rename got silence | speech recognition delivered "Receiving the document to late weekend plan"; DONE scored it 0.44, just inside the 0.5 line | `CHECK_MID_THOUGHT_MIN` 0.5 → 0.6. Every real mid-thought in the session scored 0.32 or lower |
| 14 | "Hold on" had to be said four times; "continue" got "Okay, holding" | (a) the server registered no speech for 29 s of playback: the microphone path, not this change. (b) "continue" was transcribed as "Good evening", and the model answered the earlier, unanswered "Hold on" | (a) none here: roadmap feature 8. (b) the prompt now says to answer the newest message |
| 15 | "Stop" had to be said three times | same as 14(a): 9 s before speech registered; all three arrived as one turn | none here |
| 16 | after 'Did you mean "put the first aid kit under gear"?' and "Yes.", the agent said "Document editing is off for this reply" | WRITE scored the "Yes." 0.05, so the tools were withheld; and the instruction's wording was repeated aloud | WRITE now includes agreeing to an offered change (0.06 → 0.93 on this turn, live); the instruction never names itself and asks the user to say it again |
| 20 | stuck on Thinking for 20 s and on every turn after | Cartesia returned `quota_exceeded` from 05:45:59 UTC: replies were generated and could not be spoken | owner tops up. A voice-provider failure showing as endless Thinking is older than this change, and is not fixed here |

Worked as intended: the mid-thought silences on "After that," (twice),
"I said" and "Can you put"; the nothing-asked silences on "Okay.",
"Right. Right." and "Stop. Stop. Stop."; the create, the edits and the
restructure, each closed in a word ("Done.", "Added.", "Fixed.",
"Renamed."); the playback cut on rows 14 and 15 once the interruption
registered.

Not reached: rows 21 to 36. The run is repeated from the top on the
fixed build once the voice provider has credits.

**What the fixes cost on the labelled set** (`bench_turn_checks.py
--accuracy`, same 102 turns, after the fixes):

- The reworded WRITE question withholds the edit tools less often: on
  47 of the 63 turns that asked for no edit, down from 59. It still
  withholds them from none of the 16 that did. The turns it no longer
  withholds keep their tools as they did before the checks existed,
  which is the lean FR-63 chose. Two "Great."-type turns now get a
  short closing word where they got silence, because a turn that keeps
  its tools is never silent.
- A tighter wording was tried ("saying yes to the assistant's question
  about making a specific change counts; a yes to any other question
  does not"). It kept 60 of 63, and scored the first-aid-kit "Yes." at
  0.17, which is still withheld. Rejected: the wording has to fix the
  turn it was written for with room to spare (the chosen one scores it
  0.91 to 0.93).
- With the mid-thought line at 0.6: one real request still silenced
  (row 40), three mid-thought fragments answered (rows 24, 30, 35).

### Design change after run 1 — owner, 2026-11-14

The no-edit verdict no longer touches the tools and no instruction
mentions editing (FR-63, decision 4; FR-65 removed). Run 1's row 16 is
why: a wrong verdict on a "Yes." that confirmed an edit left the turn
unable to edit. Before removing it, a softer form was probed live with
every tool available and one line saying no edit seemed to be asked,
three tries per case:

| Case | Result |
|---|---|
| "Yes." confirming an edit, wrongly steered | edited, 3 of 3 |
| "Sure." to an offered edit, wrongly steered | edited, 3 of 3 |
| a plain edit request, wrongly steered | edited, 3 of 3 — and said the conflict aloud each time ("The note says not to edit, but you've clearly asked…"), even when told never to mention the note |
| opinion and "tell me what you have" questions, rightly steered | no edit, 3 of 3 each |
| the same question with no steer at all | no edit, 3 of 3 |

So a line about edits is audible when it is wrong and unnecessary when
it is right. What a no-edit verdict now adds is only the answer
instruction's length bound.

### Before run 2: Pipecat 1.3 → 1.16 is in the same build

Run 2 is on the upgraded framework, so it tests two things at once.
If something sounds wrong, switch both checks off (`TURN_CHECK_DONE=0`,
`TURN_CHECK_MODE=0`) and repeat the row: if it still happens, it is the
framework and not the checks. Listen for, beyond the script's rows:

- barge-in while the agent is speaking (rows 14, 15, 28, 29), and a
  second burst of speech right after the first;
- what the agent believes it said after a cut (FR-61), now on 1.16's
  word-by-word releases;
- the pause between a narration and its closing word (row 32);
- `turn.straggler_dropped` warnings in the backend log;
- with `TTS_PROVIDER=assemblyai`: the first run of that voice at all.
