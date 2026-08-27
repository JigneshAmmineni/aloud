# Aloud — Roadmap

A loosely guiding document: intended feature order, vision, and direction —
**not** a source of truth for what gets implemented. Each feature starts only
on the author's explicit instructions, and its contract is the FR section
added to [REQUIREMENTS.md](REQUIREMENTS.md) at that point (numbered FRs with
acceptance criteria). PRs reference the FRs they implement; reviews check the
diff against those FRs, not against this document. Items marked *optional*
here are aspirational notes and carry no weight in specs or reviews.

## Vision

Aloud is a voice-first thinking partner for people who process ideas best by
talking out loud. The finished product: you open it on any device, talk through
whatever you're working on, and the agent — which remembers your past sessions,
your documents, and your open threads — asks sharp questions, pressure-tests
your plans, and turns the mess into organized artifacts you can watch it write.
Multi-user, private by design: every user's conversations, memories, and
documents are theirs alone.

## Feature order

The order is intentional: auth establishes `user_id`, which everything after it
hangs on (per-user metrics, per-user documents, per-user memory isolation) —
and the context engine (4) lands before memory storage/retrieval (5), because
retrieval injection and memory-management writes both depend on a context
window we control section-by-section.

### 1. Auth — individual accounts

Replace the single site-wide password gate with real per-user accounts.

- Every request resolves to a verified `user_id` at the HTTP boundary via one
  FastAPI dependency (`get_current_user_id`) — the only auth-aware code in the
  backend. Repo functions take `user_id: str` and never know where it came from.
- `user_id` always originates from server-side credential verification, never
  from anything the client sends in a payload.
- Provider: Firebase Auth — Google sign-in + email/password, open signup.
  Bearer ID tokens verified server-side via firebase-admin inside the
  dependency; the seam keeps the provider swappable.
- Admin controls (gated to admin accounts only): list users and
  disable/enable them. Usage views arrive with feature 2, which grows this
  admin surface.
- Postgres row-level security turns on when multi-user lands — not deferred.
- *Optional:* account-linking UI — a settings flow to link/unlink providers on
  one account (e.g., add a password to a Google account). Aspirational note
  only; not part of any spec or review until explicitly instructed.

### 2. Observability — per-user usage & admin view

An admin-only surface answering "who uses this, and what does it cost?"

- Searchable list of all users.
- Per-user token/usage metrics for each pipeline stage: STT (audio minutes),
  LLM (input/output tokens), TTS (characters) — and the cost they imply.
- Standard product/ops metrics per user: session counts, session durations,
  last-active, error rates, per-stage latency percentiles (the instrumentation
  already required by CLAUDE.md becomes queryable here).
- Exact metric set is decided when this feature is specced.

### 3. Documents & artifacts rework

From single upload-at-start + copy-paste artifacts to a real document workspace.

- Two side-by-side scrollable lists: **user-uploaded** documents and
  **agent-produced** documents.
- Clicking a document opens a preview.
- The agent can edit these documents, and the user sees changes live in the
  preview. Markdown first; other file types as the engineering allows.
- Later (own spec, after the basics land): multiple adjustable preview windows;
  live co-editing where user edits and agent edits flow both ways.
- Later (depends on the context engine, feature 4): **mid-conversation
  uploads** — add a document (including drag-and-drop) while talking, and the
  agentic loop proactively picks it up, indexes it, and retrieves from it as
  the conversation calls for it. This replaces today's upload-before-session
  flow, which injects whole documents into the context window at session
  start and gets deprecated once this lands.

### 4. Context engine — structured, owned context window

Replace the pipeline's monolithic `LLMContext` with a context window we fully
control — the foundation the memory layer builds on.

- Distinct, individually budgeted sections: system prompt, core memory
  (durable facts about the user), retrieved memory (populated by feature 5),
  and the current conversation.
- Auto-compression of the conversation section under memory pressure
  (recursive summarization: oldest turns compressed into summaries, raw turns
  evicted) — per memory.md's MemGPT-style sketch.
- Full programmatic control of context assembly each turn, with per-section
  token accounting and instrumentation.
- Spec-time decision: a custom context-management stage inside the Pipecat
  pipeline vs. a custom agent loop replacing the pipeline's LLM stage
  (STT/TTS/transport stay on Pipecat either way; the old hand-rolled design
  doc SDD-v2.md, kept in git history, is background for that direction).

### 5. Memory layer — cross-session memory

Tiered, MemGPT-style memory so the agent remembers past sessions and documents.
Builds on the context engine: retrieval fills its retrieved-memory section; the
memory-management loop consumes its summaries and evictions.

- Storage/indexing structure and the retrieval mechanism are designed
  **together** — the structure is judged by the latency and quality of
  retrieval, including how retrieval triggers mid-speech without touching the
  voice hot path.
- Strict per-user isolation: every memory row is scoped by `user_id` and
  covered by RLS; one user's agent can never retrieve another user's data.
- Retrieval quality is measured by evals (golden query→memory datasets), not
  just unit tests.

### 6. LLM tracing

Every LLM call recorded as a trace: full input messages, output, model, token
counts, latency, purpose (voice turn / memory loop / compression), linked to
`session_id`/`turn_id`.

- The substrate for evals (golden datasets, LLM-as-judge, provider
  comparisons) and for debugging the context engine and memory loop —
  complements feature 2's aggregate usage metrics.
- Trace content columns (inputs/outputs) separate from metadata, per the
  encryption rule.
- A minimal version may be pulled forward if debugging features 4–5 demands
  it.

## Process infrastructure

Per-PR CI (ruff + pytest, frontend build) and the tailored Claude review are
live. Still to add, roughly when its prerequisite feature lands:

- **Promotion gate (main → prod):** before `prod` fast-forwards to `main`,
  run the heavy suite per-PR CI deliberately skips — golden-audio E2E through
  the real providers (recorded utterances with known content; assert loosely:
  transcript keywords, a response produced, tool fired, latency within
  budget) — plus a manual talk-through. Later, a deploy workflow keyed off
  `prod` completes the pipeline.
- **Evals** (retrieval recall, response quality) join the scheduled/nightly
  lane once features 5–6 provide the data they run on.

## Known risks

Carried over from the retired SDD's risk register — still live, revisit as
features land:

- **LLM question quality** — the product *is* question quality. Gemini Flash
  (thinking disabled) is the bet; if it underwhelms, the swap lever is
  `make_llm()` (Claude Sonnet was the runner-up). Decide after dogfooding,
  not benchmarks.
- **Barge-in depends on browser echo cancellation** — known flaky on mobile
  Safari speakerphone/Bluetooth. Verify on real phones when touching
  turn-taking.
- **iOS screen lock kills web audio** — on-the-go use is screen-on only; a
  native wrapper is the eventual fix, out of scope for now.
- **Deepgram Flux pricing/quotas** at sustained usage — verify before any
  public launch.
- **Eager end-of-turn** (speculative LLM calls) trades cost for latency — a
  lever to pull only if measurements demand it.

## Working notes

[auth.md](auth.md) and [memory.md](memory.md) are brainstorming/learning notes,
not specs — useful background when speccing features 1 and 4, but REQUIREMENTS.md
is what implementation and review are held to.
