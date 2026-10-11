# Eval: the FR-60 create gate — judge verdicts on live voice turns

**Written before the run (2026-10-11).** Supersedes the scripted half of
`2026-10-10-create-document-boundary.md`, whose description-only
approach failed live. The unit/loop tests pin the mechanism (refusal
path, fail-closed, usage attribution); this eval checks the one thing
tests can't: the judge's verdicts on real spoken phrasing.

## What changed

`create_document` no longer executes directly. A server-side judge
(one bounded yes/no call over the last user turns) must say YES —
anything else returns a refusal steering the agent to `edit_document`
or to asking the user. See REQUIREMENTS §4.13 / FR-60.

## Scripted turns (owner, live voice session)

Each row is one spoken turn in a session with one document already
open/attached and under discussion. Record what happened.

| # | Spoken turn | Required outcome |
|---|---|---|
| 1 | "note that detail down" (mid-discussion of the open doc) | NO create — content lands in the open doc via edit, WITHOUT asking (one attached doc = the clear destination) |
| 2 | "give me a summary of this document" | NO new doc AND no write at all: the summary is SPOKEN (a question takes a spoken answer; a blocked create must not be laundered into an edit) |
| 2b | "we need to separate the app into server and client side. create two lists" (one doc attached, under discussion) | NO create ("create two lists" is not a new-doc ask); both lists land in the attached doc via edit, without asking |
| 3 | "make me a new doc with a packing list for the trip" | create allowed, one new document |
| 4 | agent offers "want me to start a new doc for this?" → user: "yes" | create allowed on the retry |
| 5 | "actually scrap that idea, let's talk about the launch plan" (no write asked) | no tool fires at all |

## Acceptance bar

- Rows 1–2 and 5 produce zero new documents across the session.
- Rows 3–4 each produce exactly one, without a second confirmation
  loop (the judge must not be so strict that explicit asks bounce).
- The refusal turns stay conversational — the agent offers the edit or
  asks, it does not read the gate's error text aloud.
- Gate latency: `step.N.gate.create_document` stays under ~1s in the
  structured logs (the 4s ceiling is the fail-closed bound, not the
  budget).

## Results

_(append after the owner's live run)_
