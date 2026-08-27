# Aloud — Project Context for Claude

## What this is

A voice-first thinking partner for people who process ideas best by talking out loud. The user speaks; the agent listens, asks sharp questions, and helps them brainstorm, pressure-test plans, and organize messy thoughts — hands-free, on the go.

**It is NOT a therapist. Never use the words "therapy," "therapist," or "counselor" in the product, UI copy, system prompts, or documentation.**

---

## Stack (cascade architecture)

| Layer | Choice |
|---|---|
| Frontend | Next.js + React + Pipecat JS client SDK |
| Backend | Python + FastAPI |
| Voice orchestration | Pipecat (cascade pipeline: turn detection → STT → LLM → TTS) |
| Transport | WebRTC via Pipecat SmallWebRTC |
| STT + turn detection | Deepgram Flux |
| LLM (default) | Gemini Flash — swappable; Claude Sonnet is the runner-up if question quality disappoints |
| TTS | Cartesia Sonic-3 |
| DB | PostgreSQL |
| Auth | Firebase Auth — Google + email/password, Bearer ID tokens verified via firebase-admin in `get_current_user_id`; admin via custom claims (REQUIREMENTS.md §4.8) |

---

## Key architectural decisions

### Cascade, not speech-to-speech

The earlier prototype (`main` branch) used Gemini Live native audio and hit its limits: 32K audio-token context, no control over memory, no provider swap. This branch rebuilds as a cascaded pipeline where conversation state is **text on our server** — which is what makes the memory layer, provider swapping, and session resume work.

### Provider abstraction (required, do not skip)

All provider-specific construction (STT/LLM/TTS service classes, SDK imports) is isolated in `agent/providers.py` factory functions; `agent/companion.py` (`CompanionAgent`) builds and runs the pipeline from those factories. Everything else — memory layer, session logic, DB, API routes, prompts — is provider-agnostic. Swapping any provider is a change to one factory plus an env var.

### Memory (MVP: in-session only)

- **Cross-session memory is deferred** (see REQUIREMENTS.md §6 Out of Scope). In-session, the Pipecat `LLMContext` holds the full session history. Planned future direction: a MemGPT-style framework, possibly RAG with semantic vector search, processed in parallel while the user is still speaking.
- Full transcripts are stored in the DB **as an operational log only** — never injected into the agent's context, never user-facing.

### Encryption (add after MVP)

Cloud processing is fine — the privacy policy discloses that voice and transcripts are processed by Deepgram, Google, and Cartesia.

Server-side encryption (key held by us) is added post-MVP. To make this painless:
- **Keep sensitive columns clearly separated from metadata at schema design time.** Transcripts, summaries, memory entries, and artifacts get their own columns; timestamps, IDs, and session metadata stay unencrypted.
- Adding `pgcrypto` or app-level AES to bounded columns later is straightforward if the schema is clean. Sensitive columns are the content-bearing ones in `db/models.py` (transcript text, artifact title/content, and future memory entries).

---

## Workflow rules

- **Everything runs in Docker.** Never install packages or dependencies on the host machine — no venvs, no global pip/npm installs. Run backend commands (tests, lint, one-off scripts) inside the compose services, e.g. `docker compose run --rm backend python -m pytest -q`; the frontend service manages its own `node_modules` volume. Dependency changes go in `requirements.txt` / `package.json` so the Docker images pick them up.
- **Commit at reasonable checkpoints; never push or open PRs unprompted.** Claude may commit on its own when a coherent unit of work is done, but must only *suggest* a push or PR and wait for explicit confirmation — the user may have additional changes or instructions first.

---

## Hard constraints

- Never describe the app as therapy or the agent as a therapist — in code, copy, system prompts, or documentation.
- All provider SDK usage lives in `agent/providers.py` (factories) behind `CompanionAgent`. No direct SDK calls in route handlers or anywhere else.
- A `user_id` reaching any repo function or query is only ever the output of server-side credential verification (`get_current_user_id`) — never read from a request body, query param, or client-set header. Repo signatures take `user_id: str` with no default value.
- DB schema: sensitive content columns must be separable from metadata. Design for encryption even if you don't implement it yet.
- Every pipeline stage is instrumented: structured logs with `session_id`/`turn_id`, per-stage latency, WARN over 1s per stage, ERROR over 3s end-of-speech → first audio.

---

## Docs

- [ROADMAP.md](ROADMAP.md) — feature order, vision, and process; PRs implement FRs specced from it
- [REQUIREMENTS.md](REQUIREMENTS.md) — what the product must do (FR/NFR/constraints)
- PLAN.md — scrap notes / future directions (deployment options, post-MVP ideas); local-only, gitignored
- [CURRENT-ARCHITECTURE.md](CURRENT-ARCHITECTURE.md) — living record of the stack, infrastructure, pages, processes, and decision log; update it in the same PR as any change to those
- The original design docs (SDD.md, SDD-v2.md) were removed as stale; the implemented code is the design's source of truth. They remain readable at commit `a84df1b` (`git show a84df1b:SDD.md`, `git show a84df1b:SDD-v2.md`) — e.g., for future blog posts.
