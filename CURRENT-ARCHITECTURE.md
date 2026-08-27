# Aloud — Current Architecture

The living record of what this system **is right now**: stack, dependencies,
infrastructure, pages, operational processes, and the decisions behind them.
Structure loosely follows [arc42](https://arc42.org) (the de-facto architecture
documentation template) at [C4](https://c4model.com) context/container depth,
with an ADR-style decision log and SRE-style runbook pointers.

**Maintenance rule:** any PR that changes the stack, infrastructure, pages, or
an operational process updates this file in the same PR. When this document and
the code disagree, the code is right and this file has a bug.

Related docs: [deployment.md](deployment.md) (step-by-step VM runbook),
[REQUIREMENTS.md](REQUIREMENTS.md) (the contract), [ROADMAP.md](ROADMAP.md)
(direction). The original design docs live in git history at commit `a84df1b`.

---

## 1. System context

```
 user (browser: desktop/mobile)
   │  HTTPS 443 — page, JS, WebRTC signaling
   │  UDP       — live audio, both directions
   ▼
 Aloud (single GCE VM, four Docker containers)
   │
   ├─▶ Deepgram Flux ─── STT + end-of-turn detection   (wss, outbound)
   ├─▶ Google Gemini ─── LLM (2.5 Flash, thinking off) (https, outbound)
   ├─▶ Cartesia ──────── TTS (Sonic-3)                 (https, outbound)
   └─▶ Firebase Auth ─── identity (project aloud-c74f5) [being integrated,
                          spec'd in REQUIREMENTS.md §4.8 — not live yet]

 Supporting systems: GitHub (repo, Actions CI, Claude PR review),
 Let's Encrypt (TLS via Caddy), Google Cloud (VM, IAP SSH).
```

One human-facing product surface (the web app), three paid AI provider
dependencies on the voice path, one identity provider (incoming).

## 2. Tech stack

| Layer | Technology | Version / detail |
|---|---|---|
| Frontend | Next.js + React | Next 15, React 19, TypeScript 5; Pipecat JS client SDK (`@pipecat-ai/client-js`, `small-webrtc-transport`) |
| Backend | Python + FastAPI | Python 3.12 (`python:3.12-slim` image), uvicorn |
| Voice orchestration | Pipecat | `pipecat-ai[webrtc,deepgram,google,cartesia]~=1.3.0` — cascade: turn detection → STT → LLM → TTS |
| Transport | WebRTC | Pipecat SmallWebRTC; server-side peer on the VM; STUN `stun.l.google.com` |
| STT + turns | Deepgram Flux | transcription and end-of-turn in one model |
| LLM | Gemini 2.5 Flash | thinking disabled (latency); swappable via `make_llm()` |
| TTS | Cartesia Sonic-3 | speed 0.85, markdown/identifier sanitizer in front |
| DB | PostgreSQL 16 | `postgres:16-alpine`; SQLAlchemy async + asyncpg (tests: aiosqlite) |
| Auth (incoming) | Firebase Auth | Google + email/password; Bearer ID tokens; firebase-admin verification |
| Reverse proxy | Caddy 2 | TLS (Let's Encrypt), path routing; currently also the interim `basic_auth` gate |
| Dev/prod runtime | Docker Compose | everything runs in Docker — no host installs (CLAUDE.md rule) |

### Backend packages (`backend/requirements.txt`)

`pipecat-ai[webrtc,deepgram,google,cartesia]` (pipeline + provider services),
`pipecat-ai-small-webrtc-prebuilt` (debug UI), `fastapi`, `uvicorn[standard]`,
`sqlalchemy[asyncio]`, `asyncpg`, `pypdf` (PDF text extraction),
`python-dotenv`; dev/test: `pytest`, `aiosqlite`, `httpx`, `ruff`.
Incoming with auth: `firebase-admin`.

### Frontend packages (`frontend/package.json`)

`next`, `react`, `react-dom`, `@pipecat-ai/client-js`,
`@pipecat-ai/small-webrtc-transport`; dev: `typescript`, `@types/*`.
Incoming with auth: `firebase`.

## 3. Repository layout

```
backend/
  app/        FastAPI app: main.py (routes, session wiring), config.py,
              documents.py (upload-time doc store, in-memory)
  agent/      companion.py (CompanionAgent: builds/runs one session's pipeline)
              providers.py (THE provider seam: all SDK construction)
              prompts.py, tools.py (create_artifact), sanitizer.py
  db/         engine.py, models.py (users, sessions, transcript_events,
              artifacts), sessions_repo.py, transcript_log.py
  obs/        logging.py (JSON structured logs), latency.py (per-turn budget
              instrumentation: WARN >1s/stage, ERROR >3s end-to-end)
  tests/      82 tests, SQLite-backed, no network
frontend/     Next.js app (single page today; /login and /admin incoming)
.github/workflows/  ci.yml (ruff+pytest+build), claude.yml (@claude),
              claude-code-review.yml (tailored auto-review)
Caddyfile, docker-compose.yml (dev), docker-compose.prod.yml (prod)
```

Two architectural seams everything hangs on:
- **Provider seam:** all STT/LLM/TTS SDK imports/construction live in
  `agent/providers.py` factories. Swapping a provider = one factory + env var.
- **Auth seam (spec'd):** `get_current_user_id()` will be the only auth-aware
  code; repos take `user_id: str` and never know where it came from.

## 4. Runtime view (one voice session)

1. Browser `POST /start` → session row created, `CompanionAgent` builds a
   dedicated pipeline (its own `LLMContext`, observers, transcript writer).
2. SDP offer/answer via `POST/PATCH /api/offer` (Caddy → backend), then WebRTC
   audio flows browser ↔ backend directly over UDP (bypasses Caddy).
3. Turn loop: Flux detects end-of-turn → transcript frame → LLM streams tokens
   → sentence-level TTS → audio streams out. Barge-in interrupts mid-response.
4. Observers off the hot path: latency breakdown per turn (structured logs,
   session_id/turn_id), transcript rows batch-written to Postgres (ops log
   only — never injected into context, never user-facing).
5. Session ends (tap End / disconnect) → row closed, transcripts flushed.

## 5. Deployment & cloud infrastructure

**Everything runs on one GCE VM.** Full provisioning/ops detail: [deployment.md](deployment.md).

| Item | Value |
|---|---|
| Cloud | Google Cloud Platform |
| Compute | 1× GCE VM `aloud` — `e2-small` (2 vCPU shared, 2 GB RAM) + 2 GB swap, 20 GB disk, Debian 12 |
| Zone | `us-west1-b` (Oregon — close to user, protects the 3s latency budget) |
| IP / DNS | reserved static external IP; A record for `work-aloud.com` (Cloudflare DNS, grey-cloud/DNS-only — proxy would break UDP) |
| GCP services | Compute Engine; IAP (SSH tunnel — port 22 not public). No other GCP services in use |
| Firebase | project `aloud-c74f5` — Auth only (no Hosting/Firestore); web app registered; being integrated |
| TLS | Let's Encrypt via Caddy, auto-renewed, cert persisted in a Docker volume |
| Firewall | tcp 80/443 (web+signaling) · udp 1–65535 (WebRTC media) · tcp 22 from IAP range only · **nothing else** (Postgres/backend ports unreachable from internet) |
| Cost | ~$14–15/mo (VM+disk+IP) + ~$10/yr domain + per-use provider APIs |

**On the VM — four containers** (`docker-compose.prod.yml`), all
`network_mode: host` because WebRTC binds unpredictable UDP ports that Docker
bridge networking can't forward:

| Container | Image | Role |
|---|---|---|
| caddy | caddy:2-alpine | :80/:443 — TLS, path routing (`/api/*,/start,/sessions/*,/healthz` → backend, rest → frontend), interim site-wide `basic_auth` gate (removed when real auth ships) |
| frontend | built from `frontend/Dockerfile` | compiled Next.js on :3000 (loopback-only in practice — not firewalled open) |
| backend | built from `backend/Dockerfile` | FastAPI + Pipecat on :7860 + UDP media on host interface |
| db | postgres:16-alpine | :5432, `listen_addresses=127.0.0.1` (loopback only) |

**Environments:** local dev = `docker-compose.yml` (hot-reload, dev Postgres,
no Caddy) · prod = the VM above · CI = GitHub Actions ubuntu runners (ruff +
pytest on PRs and main; frontend build; Claude review on PRs).

**Known infra gaps** (accepted for demo scale — see deployment.md §17): no DB
backups (pgdata on VM disk only), secrets in a `chmod 600 .env` file (Secret
Manager is the upgrade), no TURN server (UDP-blocking networks fail), single
VM = single point of failure.

## 6. Pages & API surface

**Frontend routes:** `/` — the app (Talk button, waveform states, artifact
panel, document upload). Incoming with auth (§4.8): `/login`, `/admin`.

**Backend endpoints** (`backend/app/main.py`): `GET /healthz` ·
`POST /documents` (upload → in-memory store) · `POST /start` (create session)
· `POST|PATCH /api/offer` and `POST|PATCH /sessions/{id}/api/offer` (WebRTC
signaling). Plus the WebRTC UDP media path (not HTTP). Incoming with auth:
admin endpoints (list/disable/enable users), all routes behind
`get_current_user_id`.

## 7. Operational processes (runbooks)

| Process | How | Status |
|---|---|---|
| Deploy an update | SSH to VM → `git pull` → `docker compose -f docker-compose.prod.yml up -d --build` ([deployment.md §13](deployment.md)) | live (manual; CD workflow planned — ROADMAP "Process infrastructure") |
| Promote code | PR → `main` (CI + review gate) → test on workbench → fast-forward `prod` → deploy from `prod` | live |
| Logs / debugging | `docker compose logs -f backend` on the VM; JSON events greppable by `session_id`/`event` ([deployment.md §12](deployment.md)) | live |
| DB access | `psql` in the db container, or SSH port-forward for a GUI — 5432 is never public | live |
| Add / remove an admin | `python scripts/grant_admin.py <email>` run locally with the Firebase service-account key (custom claim `admin: true`; script refuses unverified emails) | spec'd (FR-28), lands with auth |
| Disable / re-enable a user | Admin page → disable: sets Firebase `disabled`, revokes refresh tokens; new sessions blocked immediately, API access dies ≤1h (FR-29) | spec'd (FR-29), lands with auth |
| Pause spend | `gcloud compute instances stop aloud --zone=us-west1-b` | live |

## 8. Decision log (condensed ADRs)

Full original rationale: `git show a84df1b:SDD.md` (§0).

| # | Decision | Why (and what was rejected) |
|---|---|---|
| 1 | Cascade pipeline, not speech-to-speech | Conversation state as text on our server → memory layer, provider swap, resume. Gemini Live prototype (old `main`, preserved as `prototype-gemini-live`) hit 32K audio-token context and no control |
| 2 | Pipecat for orchestration | Python like the backend; barge-in/turn-taking solved; swappable services; built-in metrics. Rejected: LiveKit Agents, fully custom (custom design preserved as SDD-v2 in history) |
| 3 | WebRTC transport (SmallWebRTC) | Target user is on a phone walking: graceful under packet loss + browser echo cancellation (barge-in depends on it). Rejected: raw WebSocket (TCP head-of-line blocking) |
| 4 | Deepgram Flux / Gemini Flash (thinking off) / Cartesia Sonic-3 | Each fastest-in-class for its stage; Flash thinking-on alone blows the 3s budget. LLM runner-up: Claude Sonnet — swap is one factory |
| 5 | Firebase Auth (Google + email/password, open signup) | Third-party IDP support wanted; standard JWT verification fits a separate FastAPI backend; free at this scale. Rejected: Supabase Auth, Auth.js, hand-rolled sessions (REQUIREMENTS §4.8) |
| 6 | Admin = Firebase custom claims | uid-bound (no email-matching trap), zero admin config in repo/env/DB, granted only via service-account script |
| 7 | Postgres + pgvector-later, single DB | user_id-scoped tables + RLS for isolation; sensitive content in dedicated columns for post-MVP encryption. No separate vector DB planned at this scale |
| 8 | Single VM, host networking, Caddy | Cheapest thing that runs real WebRTC (bridge networking can't forward ephemeral UDP); Caddy for zero-config TLS |
| 9 | Git: `main` = workbench, `prod` = deploy pointer; PR + CI + Claude review gate on `main` | Two-branch model matching a solo dev with a live demo |
| 10 | Everything in Docker; commit-at-checkpoints, push/PR only on confirmation | CLAUDE.md workflow rules |

## 9. Scaling notes (when the time comes)

In rough order of when they'd pay off:

1. **DB off the VM** → Cloud SQL or managed Postgres (backups, point-in-time
   recovery) — first move once user data matters.
2. **Secrets** → GCP Secret Manager (per-service access instead of one .env).
3. **TURN server** (rented: Twilio/Cloudflare/Metered, or coturn) — required
   for users on UDP-blocking networks; also the prerequisite for any
   multi-region story.
4. **Backend horizontal scaling** — sessions are stateful (in-memory pipeline
   per session), so scale-out needs session affinity or a session-per-pod
   model; the memory layer's design should keep per-session state
   externalizable. Managed WebRTC (Daily/LiveKit/Pipecat Cloud) is the
   buy-not-build alternative at that point.
5. **VM resize before any of the above**: e2-small → e2-medium is a 2-minute
   operation and the expected first response to memory-layer RAM pressure.
