# A/B listening test — system prompt character (features 3.1 + 3.2)

**Date:** 2026-09-16 · **Branch under test:** `feature/system-instructions` ·
**Commits:** `a7ca015` (round-5 loop guards), `93d9e50` (3.1 prompt), `ff0bcf3` (3.2 repositioning)

The acceptance bar for the 3.1/3.2 PR (per ROADMAP §3.1): live A/B listening
against real speech, not just prompt-pin tests. This document is the test
plan and, once run, the record of results. Method in one line: **both
prompts face the same scripted moments, so the prompt is the only variable —
and each scenario runs twice per arm because single LLM samples mislead.**

- **Arm A** = `main` (the shipped prompt)
- **Arm B** = `feature/system-instructions` (the 3.1/3.2 prompt)
- Switch arms with `git checkout main` / `git checkout feature/system-instructions`
  — the dev backend hot-reloads; start a **fresh session** for every run.
- After listening, audit any session in cold print:
  `docker compose run --rm backend python scripts/show_trace.py <session_id>`
  (session ids: `SELECT session_id, MIN(ts) FROM llm_traces GROUP BY session_id ORDER BY 2 DESC;`)

---

## What changed

### Identity (3.2)

The product repositions from **"thinking partner"** (leads with sharp
questions and pressure-testing) to **"work assistant"**: your work becomes a
conversation with an assistant that does the tiny details — writing becomes
talking ideas through and reviewing the coherent write-up it produces;
questioning and pressure-testing remain as *invited* behavior, never the
selling point. The rename touches every purpose statement in the repo
(README, ROADMAP vision, REQUIREMENTS purpose, CLAUDE.md, frontend meta
description, reviewer context) — but the only piece the *model* sees, and
therefore the only piece this test can hear, is the system prompt.

### The four normative rules (3.1) — what B must do that A doesn't

| # | Rule | The behavior it fixes (observed live, session `dc99a7f0`/`e96cd10b`) |
|---|---|---|
| 1 | **Minimal-acknowledgment** — during a brain dump, "hmm" / "that makes sense" is a complete reply; follow the thread, don't add to it | A counter-questioned the user's first idea immediately ("What's the main problem you're hoping to solve?") |
| 2 | **Pause** — a thinking pause is not an invitation; never filled with questions or suggestions, no steering onto new topics | A pushed "What's next on your mind for the app?" after the user said they were done |
| 3 | **Direct address** — being spoken TO (question, greeting, instruction) always gets a real, direct answer; an acknowledgment is never a substitute | "How are you" got "hm" (the prompt gave the model a brain-dump/respond binary with no direct-address discriminator) |
| 4 | **Invited challenge** — pressure-testing and hole-poking happen when asked, sharply; never as the default posture | Challenging was the default; the risk in B is *overcorrection* — scenario 5 exists to catch a B that won't challenge even when asked |

Out of scope, deliberately: `FLUX_EOT_THRESHOLD` (turn detection) is
untouched — this pass changes only what the agent *says* when its turn
fires, not *when* turns fire.

### Prompt A (main) — verbatim

```
You are Aloud, a thinking partner for people who work through ideas by talking
out loud. The user is speaking to you. Help them brainstorm, pressure-test
plans, and untangle messy thoughts. Ask sharp questions that surface
assumptions and gaps.

Your replies are read aloud by a text-to-speech voice. Speak in short,
natural, conversational sentences. Do not use markdown, headings, bullet
points, numbered lists, or emoji. Ask at most one question at a time. Try not
to stack questions, and never volunteer lists of suggestions. Keep replies
brief; this is a conversation, not a lecture. Remember that you don't HAVE to
ask a question at every turn. When the user is just thinking out loud and just
trying to get all his thoughts out, it is okay to use filler phrases like
"hmm" or "that's interesting" until the user prompts you to give your
thoughts. Try to minimize interrupting the user's flow/train-of-thought when
they are on a roll. Only ask a question or make a suggestion when you
genuinely have one.

[tool and greeting paragraphs — unchanged between arms]
```

### Prompt B (branch) — verbatim

```
You are Aloud, a voice work assistant for people who think and work by
talking out loud. The user speaks; you handle the small details and give
their words structure. You are an intelligent, productive partner in the
conversation — never its leader. The user drives; you keep up, keep track,
and make yourself useful.

Your default posture is listening. When the user is thinking out loud,
brain-dumping, or on a roll, stay out of their way: a minimal acknowledgment
— "hmm", "right", "that makes sense", "go on" — is a complete and good
reply, and following their thread matters more than adding to it. A pause
is not an invitation: never fill the user's thinking pauses with questions
or suggestions, and do not steer the conversation onto new topics.

But when the user speaks TO you — asks you a question, greets you, gives
you an instruction — always answer directly and completely. A minimal
acknowledgment is never a substitute for a real answer when you are
spoken to.

Challenging the user's thinking is something you do when invited, not by
default. If they ask you to poke holes, pressure-test a plan, or give your
honest take, do it sharply and concretely. Otherwise, offer a question or
suggestion only when you genuinely have one that serves their thread — at
most one question at a time, never stacked, and never a volunteered list
of suggestions.

Your replies are read aloud by a text-to-speech voice. Speak in short,
natural, conversational sentences. Do not use markdown, headings, bullet
points, numbered lists, or emoji. Keep replies brief; this is a
conversation, not a lecture.

[tool and greeting paragraphs — unchanged between arms, except the closing
invitation: "start thinking out loud" → "start talking through whatever
they're working on"]
```

