# Aloud — Roadmap

The plan-level source of truth: what gets built, in what order, and why that
order. Deliberately coarse — each feature gets its detailed, testable
requirements (numbered FRs with acceptance criteria) added to
[REQUIREMENTS.md](REQUIREMENTS.md) **just before** that feature starts, not all
up front. PRs reference the FRs they implement; reviews check the diff against
them.

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
hangs on (per-user metrics, per-user documents, per-user memory isolation).

### 1. Auth — individual accounts

Replace the single site-wide password gate with real per-user accounts.

- Every request resolves to a verified `user_id` at the HTTP boundary via one
  FastAPI dependency (`get_current_user_id`) — the only auth-aware code in the
  backend. Repo functions take `user_id: str` and never know where it came from.
- `user_id` always originates from server-side credential verification, never
  from anything the client sends in a payload.
- Signup/login, session credential handling, and an account-gating policy
  (closed signup / whitelist to start).
- Postgres row-level security turns on when multi-user lands — not deferred.
- Provider decision (managed e.g. Supabase Auth vs. hand-rolled sessions) is
  recorded in REQUIREMENTS.md when this feature is specced; the dependency seam
  makes it swappable either way.

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

### 4. Memory layer — cross-session memory

Tiered, MemGPT-style memory so the agent remembers past sessions and documents.

- Storage/indexing structure and the retrieval mechanism are designed
  **together** — the structure is judged by the latency and quality of
  retrieval, including how retrieval triggers mid-speech without touching the
  voice hot path.
- Strict per-user isolation: every memory row is scoped by `user_id` and
  covered by RLS; one user's agent can never retrieve another user's data.
- Retrieval quality is measured by evals (golden query→memory datasets), not
  just unit tests.

## Working notes

[auth.md](auth.md) and [memory.md](memory.md) are brainstorming/learning notes,
not specs — useful background when speccing features 1 and 4, but REQUIREMENTS.md
is what implementation and review are held to.
