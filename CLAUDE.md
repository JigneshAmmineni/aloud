# Aloud — Project Context for Claude

## What this is

A voice-first work assistant for people who think and work by talking out loud. The user speaks; the agent listens, handles the tiny details, and gives their words structure — turning trains of thought into coherent write-ups the user can review, answering directly when spoken to, and following the user's lead rather than steering (it still asks sharp questions and pressure-tests plans when invited) — hands-free, on the go.

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

## SDLC — how a feature ships

The lifecycle every feature follows, start to finish. The Workflow rules below are the hard rules inside it; this is the shape of the whole loop.

**1. Roadmap.** Every feature starts as a [ROADMAP.md](ROADMAP.md) entry — vision, rough scope, open questions, decisions parked as "spec-time". Working notes there are aspirational and carry no weight in specs or reviews. Feature order is deliberate (dependencies are stated in the entries).

**2. Spec.** When a feature is next up, it is specced from its roadmap entry into [REQUIREMENTS.md](REQUIREMENTS.md) as a new § on its own branch, and the spec PR goes through the same Claude-review gate as code — spec PRs get multiple review rounds too (§4.11 took 12). Spec conventions:
- **FRs are atomic, and the first sentence of an FR *is* the requirement.** Everything after it is mechanism, rationale, or bounds.
- **Requirements are state, not journey.** Amend FRs in place; delete removed features outright. The journey lives in ROADMAP.md and git history, never in REQUIREMENTS.md.
- Contested design decisions are written into the spec explicitly (e.g., §4.12: on merge conflict, user edits take precedence; CRDTs rejected for server-row-as-buffer). Reviewers then check that a mechanism exists behind every stated guarantee.

**3. Implementation.** A feature branch off `main` implements the §. Commits land at reasonable checkpoints on Claude's judgment (no approval needed); pushes and the PR itself need explicit approval — see Workflow rules. The per-push approval can be lifted for one PR by an explicit standing instruction (e.g., "keep fixing and pushing until there are no blockers, then merge"); it covers that PR only and expires at merge.

