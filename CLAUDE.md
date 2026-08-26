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
| Auth | TBD (likely Supabase Auth or Auth.js); MVP runs single-user |

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
- Adding `pgcrypto` or app-level AES to bounded columns later is straightforward if the schema is clean. The schema in SDD.md §3 marks every sensitive column.

---

## Workflow rules

- **Never commit or push without being explicitly asked.** Make changes, then wait for the user to say "commit" or "push" before running any git commit or git push command.

---

## Hard constraints

- Never describe the app as therapy or the agent as a therapist — in code, copy, system prompts, or documentation.
- All provider SDK usage lives in `agent/providers.py` (factories) behind `CompanionAgent`. No direct SDK calls in route handlers or anywhere else.
- DB schema: sensitive content columns must be separable from metadata. Design for encryption even if you don't implement it yet.
- Every pipeline stage is instrumented: structured logs with `session_id`/`turn_id`, per-stage latency, WARN over 1s per stage, ERROR over 3s end-of-speech → first audio.

---

## Docs

- [ROADMAP.md](ROADMAP.md) — feature order, vision, and process; PRs implement FRs specced from it
- [REQUIREMENTS.md](REQUIREMENTS.md) — what the product must do (FR/NFR/constraints)
- [SDD.md](SDD.md) — software design, Pipecat-based (the version to build)
- [SDD-v2.md](SDD-v2.md) — alternative fully hand-rolled design, for learning value
- PLAN.md — scrap notes / future directions (deployment options, post-MVP ideas); local-only, gitignored
