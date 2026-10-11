# Eval: voice behavior fixes — questions beat "Hmm", silent tool work, filler threshold, rename

**Written before the run (2026-10-11).** Covers the prompt and loop
changes motivated by the 2026-10-11 live sessions (traces in sessions
`dd21f2dd…` and `398e142d…`): "Hmm." answered a direct question twice, a
title change narrated four internal steps after two refused tool calls,
every edit closed with "Done — it's on your screen", and an instant edit
got "One moment." welded to its "Done".

## What changed

- Prompt: a turn containing a question or instruction is never a
  brain-dump turn ("Hmm." is never the reply to a question, even a
  misheard one); tool work is silent (no step narration, no tool names);
  confirmations are a word or two with no screen mention; small
  decisions (placement, phrasing) are made, not asked about.
- `edit_document` gains `rename` (title-only; the description now states
  the title is never in the content).
- The FR-43 filler arms at the tool round but speaks only after
  `FILLER_DELAY_MS` (800ms) of dead air — instant actions stay silent.
- Tool result notes no longer teach screen talk ("Created." / "Saved.").

## Scripted turns (owner, live voice session)

One document open and under discussion. Record what the agent said,
verbatim, for each.

| # | Spoken turn | Required outcome |
|---|---|---|
| 1 | a clear question, phrased oddly ("do you see the categorization options listed?") | a real answer — never "Hmm." |
| 2 | the same question with a word the STT will likely garble | an answer or "say that again?" — never "Hmm." |
| 3 | "change the title to add App before Core Loop" | ONE rename call, no read of the document, no narration; reply is "Done." or similar — no step talk, no screen mention |
| 4 | "this is supposed to be a mobile app — can you make that note in the document?" (casual, mid-flow) | the edit happens; placement chosen by the agent, no "where?" question, no "Hmm." |
| 5 | a small instant edit ("remove X from that list") | NO filler phrase; just the short confirmation, with a natural gap if anything precedes it |
| 6 | a slower operation (e.g., a search across documents) | filler IS spoken once the wait is real (~0.8s+), then the result |
| 7 | an edit that will fail (e.g., referencing text not in the doc) | one short sentence about what couldn't be done — no tool names, no internal mechanics, no apology chain |

## Acceptance bar

- Zero "Hmm." replies to turns 1–4.
- Turn 3 is a single tool call in the trace (`edit_document` mode
  `rename`), and the spoken reply contains no narration and no mention
  of the screen.
- Turn 5's transcript contains no FILLER_LINES entry; turn 6's does.
- No reply in the session contains "on your screen" or equivalent.
- Instrumentation intact: filler-led turns still stamp `step.N.filler`.

## Results

_(append after the owner's live run)_
