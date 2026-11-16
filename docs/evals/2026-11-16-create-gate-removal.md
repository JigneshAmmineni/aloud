# Eval: the create gate is removed

**Written before the run (2026-11-16).** Covers the removal of the FR-60
create gate on `feature/turn-checks` (PR #28). `create_document` now runs
whenever the model calls it. Where a write goes (an existing document or
a new one) is the model's own choice.

## What changed

- `agent/gate.py` and the judge call in `agent/loop.py` are deleted. No
  second model is asked before a create, and no refusal text exists.
- The judge client, the `judge_model` setting and `JUDGE_MODEL` are gone.
  Claude Haiku is no longer called.
- `agent/prompts.py`: the system prompt no longer says a new document
  needs an explicit ask, and no longer says to ask where a note should go.
  It now says: write only when asked, choose the destination yourself (the
  existing document it belongs in, or a new one when none fits or the user
  asks for one), and do not ask where to put it.
- `agent/tools.py`: the `create_document` description drops "ONLY when
  the user explicitly asks for a document".
- Unchanged: "never volunteer a document", "a question or 'give me a
  summary' is answered out loud", the lookup tools, the turn checks.

## Why

Session `5dc89ade…` (2026-11-16): the user said "Can you write all this
down?" with two unrelated documents in the workspace. Sonnet called
`create_document` ("Camping Trip Plan") at once. The Haiku judge said NO
in 826 ms. The agent then said "The tool says to look for an existing
document first.", listed the documents, and asked "Do you want a new
document for this?". The document was created only after "Yes.".

The gate was built in October for Gemini Flash, which created stray
documents. Sonnet has not been run without it until now, so this eval is
also the first measurement of whether Sonnet has that habit.

## Scripted turns (owner, live voice session, local dev server)

Part A is the new behavior. Part B is the standing core set, run in the
same session so a regression shows up next to the change.

### Part A: where the write goes

Start a fresh session with nothing attached. Before it, make sure the
workspace holds at least two unrelated documents from earlier sessions.

| # | Spoken turn | Required outcome |
|---|---|---|
| A1 | Talk through a small plan for three or four turns (a camping trip: dates, who is coming, gear). Then: "Can you write all this down?" | one new document with the plan; a short confirmation; no question, no "the tool says" |
| A2 | "Also note that I need to book the campsite by Friday." | lands in the document from A1; no new document; no question |
| A3 | "Add a packing list to that." | lands in the same document |
| A4 | "Write the budget down in a new document: two hundred for the site, a hundred for food." | a second, separate document, even though the camping one is right there |
| A5 | Change the subject for two turns (a birthday gift for a sister). Then: "Jot that down." | the model's choice is recorded: a new document is the expected outcome; landing in the camping document is a finding |
| A6 | "Put the campsite deadline in my launch plan." (name a document from an earlier session that is not attached) | it finds that document and edits it; no new document |
| A7 | "What's in the camping plan so far?" | spoken answer; nothing written |
| A8 | "Give me a summary of everything we've covered." | spoken summary; no document created |

Then end the session, attach one existing document, start a new session,
and run:

| # | Spoken turn | Required outcome |
|---|---|---|
| A9 | Discuss the attached document for two turns. Then: "Note that down." | lands in the attached document; no new document |
| A10 | Say something unrelated to the attached document for two turns. Then: "Write that down." | recorded, not gated: new document or the attached one? Either way no question to the user |

### Part B: core set

| # | Spoken turn | Required outcome |
|---|---|---|
| B1 | (connect) | a few words of greeting |
| B2 | "Change the title to Lake Weekend Plan." | one rename; short confirmation; no step talk |
| B3 | "Remove the last item from the packing list." | quick edit; short confirmation |
| B4 | "What do you think of that plan?" | a spoken opinion; no write |
| B5 | Think aloud mid-sentence and trail off ("so maybe we could, um…") | the agent stays silent |
| B6 | "Hold on." | the agent stays silent |
| B7 | After the agent asks a yes or no question: "Yeah." | it acts on the yes |
| B8 | "Change the line that says the budget is ten thousand dollars to twenty thousand." (no such line) | one short sentence that it could not; no tool names |
| B9 | "Which of my documents mentions pricing?" | silence, then the answer |
| B10 | "Explain the whole plan in detail." then barge in with "wait", then "continue" | resumes from what was actually heard |

## Acceptance bar

- A1 to A4, A6 and A9: the write lands where the table says, with no
  clarifying question about where to put it.
- No turn's transcript contains "the tool says", "existing document
  first", or "do you want a new document".
- A7 and A8 create nothing.
- Stray documents are counted: any `create_document` call on a turn that
  did not ask for a new document and had a fitting document under
  discussion (A2, A3, A9 are the turns at risk). Zero is the bar. One or
  more reopens the question of a guard, and the first response is a
  prompt change, not a second model.
- A5 and A10 are recorded, not gated.
- Duplicates from documents the model cannot see are a known gap (a
  related document from an earlier session that is neither attached nor
  named by the user). Record any that happen. They are not a failure of
  this change.
- First audio on A1 is recorded from `turn_metrics` and compared with
  the same turn in session `5dc89ade…`, where the gate added a judge
  call, a listing step and an extra user turn.

## Results

Not run yet. The run needs the owner's voice on the local dev server.
Resume point: start the dev containers on the commit that removes the
gate, then Part A row A1.
