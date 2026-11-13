# Eval: the filler line is removed

**Written before the run (2026-11-12).** Covers PR #25: the loop no longer
speaks "One moment." (or any line of its own) before tool work, and the
system prompt no longer tells the model that the system speaks a filler
for it. A turn that goes straight to a tool call is silent until the
agent's real reply.

## What changed

- `agent/loop.py`: the filler lines, their delayed-fire task and the
  playing-or-queued state are gone. A step that spoke nothing before its
  first tool call stamps `step.N.tool_first` on the turn's metrics row.
- `agent/prompts.py`: the sentence "if a wait is worth marking, the system
  speaks a brief filler for you" is removed from the work-silently rule.
- `agent/context.py`: the create gate's judge reads a tool-call step's
  text as it is (nothing to strip).
- Added after run 1 (see Results): one acknowledgment per piece of work.
  In a step that follows speech in the same turn, a leading bare
  "Done." is not spoken.

## Scripted turns (owner, live voice session, local dev server)

Fresh session, no documents attached. The same session also covers the
open rows of `2026-11-12-anthropic-llm-swap.md` (A–C) and the two
2026-10-11 scripts.

| # | Spoken turn | Required outcome |
|---|---|---|
| 1 | (connect) | a few words of greeting |
| 2 | "Make me a new document with a launch plan for a recipe app." | one new document; silence, then a short confirmation |
| 3 | "Give me a summary of this document." | spoken summary; nothing written |
| 4 | "Note down that the launch date is March first." | lands in the open document without asking; "Done." |
| 5 | "We need to separate the app into server side and client side. Create two lists. And give me your ideas for what could be client side and server side." | both halves: the lists land in the document AND the ideas are spoken |
| 6 | "What do you think of that split?" | a spoken opinion; no write |
| 7 | "Change the title to add App before Launch Plan." | one rename; "Done."; no step talk |
| 8 | "Remove the last item from the client side list." | quick edit, short confirmation |
| 9 | "Put my hiring ideas somewhere separate: a designer and two engineers." then answer its question with "Yeah." | it asks whether to start a new document; after "Yeah." the document is created |
| 10 | "Actually scrap that, let's talk about pricing." | no tool runs |
| 11 | "Change the line that says the budget is ten thousand dollars to twenty thousand." | one short sentence that it could not; no tool names |
| 12 | "Which of my documents mentions pricing?" | silence, then the answer |
| 13 | "Explain the whole launch plan in detail." then barge in with "wait", then "continue" | resumes from what was actually heard |
| 14 | any question, then "and also…" with a second question before the reply | both answered; the session keeps working afterwards |

## Acceptance bar

- No turn's transcript contains a canned wait line ("One moment.", "Let
  me get that.", "Just a second.", "On it.").
- The silence on tool turns (2, 4, 7, 8, 12) is tolerable to the owner
  for ordinary edits. Record the measured end-of-speech → first audio for
  each from `turn_metrics`, and confirm those rows carry
  `step.N.tool_first`.
- Row 9: the create succeeds after "Yeah.", and the agent never says
  "gate".