Each rule is also pinned by a prompt-contract test in
`backend/tests/test_prompts.py`, so a future rewrite cannot silently drop
one — this listening test judges whether the words *work*; the pins only
guarantee they *exist*.

---

## Protocol

1. Run all seven scenarios on one arm, then all seven on the other, then
   repeat both (2 runs per scenario per arm; alternate which arm goes first
   between run 1 and run 2 to spread any warm-up bias).
2. Fresh session per scenario. Read the scripts as written — the exact
   wording matters, especially scenario 6, which must *not* contain an
   invitation to critique.
3. Judge each run against the scenario's pass line while listening; note
   surprises verbatim if short. Afterwards, spot-check traces with
   `show_trace.py` for anything your ear was generous to.

## Scenarios

**S1 — Direct address (rule 3).** Say: *"Hey! How are you today?"*
→ **Pass:** a real, direct answer to the question. **Fail:** "hm" or any
bare acknowledgment.

**S2 — Brain dump (rule 1).** Read in a natural rambling voice, letting the
agent take turns wherever it wants: *"Okay so I've been thinking about the
restaurant app again. I think the core loop is you log a place right after
you eat there, like a two-tap thing... and the ranking isn't stars, it's
comparisons, like — is this better than the last place, and it builds the
list from that. And maybe the feed is just your friends' recent rankings.
I don't know, I'm just getting it all out."*
→ **Pass:** every agent turn during the dump is a minimal acknowledgment or
a short follow of the thread; zero questions, zero suggestions, zero
redirections. **Fail:** any counter-question or "have you considered…".

**S3 — Pause (rule 2).** Say: *"So the thing I keep going back and forth on
is pricing…"* — then stop and stay silent until the agent takes a turn.
→ **Pass:** whatever fires is minimal ("mm-hm", "take your time", silence-
adjacent) and does NOT ask a question, suggest an option, or change topic.
**Fail:** "What pricing models are you considering?" or any steer.

**S4 — No steering at a close (rule 2).** After any short exchange, say:
*"Okay, I'm done with that topic."*
→ **Pass:** brief closing acknowledgment; the agent does not propose what
to discuss next. **Fail:** "What's next on your mind?" (the live-test
behavior).

**S5 — Invited challenge (rule 4, overcorrection check).** Say: *"Here's my
plan: I'll quit my job next month and fund the bakery entirely on credit
cards until it's profitable. Poke holes in this — be honest."*
→ **Pass:** a sharp, concrete critique actually lands (named risks, not
hedging). **Fail:** a minimal acknowledgment, or mush — B being *too*
passive here is a real bug in the rule wording.

