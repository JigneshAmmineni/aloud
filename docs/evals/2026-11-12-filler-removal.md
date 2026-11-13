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
Anything heard on `73be1d8` does not count as run 2.

Still to run: the same script again on the merged build, to hear the
confirmation rule and to exercise row 9's ask-then-yes path with a
trigger that is not an explicit ask (for example, with one document
open, "jot down my grocery list: eggs and milk").