**4. The review loop.** Every push to the PR triggers CI and the tailored Claude review; rounds repeat until a round returns zero blocking findings.
- **Local gates before every push**, all in Docker: `npx tsc --noEmit`, vitest, the backend suite on the CI lane (`docker compose run --rm -e DATABASE_URL=sqlite+aiosqlite:// backend python -m pytest -q`), the Postgres-only lane (`tests/test_documents_pg.py` with `RLS_TEST_DATABASE_URL`), `ruff check .`, and a clean `npm run build` (stop the dev container first — `.next` contention). Compose sets a Postgres `DATABASE_URL`, so the sqlite env override is what reproduces CI.
- **When a round lands:** findings summary as its own turn-ending message first (real vs. churn, fix vs. push back), then fix without waiting — the Workflow rules below spell this out. Where the review mandates a test, prefer proving it discriminates (revert the fix, watch it fail) over trusting it.
- Fixes land as one commit per round (`fix: PR #N round K — …`). A new push supersedes a running review — cancel the stale run rather than paying for both.
- Non-blocking findings are either fixed in the same round or carried forward explicitly (recorded in the round summary, or opening the next branch's to-do list) — never silently dropped. An advisory can be accepted with a comment recording the trade instead of a change.

**5. Merge.** Merge commit, not squash (matches #21/#22). The merge boundary enforces CURRENT-ARCHITECTURE.md accuracy (see Docs). Merging is covered by the same approval rule as pushes unless a standing instruction already authorized it at zero blockers.

**6. Deploy.** Always the user's call, never Claude's. The "Deploy to production" workflow ships a chosen `main` commit to the VM, health-checks it, and only then moves `prod`; rollback moves `prod` backward. Before any deploy whose range includes a DB migration: manual `pg_dump` first (deployment.md §13). Deploys batch — several merged features can ship as one promotion. A promotion gate (golden-audio E2E + manual talk-through) is planned to run inside the deploy workflow (ROADMAP.md "Process infrastructure").

**7. Behavioral acceptance.** Changes to prompts or agent behavior get a dated `docs/evals/` file **written before the run**, results appended after — that file is what the PR's acceptance bar points to. If only the owner can run the check (live voice session), the deferral is recorded in the Results section with its resume point and the PR ships provisionally; the eval stays open until the owner runs it.

---

## Workflow rules

- **Branching model:** `main` is the GitHub default and the integration "workbench" — feature branches PR into it (CI + Claude review gate). `prod` is the production pointer: it only ever moves to a **verified-deployed** commit (the deploy workflow moves it after the health check; rollback moves it backward). Promotion to prod is always the user's explicit call. `prototype-gemini-live` preserves the original Gemini Live prototype (retired design docs: see the Docs section).
- **Everything runs in Docker.** Never install packages or dependencies on the host machine — no venvs, no global pip/npm installs. Run backend commands (tests, lint, one-off scripts) inside the compose services, e.g. `docker compose run --rm backend python -m pytest -q`; the frontend service manages its own `node_modules` volume. Dependency changes go in `requirements.txt` / `package.json` so the Docker images pick them up.
- **When a review lands, the summary is its own message BEFORE any editing.** Output a concise findings summary with an assessment (real vs. churn; what will be fixed vs. pushed back on) as a complete, turn-ending message — mid-turn text between tool calls may not render, so the summary must stand alone on screen first. Then resume (e.g., via a short background timer) and do the fixes without waiting for approval; the user reads while the work happens and vetoes afterward. The unpushed commit is the veto window.
- **NEVER pre-emptively push or open PRs — no exceptions.** Commits at reasonable checkpoints are fine on Claude's own judgment. Pushes and PRs are not: only *suggest* them and wait for explicit approval, **every time** — a batch of pending commits gets one suggestion, and each subsequent push needs its own approval. Broad instructions like "work end-to-end," "be agentic," or "resolve issues on your own" do NOT override this rule; only an explicit "push" / "open the PR" does. (Every push to an open PR re-runs the Claude review — pushes cost money and are the user's call.)

---

## Hard constraints

- Never describe the app as therapy or the agent as a therapist — in code, copy, system prompts, or documentation.
- All provider SDK usage lives in `agent/providers.py` (factories) behind `CompanionAgent`. No direct SDK calls in route handlers or anywhere else. (One stated exemption: `backend/scripts/spike_gemini_stream.py`, the FR-48 spike kept as a local-only diagnostic — never imported by runtime code.)
- A `user_id` reaching any repo function or query is only ever the output of server-side credential verification (`get_current_user_id`) — never read from a request body, query param, or client-set header. Repo signatures take `user_id: str` with no default value.
- DB schema: sensitive content columns must be separable from metadata. Design for encryption even if you don't implement it yet.
- Every pipeline stage is instrumented: structured logs with `session_id`/`turn_id`, per-stage latency, WARN over 1s per stage, ERROR over 3s end-of-speech → first audio.

---

## Docs

- [ROADMAP.md](ROADMAP.md) — feature order, vision, and process; PRs implement FRs specced from it
- [REQUIREMENTS.md](REQUIREMENTS.md) — what the product must do (FR/NFR/constraints)
- PLAN.md — scrap notes / future directions (deployment options, post-MVP ideas); local-only, gitignored
- **[CURRENT-ARCHITECTURE.md](CURRENT-ARCHITECTURE.md) is the accurate map of the current product — enforced at merge and deploy.** Everything the "current" product runs on must be reflected there: code structure, deployment, and every piece and service of its infrastructure — database, GCP services, security protocols, alerting and email policies, third-party providers, CI/CD — and anything else a new engineer would need to know exists. The hard requirement sits at the boundaries: any commit merging to `main` or deploying to prod must carry an architecture document accurate as of that commit. On dev branches, update it at Claude's discretion as pieces are *finalized* (a service installed and verified, a design settled — document it then, don't wait for the PR); skip documenting intermediate states while something is still in ideation, development, or testing. When the document and reality disagree, reality is right and the document has a bug.
- `docs/evals/` — dated behavioral test plans and their results (A/B listening tests, prompt-change evaluations). One file per evaluation, written before the run, results appended after — the record a PR's acceptance bar points to.
- `docs/notes/` — personal brainstorming/learning notes (agent-loop.md, memory.md, auth.md). Background reading, never specs; excluded from review scope.
- The original design docs (SDD.md, SDD-v2.md) were removed as stale; the implemented code is the design's source of truth. They remain readable at commit `a84df1b` (`git show a84df1b:SDD.md`, `git show a84df1b:SDD-v2.md`) — e.g., for future blog posts.
