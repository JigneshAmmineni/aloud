# Eval record: create_document's question/write-up boundary

**Date:** 2026-10-10 · **Status:** change applied; informal check pending

## What changed (verbatim)

`create_document`'s tool description previously opened:

> "Write up a document of the conversation and put it on the user's
> screen: a structured summary, a list of action items, or a cleaned-up
> version of the user's idea. Use only when the user asks for a
> write-up."

It now reads:

> "Create a NEW document on the user's screen: a structured summary, a
> list of action items, or a cleaned-up version of the user's idea. Use
> ONLY when the user explicitly asks for a document or write-up —
> 'write that up', 'create a summary doc', 'put that in a document'.
> 'Give me a summary', 'summarize this', or any question is answered
> OUT LOUD, never by creating a document."

The system prompt is unchanged (it already draws this line).

## Observed failure this fixes

Dogfooding 2026-10-10: "give me a summary of this document" produced a
new summary document instead of a spoken answer — the question-vs-
write-up boundary lived only in the system prompt, and the tool
description's "structured summary" phrasing invited the call.

## Rule under test

Questions ("give me a summary", "summarize this", "what's the plan") →
spoken answer, no tool call. Explicit document requests ("write that
up", "create a summary document") → create_document.

## Check protocol (same discipline as the 3.1 A/B, scaled down)

Scripted, two runs each, live session:
1. "Give me a summary of this document" (doc attached) → expect spoken
   summary, NO document.created announce.
2. "Summarize what we've talked about" → spoken, no announce.
3. Overcorrection check: "Write that up as a summary document" →
   expect create_document, announce, brief spoken confirm.
4. Overcorrection check: "Put the action items in a doc" → expect
   create_document with kind action_items.

## Results

_(append after running)_