**S6 — Un-invited flaw (rule 4's boundary).** Say, with no request for
feedback: *"I've decided I'll fund the bakery on my credit cards. Anyway,
the next thing I need is an oven."*
→ **Pass (B):** the agent follows along (ack / stays with the oven thread)
without volunteering a critique of the credit-card plan. **Expected A
behavior:** challenges it. *Judgment call to note: if B softly flags mortal
danger, decide whether you actually want that — the current rule says it
shouldn't.*

**S7 — Regression: tools + greeting.** Fresh session: listen to the
greeting (short, inviting, no tool use), then say: *"Write this up as a
summary: the bakery plan is sourdough focus, market stall first year,
storefront in year two."* Then: *"Add a bullet that the oven budget is five
thousand dollars."*
→ **Pass:** spoken acknowledgment → artifact appears → one-sentence
confirmation; edit updates the panel; behavior identical across arms.

## Judging sheet

| Scenario | A run 1 | A run 2 | B run 1 | B run 2 | Verdict |
|---|---|---|---|---|---|
| S1 direct address | | | | | |
| S2 brain dump | | | | | |
| S3 pause | | | | | |
| S4 no steering | | | | | |
| S5 invited challenge | | | | | |
| S6 un-invited flaw | | | | | |
| S7 tools + greeting | | | | | |

---

## Results

### Iteration 1 — arm B only (2026-09-16, ~08:40–09:10 UTC)

Arm A deferred by decision: B iterates until it behaves as intended, then
the comparative run happens. Verdicts below combine listening notes with
trace audit (`show_trace.py`, sessions `587f42ce…`, `d9d8b6c9…`,
`651dd743…`, `549dc72d…`, `6952b7ab…`, `ff39bf64…`, `4b588a16…`,
`1791a3be…`).

| Scenario | Listening verdict | Trace audit |
|---|---|---|
| S1 direct address | **pass** — "How are you?" got a real answer + question back | confirmed |
| S2 brain dump | acks right in kind but **too wordy**; **agent created an artifact UNPROMPTED** at the dump's end | confirmed: STT clipped "I'm just getting it all out" to *"getting out."* and the model invented a `create_artifact` — a real FR-7-style violation, prompt-level |
| S3 pause | **fail** — "Hmm, pricing. That makes sense. Go on." where a bare "Mm-hmm" (or nothing) was wanted; multi-word acks collide with the user resuming (barge-in cuts in only after several words — bad UX) | confirmed across three sessions |
| S4 no steering | **fail** — "alright, is there something else you'd like to discuss?" | (listening) |
| S5 invited challenge | *looked* like overcorrection ("right…", "that makes sense" to "poke holes, be honest") | **exonerated — STT never delivered the invite.** Four attempts arrived as *"pork horns in this"*, *"for calling centers"*, *"Pork wholesalers"*, *"Cook holds not plan"*. In the session where STT correctly heard **"stress test this plan"**, the model challenged immediately and concretely — rule 4 works when the words arrive |
| S5b "tell me the plan so far" | **fail** — created an artifact + "I've put a summary on the screen" instead of speaking the plan | confirmed |
| S6 un-invited flaw | **pass on substance** (no unsolicited critique) but parroted the user's full sentence back | confirmed |
| S7 tools | edit worked; "add a bullet" first got "Right." | **partially exonerated:** STT heard *"I **had** a bullet…"* — a statement. The retry ("Add the bold." — also garbled but imperative) was understood from context and executed. Confirmations still too long |
| Greeting | **fail** — "Hi there. I'm ready when you are, so feel free to start talking…" every session; wanted "Hey" |confirmed, near-identical wording all 8 sessions |

**Cross-cutting finding — STT noise is a first-class factor.** Deepgram
Flux garbled meaning-bearing phrases repeatedly: *poke holes* (4/4
attempts), *core loop → "code loop"*, *ranking **isn't** stars → "Ranking
**is in** stars"* (meaning inverted), *add → "had"*, *fund the bakery →
"fondo bakery"*, *write this up → "Wrap this up"* (survivable). The prompt
can't fix STT, but it can stop the model treating garble as content —
iteration 2 adds exactly that rule. Scenario scripts stay unchanged so
the garble-handling path gets exercised deliberately.

### Iteration 2 — prompt v2 changes (commit on this branch)

1. **Global brevity economy:** "say no more than utility requires"; never
   restate/parrot the user; agreement is a word or two.
2. **Brain-dump acks exclusively "Hmm." / "Mm-hmm."** — nothing longer
   (was: an open set incl. "that makes sense", "go on").
3. **Greeting: a few words at most** — "Hey.", "Hey, what's up?",
   "Hey {name}." (the FR-24 preferred name now rides the greeting trigger
   — small code change in `users_repo`/`companion`/`loop`); no offers, no
   "I'm ready", no invitations.
4. **Topic close:** acknowledge in a word or two, never ask what's next.
5. **Artifacts explicit-only:** never volunteered, never the answer to a
   question — "tell me the plan" gets a SPOKEN recap.
6. **STT-garble rule:** a garbled instruction/question → briefly ask to
   repeat, never guess; garble mid-dump → just "Hmm."
7. **Tool confirmations:** a few words ("Done — it's on your screen.").

Design note recorded for later: true *silence* on a mid-dump turn is not
prompt-expressible today — an empty generation triggers the FR-42
empty-step fallback (it would SPEAK a snag line). If "say nothing" is
wanted, the loop needs a deliberate quiet-turn mechanism (e.g., a
sentinel the loop swallows); parked unless single-syllable acks still
feel like too much. The barge-in overlap complaint (agent gets words out
as the user resumes) is mitigated by shorter acks; the residual is
turn-detection timing, out of scope by decision.

### Iteration 2 — listening results

*(not run)*

### Status: PAUSED (2026-10-08) — v2 ships provisionally

Decision by the product owner: the prompt-tuning cycle is paused and
prompt v2 ships with the 3.1/3.2 PR on iteration 1's evidence — every v2
change targets a failure observed live (and arm A's prompt demonstrably
has the same failures, worse). This is UX fine-tuning, not a system
blocker; the comparative A/B (iteration 2 on v2, then arm A runs, then
the filled judging sheet) resumes from this document when picked back up.
The scenario scripts above stay valid, including the deliberately
unchanged "poke holes" wording that exercises the STT-garble path.
