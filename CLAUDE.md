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

- **Branching model:** `main` is the GitHub default and the integration "workbench" — feature branches PR into it (CI + Claude review gate). `prod` is the production pointer: it only ever moves to a **verified-deployed** commit (the deploy workflow moves it after the health check; rollback moves it backward). Promotion to prod is always the user's explicit call. `prototype-gemini-live` preserves the original Gemini Live prototype (retired design docs: see the Docs section).
- **Everything runs in Docker.** Never install packages or dependencies on the host machine — no venvs, no global pip/npm installs. Run backend commands (tests, lint, one-off scripts) inside the compose services, e.g. `docker compose run --rm backend python -m pytest -q`; the frontend service manages its own `node_modules` volume. Dependency changes go in `requirements.txt` / `package.json` so the Docker images pick them up.
- **When a review lands, the summary is its own message BEFORE any editing.** Output a concise findings summary with an assessment (real vs. churn; what will be fixed vs. pushed back on) as a complete, turn-ending message — mid-turn text between tool calls may not render, so the summary must stand alone on screen first. Then resume (e.g., via a short background timer) and do the fixes without waiting for approval; the user reads while the work happens and vetoes afterward. The unpushed commit is the veto window.
- **NEVER pre-emptively push or open PRs — no exceptions.** Commits at reasonable checkpoints are fine on Claude's own judgment. Pushes and PRs are not: only *suggest* them and wait for explicit approval, **every time** — a batch of pending commits gets one suggestion, and each subsequent push needs its own approval. Broad instructions like "work end-to-end," "be agentic," or "resolve issues on your own" do NOT override this rule; only an explicit "push" / "open the PR" does. (Every push to an open PR re-runs the Claude review — pushes cost money and are the user's call.)

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
- **[CURRENT-ARCHITECTURE.md](CURRENT-ARCHITECTURE.md) is the accurate map of the current product — enforced at merge and deploy.** Everything the "current" product runs on must be reflected there: code structure, deployment, and every piece and service of its infrastructure — database, GCP services, security protocols, alerting and email policies, third-party providers, CI/CD — and anything else a new engineer would need to know exists. The hard requirement sits at the boundaries: any commit merging to `main` or deploying to prod must carry an architecture document accurate as of that commit. On dev branches, update it at Claude's discretion as pieces are *finalized* (a service installed and verified, a design settled — document it then, don't wait for the PR); skip documenting intermediate states while something is still in ideation, development, or testing. When the document and reality disagree, reality is right and the document has a bug.
- The original design docs (SDD.md, SDD-v2.md) were removed as stale; the implemented code is the design's source of truth. They remain readable at commit `a84df1b` (`git show a84df1b:SDD.md`, `git show a84df1b:SDD-v2.md`) — e.g., for future blog posts.
