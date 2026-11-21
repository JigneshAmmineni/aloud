# Eval: replies default to one sentence, and tool work is silent

**Written before the run (2026-11-19).** Two findings of the live run in
`2026-11-16-create-gate-removal.md`, and the wording change that answers
them. Checked on frames (one model call per replay,
`backend/scripts/replay_frames.py`), not by a new voice session.

## The behaviour wanted (owner, 2026-11-19)

- When nothing is asked: silence or an affirm. This already holds.
- When a reply is asked for: one sentence by default, two at most when
  the second carries something the user needs. This includes a request
  for a summary.
- Longer only on an explicit ask: "in detail", "explain", "walk me
  through", "read that part out to me" or the like. Reading out is word
  for word.
- No narration before or between tool calls.
- No added model step and no check in the tool path: wording only.

## What the live run showed

- Summaries of 2 to 4 sentences and 57 to 80 words. Factual answers of 3
  to 4 sentences, each ending in an unasked offer ("Tell me the region
  and I can be more specific").
- Spoken before tool calls: "I'll find the camping plan first.", "Let me
  look for them.", "They want the whole document read word for word, so
  I'll pull the current version first."

Both rules are already in the prompt. The answer bound reads "one or two
sentences, ideally one", allows a five-point list, and sits beside a
system-prompt line that says "You may end by offering to go deeper"
while the turn's own instruction says to add no offer.

## The change under test

In `agent/prompts.py`, the system prompt's answer mode and the one-turn
answer instruction: one sentence by default with a stated condition for
a second, a word budget, no offer line, no five-point list, the explicit
ask kept as the only way to a long reply, and read-outs word for word.
The work-silently rule and the affirm instruction say outright that
nothing is spoken before or between tool calls.

## Frames and what they check

The 37 frames of the 2026-11-19 run, with these expectations added:

- **Answer frames** (1.01 to 1.03, 1.07, 1.08, 2.01, 2.02, 2.07, 2.08,
  3.03, 4.05, 4.09): at most 2 sentences and 40 words. 1.13 (an edit
  that cannot be made): at most 2 sentences.
- **Tool frames** (every frame whose first call must use a tool, except
  the edit-and-speak frame 1.12): nothing is spoken with the call.
- **Frames that must stay long:** 4.07 "Walk me through the whole plan
  in detail" (at least 60 words) and 4.13 "Explain the gear section in
  detail" (at least 40). 2.06 "Read me the packing list" must contain
  the list's items and nothing else.
- Every tool-decision expectation from the create-gate eval stays.

## Acceptance bar

- No frame that passed before the change fails after it on a tool
  decision.
- The answer frames and the tool frames pass in at least 2 of 3 runs
  each, and more of them pass 3 of 3 than before the change.
- 4.07, 4.13 and 2.06 pass 3 of 3: the change must not shorten what was
  explicitly asked for at length.
- Recorded, not gated: the median words of the answer frames before and
  after.

The owner's ear is still the last check: a live session after the frames
pass, listening for answers that are now too thin to be useful.

## Results

### Frames, 2026-11-19

**Reply length: better, and the long forms kept their length.** Words
spoken per run, 5 runs per frame, the committed wording against the new
one:

| Frame | Before | After |
|---|---|---|
| 1.07 "Give me a summary of the document." | 51 to 56 | 27 to 50 |
| 2.01 "Summarize this document for me." | 48 to 51 | 36 to 46 |
| 3.03 "Summarize my camping plan." | 62 to 80 | 34 to 40 |
| 1.08 "What do you think is missing from the plan?" | 31 to 52 | 28 to 45 |
| 4.05 "What time should we leave on Friday…?" | 40 to 59 | 34 to 42 |
| 4.09 "How cold does it get at the lake in December?" | 41 to 59 | 34 to 44 |
| 4.07 "Walk me through the whole plan in detail." | 150 to 158 | 162 to 187 |
| 4.13 "Explain the gear section in detail." | 98 to 135 | 144 to 185 |
| 2.06 "Read me the packing list." | 8 to 10 | 8 to 10 |

Before the change the summaries and answers were 3 or 4 sentences in
most runs. After it they are 1 or 2 in nearly all, at 30 to 45 words:
shorter by a quarter to a half, but still usually two sentences, not
one, and several runs sit just over the frames' 40-word bound. Passing
runs out of 5 on the six answer frames went from 1 of 30 to 18 of 30.
4.09 still closes with "Tell me where, and I'll be more specific" in
most runs, the one offer the wording did not remove.

**Narration before tool calls: not fixed by wording.** On the five
frames that narrated before the change (3.01, 3.02, 3.04, 4.02, 4.10)
the model stayed quiet in 1 of 15 runs before and 4 of 15 after. The
two lookups 3.01 and 3.04 are now quiet in 2 of 3; 3.02 ("I'll look for
them."), 4.02 and 4.10 still speak every time, and 2.09 spoke once
where it had not before. What it says is its own reasoning about where
something goes ("Gear has sections per person, so I'll read the
document to edit precisely."). Three wordings were tried (the system
prompt's rule, the affirm instruction, the answer instruction), and
none held.

**Tool decisions: unchanged.** No gated frame missed on which tool is
called or which document is edited, before or after. Frame 2.3 is
outside the gated set and edits in every replay, under either wording
(`2026-11-16-create-gate-removal.md`, "Against the bar"). Full set on the final
wording, 3 runs each: 26 of the 36 gated frames passed every run (24 before), and
every miss is length or narration.

**Against the bar.** Met: no tool-decision regression; 4.07, 4.13 and
2.06 pass every run; more answer frames pass than before. Not met: the
tool frames do not pass 2 of 3 on quiet work (3.02, 4.02, 4.10), and
1.08, 2.01, 4.05 and 4.09 do not pass every run on length.

### Owner's wording, 2026-11-20

The owner edited the wording by hand before it was committed: the
second sentence is for "something they asked for", the thirty words are
something to "try to keep", a summary does not "mention every single
thing from the document" and speaks "at a high level, using summary
words", a closing suggestion is ruled out with the offer, and reading
out word for word applies when asked to recite. That is the wording
that ships.

Full set on it, 3 runs each: 25 of the 36 gated frames passed every run, with no
miss on a tool decision. "Give me a summary of the document." (1.07)
came back as one 35-word sentence in all three runs, naming the
document's sections instead of listing their contents. The other answer
frames are where they were (36 to 51 words), the long forms kept their
length (93 to 172 words), and narration before tool calls is unchanged.
Frame 4.14 spoke before reading the plan in 2 of 3 runs; it had not in
the 3 runs before.

**Decision (owner, 2026-11-20):** nothing more for now. Reply length is
acceptable as it is, and narration stays accepted as in FR-43.

**Open for the owner.**
- Whether 30 to 45 words in two sentences is short enough. A lower
  stated budget is one more replay away.
- Narration. Wording did not move it. What is left is the trade
  recorded in FR-43 on 2026-11-14 (accept it), or not speaking text
  that arrives in a step that goes on to call a tool, which was
  rejected then.
- A live session, to hear whether the shorter answers are too thin.