- From run 2 on: no "Done." directly after a sentence in which the
  agent said what it was doing; a turn that said nothing else still
  ends with a short confirmation. Listen for the other side of that
  too: a turn whose last audio is the narration ("I'll add that to
  the document.") with nothing after the work lands. Is that enough
  of an acknowledgment?
- Narration before tool calls is recorded, not gated: is there more or
  less of it than in the 2026-11-12 session ("I'll add that to the
  document")? More is a finding for the decision-gates PR.
- Reply length on rows 5 and 13 is recorded, not gated (the bound ships
  with the answer modes).

## Results

### Run 1 — owner, local dev server, 2026-11-12 (session `19f491c2…`)

Run on the filler-removal commit, BEFORE the confirmation rule below
existed. 28 turns, 42 LLM calls. Speech recognition garbled several
scripted lines ("Note down" arrived as "No doubt", "two lists" as "tool
list", "hiring" as "hiding"), so some rows ran on different words than
written; outcomes are judged against what was actually heard.

| # | Outcome |
|---|---|
| 1 | PASS. "Hey." |
| 2 | PASS on the write (judge YES, 815ms). Spoke "I'll create that now, though I don't have details from you yet…" then "Done." |
| 3 | PASS. Spoken summary, nothing written. Six sentences (146 tokens): longer than the two or three wanted. |
| 4 | PASS. Read "No doubt that launch date is March first" correctly, edited, "…I'll add that to the document." then "Done." |
| 5 | HALF. The lists landed in the document. The ideas were NOT spoken: it said "Done. I split it into client side, service side, and a tool list, with my own suggestions for each." Same miss as the Gemini result this row was written for. |
| 6 | PASS. Spoken opinion, no write. |
| 7 | VOID (script error). The title already read "Recipe App Launch Plan", so there was nothing to change; the agent said so and asked. |
| 8 | PASS. Named the item, removed it, "Done." |
| 9 | NOT EXERCISED. The agent did not ask; it created the document directly, and the judge allowed it (YES, 921ms) because the user said "somewhere separate", which is an explicit ask for a separate document. The ask-then-yes path needs a different trigger. It also asked "Is 'hiding' maybe 'hiring'?" and acted in the same breath without waiting. Never said "gate". |
| 10 | PASS. No tool ran. |
| 11 | PASS. Searched (three searches in one step), said it could not find the line, offered the nearest thing. More than one sentence. |
| 12 | PASS. Silent search, then the answer. |
| 13 | PASS. Cut about 16 seconds into a long explanation, then "Continue." resumed at pre-launch instead of from the top. |
| 14 | PASS. The first question's reply was cancelled before anything was spoken, both user messages went to the model back to back, and the one reply answered all three parts. The session kept working afterwards. |

Against the acceptance bar:

- **No canned wait line: PASS.** None in the transcript, and no filler,
  fallback or empty-step event in the backend log.
- **Silence on tool turns: recorded.** End of speech to first audio on
  tool turns: 5019, 2653, 5461, 2454, 1660, 2236, 3024, 3427, 2302 ms.
  Plain turns: 1530–2051 ms. The two tool-first turns (silent before the
  call) were 3024 and 3427 ms. `step.N.tool_first` was added after this
  run, so those rows do not carry it.
- **Narration: 7 of 9 tool turns** spoke a sentence before the call ("I'll
  add that to the document.", "I'll search for that line first."). The
  work-silently rule still does not hold on this model.
- **Reply length:** the explanation in row 13 was 543 tokens, its
  continuation 438; row 14's answer 310.

**Finding 1 — "Done." ran into the sentence before it. FIXED in this
PR.** The owner heard "I'll add that to the documentDone" on almost
every tool turn. Two causes. The model closes every tool turn with
"Done." (the prompt asks for a one-word confirmation) even when it has
just said what it was doing. And the narration is not heard when it is
generated: a single sentence followed by a tool call sits in sentence
aggregation until the step ends, because nothing after it marks the
sentence as finished. So the narration is spoken AFTER the wait, with
"Done." 0.6 seconds behind it. That is also why the two slowest turns
(5019 and 5461 ms) were narrated ones: their first audio waited for the
whole tool call to be written.

First attempt, a prompt rule ("if you already spoke in this turn, end
without another word"): tested against the live model with the real
system prompt before any voice run, and it failed 3 of 3; the model still
wrote "Done." after its own narration. The fix is in the loop instead:
in a step that follows speech in the same turn, a leading bare
confirmation is not spoken and not recorded. A turn that said nothing
else keeps its "Done.". The held text is at most a few characters, so it
adds no delay. Unit-tested; not yet heard live.

**Finding 2 — narration plays after the wait, not during it. Carried.**
See the mechanism above. Flushing the pending sentence when the model's
text block ends (Claude signals that before it starts writing the tool
call) would put the narration at the start of the wait, where it would
do the job the filler was meant to do. Carried to the roadmap with the
tool-call-start event it shares a seam with.

**Finding 3 — row 5's ideas are written but not spoken. Carried** to the
answer modes (decision-gates PR): the model treats "create two lists and
give me your ideas" as one write.

Review round 2 then found two defects in that first version of the
rule (commit `73be1d8`, which the owner's local stack ran for a while
that evening): a blank line before a tool call counted as speech, so a
turn could end with no audio at all; and on chained edits the "Done."
in the middle was spoken while the last one was dropped. Both fixed:
the rule now applies only to the step directly after one that spoke.
Round 3 found the chained case still inverted when the turn began
silently (commit `32ad8b8`); the rule was then restated whole: a
"Done." is dropped when it is premature (its step goes on to call a
tool) or redundant (the step before it spoke). Run 2 should include
one turn that asks for two separate notes in one breath.
Anything heard on `73be1d8` does not count as run 2.

Run 2 below covers this.

Originally still to run: the same script again on the merged build, to hear the
confirmation rule and to exercise row 9's ask-then-yes path with a
trigger that is not an explicit ask (for example, with one document
open, "jot down my grocery list: eggs and milk").

### Run 2 — owner, local dev server, 2026-11-12 (session `cd514be0…`)

Run on commit `32ad8b8` (the round-2 form of the confirmation rule; the
one shape round 3 later fixed did not occur). 36 turns, 53 LLM calls. A
new script: 17 core rows meant to stay the same from eval to eval, then
6 rows for this PR's changes. Topic: a camping trip. Speech recognition
again changed several lines ("Poke holes in this plan" arrived as "Borg
Hall and Somersbourne", "Stop." as "Stroke.", "Add a packing list" as "I
had a packing list").

Core:

| # | Spoken turn | Outcome |
|---|---|---|
| 1 | (connect) | PASS. "Hey." |
| 2 | "What's a good temperature rating for a sleeping bag in spring?" | PASS. Direct answer, no document. Four sentences. |
| 3 | thinking aloud, trailing off ("…maybe two nights? Probably…") | HALF. "Mm-hmm." and "Hmm." to the first fragments, then on "Probably" it answered with advice ("Okay, two nights sounds manageable. A 20 to 30 degree bag would cover you.") while the owner was pausing to think. |
| 4 | "Create a new document called Camping Trip Plan with what I've said so far." | PASS. One document (judge YES, 966ms). Silent, then "Done." 5.4s to first audio. |
| 5 | "Add that we need to book the campsite by Friday." | PASS. Heard "cam sign", read it as campsite, edited without asking. |
| 6 | "Give me a quick summary of the plan." | PASS. Spoken, nothing written. Four sentences. |
| 7 | "Organize the plan into three sections: gear, food, and schedule." | PASS. Restructured. |
| 8 | "What am I missing?" | PASS. Spoken, no write. Five sentences. |
| 9 | "Poke holes in this plan." | VOID. Heard as two unknown names; it asked what they were. |
| 10 | "Rename the document to Lake Weekend Plan." | PASS. One rename, "Done." |
| 11 | "Which of my documents mention a budget?" | PASS. Silent search, then the answer. |
| 12 | "Change the line about the canoe rental to a kayak rental." | PASS. Two sentences, no tool names, offered to add it instead. |
| 13 | "Actually, never mind. Let's go back to the trip." | PASS. "Okay." No tool. |
| 14 | "Walk me through the whole plan in detail." / "Hold on." / "Okay. Continue." | PASS. Cut 25s in; the entry was trimmed to 63 of 108 words and it resumed at "Food". |
| 15 | "Stop." mid-reply | VOID. The reply had already ended, and "Stop" was heard as "Stroke", then "store". |
| 16 | "Can you put the…" | PASS. "Put the what? Go ahead." It did not guess. |
| 17 | "That's all for now." | PASS. "Okay." |

This PR's changes:

| # | Spoken turn | Outcome |
|---|---|---|
| 18 | "Add a packing list with a tent and a lantern." | PASS. (First heard as a statement; it asked, the owner repeated.) Silent edit, then "Done." alone. |
| 19 | "Remove the lantern." | PASS. Silent edit, "Done." |
| 20 | "Add sunscreen to the packing list, and also change the trip to three nights." | PASS. One read, two edits in one step, "Done." once at the end. |
| 21 | "Jot down an idea for my sister's birthday: a pottery class." | PASS. It asked "a new document, or added to the lake weekend plan?"; the owner asked for a new one; created (judge YES, 741ms). Never said "gate". |
| 22 | "What should we cook the first night?" then at once "And also, what about breakfast?" | FAIL. See finding 4. |
| 23 | "Read me the gear section." | As expected today: it described the section fully in its own words, not word for word. |

Against the acceptance bar:

- **No canned wait line: PASS.** No fallback or empty-step event either.
- **One acknowledgment per piece of work: PASS.** The two narrated tool
  turns (rows 5 and 7) ended without "Done." (`agent.confirmation_dropped`
  twice); the six silent ones ended with "Done." alone; the chained one
  said it once. The owner did not report a welded "Done." in this run.
- **Narration: 2 of 9 tool turns**, down from 7 of 9 in run 1. One
  session each, so this is an observation, not a trend.
- **Tool-first marker: PASS.** All eight turns whose first step went
  straight to a tool call carry `step.1.tool_first`. Their time to first
  audio: 5415, 2905, 3131, 3291, 3246, 2943, 5734, 3984 ms. Plain turns:
  1591–2566 ms.
- **Reply length, recorded:** rows 2, 6 and 8 ran four to five sentences.
- Tokens: 269,555 in / 3,425 out on Sonnet, 1,015 / 8 on Haiku.

**Finding 4 — a reply cut inside its first sentence is recorded as fully
heard. Carried.** In row 22 the dinner answer finished generating in 2.1s
and began to play; the owner started the breakfast question about a
second later, inside the first sentence. FR-61 learns what was heard one
sentence at a time, when the audio transport releases each sentence. None
had been released yet, and in that state it cannot tell "cut inside the
first sentence" from "the release signal is broken", so by design it
changes nothing (`fr61.signal_suspect`). The whole four-sentence answer
stayed in the context as heard. The model then answered only breakfast,
and when asked insisted it had already given the dinner suggestions. Row
14 worked because its cut came 25 seconds in, after several sentences had
been released. Fix carried to the roadmap: when sentences HAVE been
released earlier in the session (so the signal is known to work), a cut
with none released for this reply should keep only the first sentence,
marked as cut.

**Finding 5 — it answers a trailing thought. Carried** (row 3). This is
the keep-listening decision the decision-gates PR exists for.

**What the records do and do not hold.** The trace row holds everything
the model generated for a call. When a reply is cut while it is still
generating, the row is marked `interrupted` and holds what had been
generated by then. How much was HEARD is not in the trace: it shows only
in the context (the entry trimmed to the heard part, with a cut mark) and
in a log line (`context.playback_cut`, kept and total words). Nothing in
the database records heard against unheard.

Still open: rows 9 and 15 (voided by speech recognition both times they
were tried).

**Deferred — run 3, owner, on the merged build (now also live at
work-aloud.com, deployed 2026-11-13 as `f051bdb`).** Run 2 heard the
round-2 form of the confirmation rule; the shipped form (dropped when
premature or redundant) is unit-tested only. Resume point: the run-2
script, plus one turn that chains two edits from a silent first step
("add bug spray to the packing list and add a first aid kit to the gear
section") and one that narrates first. Listen for a "Done." in the
middle of the work, and for a turn that ends with nothing spoken.
