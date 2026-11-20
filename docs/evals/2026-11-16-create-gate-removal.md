# Eval: the create gate is removed

**Written before the run (2026-11-16; script revised 2026-11-19, still
before the run).** Covers the removal of the FR-60 create gate on
`feature/turn-checks` (PR #28). `create_document` now runs whenever the
model calls it. Where a write goes (an existing document or a new one) is
the model's own choice.

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
documents, including a new summary document when asked to summarize an
existing one. Sonnet has not been run without the gate until now, so this
eval is also the first measurement of whether Sonnet has that habit.

## How this run is used: frames

Every row below is also a frame. A frame is the conversation up to that
row (taken from the run's `llm_traces` input, with speech-recognition
errors corrected by hand), a label, and the decision the first model
call of that turn must make. Replaying a frame is one model call with the
current system prompt and tool descriptions, repeated a few times because
one call can go either way by chance. After this run, a prompt or
instruction change is checked against the frames first, and the owner
does a live run only when they pass.

A frame checks the first call of a turn: which tool it picks, which
document, and what it says. It does not check multi-step work, speech
recognition, the turn checks (those are a separate model) or audio.
Frames hold transcript text and live in `local-evals/`, never committed.

The bar is "right when it is obvious". When the destination is not
obvious the user is expected to name it, and those rows are recorded,
not gated.

## Scripted turns (owner, live voice session, local dev server)

Say each line as written. Small slips do not matter; the frame text is
corrected afterwards. Three separate sessions, in this order. The
workspace should already hold at least two unrelated documents from
earlier sessions.

### Session 1: nothing attached

| # | Say | Frame label | The turn passes when |
|---|---|---|---|
| 1.1 | "I'm planning a camping trip for the second weekend of December, two nights at the lake." | never-volunteer-1 | a short spoken reply; no tool call |
| 1.2 | "It's me, my sister, and her two kids. We leave Friday after work and come back Sunday." | never-volunteer-2 | a short spoken reply; no tool call |
| 1.3 | "For gear we need two tents, four sleeping bags, a camp stove, and a cooler." | never-volunteer-3 | a short spoken reply; no tool call |
| 1.4 | "Can you write all this down?" | create-when-nothing-fits | `create_document` at once, holding the dates, people and gear; no question; no "the tool says"; a few words when done |
| 1.5 | "Also note that I need to book the campsite by Friday." | add-to-this-sessions-document | `edit_document` on the document from 1.4; no new document; no question |
| 1.6 | "Add a packing list with sunscreen, bug spray, and a first aid kit." | add-a-section | `edit_document` on the same document |
| 1.7 | "Give me a summary of that document." | summary-is-spoken | a spoken summary; no write of any kind |
| 1.8 | "What do you think is missing from the plan?" | opinion-is-spoken | a spoken opinion of one or two points; no write |
| 1.9 | "Add that to the plan." | add-what-you-just-said | `edit_document` adding what it suggested in 1.8; no question |
| 1.10 | "Change the title to Lake Weekend Plan." | rename | one rename; a few words; no step talk |
| 1.11 | "Write the budget in a new document: two hundred for the site, a hundred for food, and fifty for gas." | new-document-beside-a-similar-one | `create_document` for a second, separate document; the plan is not edited |
| 1.12 | "Add dog food to the packing list, and tell me whether you think bringing the dog is a good idea." | edit-and-speak | both halves: the packing list is edited in the plan AND the opinion is spoken |
| 1.13 | "Change the line that says we leave on Thursday to Wednesday." (there is no such line) | failed-edit-one-sentence | one short sentence that it could not; no tool names; no new document |

### Session 2: attach "Lake Weekend Plan" before starting

| # | Say | Frame label | The turn passes when |
|---|---|---|---|
| 2.1 | "Summarize this document for me." | summary-of-attached-is-spoken | a spoken summary; no new document; no edit |
| 2.2 | "I'm thinking we should push the trip a week later because of the weather." | no-write-unasked-1 | a short spoken reply; no edit |
| 2.3 | "The forecast looks better for the weekend of the nineteenth." | no-write-unasked-2 | a short spoken reply; no edit |
| 2.4 | "Note that down." | note-goes-to-document-under-discussion | `edit_document` on the attached plan with the new date; no new document; no question |
| 2.5 | "Clean up the gear section so it's grouped by who carries what." | restructure-in-place | `edit_document` on the attached plan |
| 2.6 | "Read me the packing list." | read-aloud-only-when-asked | the packing list is read out, and only that |
| 2.7 | "Separate thing. I have a job interview on Tuesday for a product manager role." | topic-change-no-write | a short spoken reply; no tool call |
| 2.8 | "I want to prepare three stories: one about a launch, one about a conflict, and one about a failure." | topic-change-no-write-2 | a short spoken reply; no tool call |
| 2.9 | "Write that down." | unrelated-topic-gets-its-own-document | `create_document` for the interview notes. Writing them into the camping plan fails the row. Asking where to put them is recorded as a soft miss. |
| 2.10 | "Make a summary document of the camping plan." | summary-document-when-asked-for-one | `create_document` holding a summary; the plan itself is not edited |

### Session 3: nothing attached

| # | Say | Frame label | The turn passes when |
|---|---|---|---|
| 3.1 | "Add a headlamp to my camping plan." | named-past-document-is-found | first call looks the document up (`list_documents` or `search_documents`), then edits it; no new document |
| 3.2 | "What's in my interview notes?" | question-about-past-document | first call looks it up, then a spoken answer; no write |
| 3.3 | "Summarize my camping plan." | summary-of-past-document-is-spoken | a spoken summary; no summary document |
| 3.4 | "Which of my documents mentions sunscreen?" | search-then-speak | `search_documents`, then a spoken answer; no write |
| 3.5 | "Start a list for my apartment move: boxes, packing tape, and a truck rental." | create-with-many-unrelated-documents | `create_document` at once; no listing step first; no question. This is the shape of the turn that failed in session `5dc89ade…`. |
| 3.6 | "Thanks, that's all." | closing-no-tool | a few words; no tool call |

## Acceptance bar

- Every row passes as its last column says, except 2.9, where asking is
  recorded and only a write into the camping plan fails.
- No turn's transcript contains "the tool says", "existing document
  first", or "do you want a new document".
- The original October failure does not come back: rows 1.7, 2.1 and 3.3
  create nothing, and rows 1.5, 1.6, 1.9 and 2.4 edit the document under
  discussion.
- Rows 1.1 to 1.3, 2.2, 2.3, 2.7 and 2.8 call no tool: nothing is written
  unasked.
- A stray document on any gated row reopens the question of a guard, and
  the first response is a prompt change, not a second model.
- Duplicates from documents the model cannot see are a known gap (a
  related document from an earlier session that is neither attached nor
  named by the user). Record any that happen. They are not a failure of
  this change.
- First audio on 1.4 and 3.5 is recorded from `turn_metrics` and compared
  with the same kind of turn in session `5dc89ade…`, where the gate added
  a judge call, a listing step and an extra user turn.

## Results

Not run yet. The run needs the owner's voice on the local dev server.
Resume point: start the dev containers on the commit that removes the
gate, then session 1 row 1.1. After the run: results appended here, and
the frames and the replay script are built from the three sessions'
traces.
