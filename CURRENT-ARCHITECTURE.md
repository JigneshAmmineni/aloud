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
   └─▶ Firebase Auth ─── identity (project aloud-c74f5): Google + email/
                          password sign-in; backend verifies Bearer ID tokens
                          via firebase-admin (REQUIREMENTS.md §4.8)

 Supporting systems: GitHub (repo, Actions CI, Claude PR review),
 Let's Encrypt (TLS via Caddy), Google Cloud (VM, IAP SSH; Cloud
 Logging/Monitoring — log shipping via the VM's Ops Agent, uptime check,
 email alerts).
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
| Auth | Firebase Auth | Google + email/password, open signup; Bearer ID tokens verified in `app/auth.py`; admin via `admin: true` custom claim; RLS on user-owned tables through a dedicated `aloud_app` Postgres role |
| Reverse proxy | Caddy 2 | TLS (Let's Encrypt), path routing; currently also the interim `basic_auth` gate |
| Dev/prod runtime | Docker Compose | everything runs in Docker — no host installs (CLAUDE.md rule) |

### Backend packages (`backend/requirements.txt`)

`pipecat-ai[webrtc,deepgram,google,cartesia]` (pipeline + provider services),
`fastapi`, `firebase-admin` (token verification + account admin),
`uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `pypdf` (PDF text
extraction), `python-dotenv`; dev/test: `pytest`, `aiosqlite`, `httpx`, `ruff`.

### Frontend packages (`frontend/package.json`)

`next`, `react`, `react-dom`, `firebase` (client auth),
`@pipecat-ai/client-js`, `@pipecat-ai/small-webrtc-transport`;
dev: `typescript`, `@types/*`.

### Provider accounts & usage dashboards

Where the money goes and where to check remaining credits/usage. (Feature 2 —
observability — will surface per-user consumption in-app; until then these
consoles are the source of truth.)

| Provider | Used for | Env key | Pricing model | Check usage at |
|---|---|---|---|---|
| Deepgram | STT + turn detection (Flux) | `DEEPGRAM_API_KEY` | pay-per-audio-minute against prepaid credits | [console.deepgram.com](https://console.deepgram.com) → Usage |
| Google Gemini | LLM (2.5 Flash, thinking off) | `GOOGLE_API_KEY` (AI Studio key) | per input/output token | [aistudio.google.com/usage](https://aistudio.google.com/usage); paid-tier detail also under [GCP → APIs → Generative Language API](https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/metrics) |
| Cartesia | TTS (Sonic-3) | `CARTESIA_API_KEY` | per-character credits on the subscription tier | [play.cartesia.ai](https://play.cartesia.ai) → Usage/Subscription |
| Firebase Auth | identity (project `aloud-c74f5`) | service-account JSON | free ≤ 50K MAU (email/password + Google) | [Firebase console → Authentication → Usage](https://console.firebase.google.com/project/aloud-c74f5/authentication/usage) |
| Google Cloud | the GCE VM, static IP, egress | — | ~$14–15/mo flat | [GCP Billing](https://console.cloud.google.com/billing) · [Compute Engine](https://console.cloud.google.com/compute/instances) |
| GCP Cloud Logging + Monitoring | shipped logs (Ops Agent), host metrics, uptime check, email alerts | — | $0 at current scale (free tiers: 50 GiB/mo log ingestion, 1M uptime executions/mo, agent metrics allotment); scales with log volume | [GCP Billing](https://console.cloud.google.com/billing) · [Logging usage](https://console.cloud.google.com/logs/usage) |
| Anthropic (Claude PR reviews + @claude) | the review on every push to an open PR, and @claude mentions | app token (GitHub secret) | per-review API/subscription usage — this is why pushes to open PRs are deliberate, per CLAUDE.md | [console.anthropic.com](https://console.anthropic.com) usage (or the Claude plan's usage page) |

The three voice providers bill per use and dominate cost at demo scale; the
VM is flat; Firebase, Cloud Logging/Monitoring, and GitHub Actions (public
repo) are $0 at this scale; Claude reviews bill per PR push. The domain is
~$10/yr (Cloudflare registrar; DNS free).

### Provider swap checklist

The provider seam makes the code change small — but a swap touches config,
money, docs, and environments too. When swapping ANY of STT / LLM / TTS
(or just changing models within a provider), walk every bullet:

**Code**
- [ ] `agent/providers.py` — the factory for that stage (the ONLY place SDK
  construction may live). New SDK = new extra on the `pipecat-ai[...]` line
  in `backend/requirements.txt` (Docker picks it up on rebuild).
- [ ] `backend/tests/test_providers.py` — the service-class assertions.
- [ ] Stage-specific couplings that do NOT swap automatically:
  - **STT**: `obs/latency.py`'s `FluxAwareLatencyObserver` and
    `companion.py`'s `ExternalUserTurnStrategies` both assume Flux does its
    own turn detection with no VAD frames — a VAD-based STT needs both
    revisited, and `FLUX_EOT_THRESHOLD` becomes dead config.
  - **TTS**: the sanitizer text filters sit in front of it;
    `CARTESIA_SPEED` / `CARTESIA_VOICE_ID` are Cartesia-only.
  - **LLM**: the thinking-off-for-latency decision (ADR #4) and prompt
    phrasing were tuned against Flash — re-evaluate both.
- [ ] `app/costs.py` — only if the new provider bills in different UNITS
  (the current math assumes STT $/minute, LLM $/1M tokens in+out, TTS
  $/1M characters). Same units, different prices = env change only.

**Config (BOTH the local `.env` AND the VM's `~/aloud/.env` — they are
separate hand-maintained files; deploys never sync them)**
- [ ] The provider selector / model env (`STT_PROVIDER`, `LLM_PROVIDER`,
  `LLM_MODEL`, `TTS_PROVIDER`) and the new provider's API key.
- [ ] **The matching `RATE_*` values — rates are the OLD provider's prices
  until you change them, and every admin cost figure quietly becomes wrong
  the moment the swap deploys.**
- [ ] `.env.example` — document the new keys, retire the dead ones.

**Docs (this file — the merge/deploy accuracy rule applies)**
- [ ] §1 system-context diagram and §2 tech-stack row.
- [ ] The provider table above: env key, pricing model, usage-console link
  (that link is where you verify the new bills).
- [ ] §8 decision log: note what replaced what, and why.

**After deploy**
- [ ] Confirm latency in the logs/admin (`turn.latency_breakdown` names the
  guilty stage if the new provider is slower — C-1's 1s/stage advisory).
- [ ] Check the OLD provider's console for residual spend, then retire its
  key from both `.env`s.

## 3. Repository layout

```
backend/
  app/        FastAPI app: main.py (routes, session wiring), auth.py (THE
              auth seam: token verification, admin ops), admin.py (/api/admin
              router), ratelimit.py (in-memory per-caller limiter),
              config.py, documents.py (upload-time doc store)
  agent/      companion.py (CompanionAgent: builds/runs one session's pipeline)
              providers.py (THE provider seam: all SDK construction)
              prompts.py, tools.py (create_artifact), sanitizer.py
  db/         engine.py (two engines + RLS bootstrap + user_scoped_session),
              models.py (users, sessions, transcript_events, artifacts,
              usage_events, turn_metrics), users_repo.py, sessions_repo.py
              (incl. the FR-32 boot sweep), transcript_log.py,
              batch_writer.py (shared NFR-10 background writer),
              admin_repo.py (FR-38 admin_scoped_session + cross-user reads)
  obs/        logging.py (JSON structured logs), latency.py (per-turn budget
              instrumentation: WARN >1s/stage, ERROR >3s end-to-end),
              usage.py (FR-32/33 usage + turn-metric capture)
  scripts/    grant_admin.py (mint/revoke the admin claim, local-only)
  tests/      SQLite-backed suite + Postgres-only RLS tests (test_rls.py)
frontend/     Next.js app: / (session console), /login, /admin (overview),
              /admin/users (+ /[uid]), /admin/sessions/[id];
              lib/firebase.ts + lib/auth.tsx (client auth)
.github/workflows/  ci.yml (ruff+pytest+Postgres service+build),
              claude.yml (@claude), claude-code-review.yml (auto-review)
Caddyfile, docker-compose.yml (dev), docker-compose.prod.yml (prod)
```

Two architectural seams everything hangs on:
- **Provider seam:** all STT/LLM/TTS SDK imports/construction live in
  `agent/providers.py` factories. Swapping a provider = one factory + env var.
- **Auth seam:** `app/auth.py` is the only auth-aware code — routes take
  identity from its dependencies, repos take `user_id: str` (no defaults) and
  never know where it came from, and every DB transaction is scoped through
  `user_scoped_session(user_id)` (RLS enforced by Postgres underneath).
  Cross-user reads exist only behind `db/admin_repo.py`'s
  `admin_scoped_session(admin)` — structurally admin-gated, transaction-local
  `app.is_admin`, DB-enforced read-only, and blind to content tables (FR-38).
  The single exception is the boot-time orphan sweep, which runs on the
  RLS-exempt bootstrap engine — legitimate only because it executes before
  the app serves traffic. That confinement is **structural, not
  conventional**: the lifespan retires the bootstrap engine right after the
  sweep (`retire_bootstrap_engine()`), and any later `bootstrap_session()`
  raises — request-path code *cannot* reach it.

## 4. Runtime view (one voice session)

1. Browser `POST /start` → session row created, `CompanionAgent` builds a
   dedicated pipeline (its own `LLMContext`, observers, transcript writer).
2. SDP offer/answer via `POST/PATCH /sessions/{id}/api/offer` (Caddy →
   backend; session-owned — the sessionless variant was removed), then WebRTC
   audio flows browser ↔ backend directly over UDP (bypasses Caddy).
3. Turn loop: Flux detects end-of-turn → transcript frame → LLM streams tokens
   → sentence-level TTS → audio streams out. Barge-in interrupts mid-response.
4. Observers off the hot path: latency breakdown per turn (structured logs +
   a `turn_metrics` row, FR-33), transcript rows batch-written to Postgres
   (ops log only — never injected into context, never user-facing), and
   usage capture (FR-32: LLM tokens + TTS characters from the pipeline's own
   metrics frames, stamped with the tracker's turn number). All three ride
   the same `BackgroundBatchWriter`: hot path enqueues, batches flush ~1s,
   failures log and drop (NFR-10).
5. Session ends (tap End / disconnect) → row closed, transcripts + usage
   flushed, STT seconds recorded as the session's audio duration (the
   streamed-time proxy). Sessions orphaned by a process death are closed as
   `interrupted` by the boot-time sweep, which also emits their inferred STT
   usage (FR-32).
6. Unexpected-death UX: while active, the client polls
   `GET /sessions/{id}/alive` every 5s (DB-backed truth — correct across
   restarts, crashes, media-timeout closes, and any future multi-VM setup);
   a dead answer or two missed polls drops the UI to idle with a persistent
   "connection lost" notice (artifacts kept). On graceful shutdown (SIGTERM
   — deploys/restarts) the backend sends a `session.ending` goodbye over the
   data channel and cancels live pipelines so their rows close as
   `interrupted` before the process exits.

## 5. Deployment & cloud infrastructure

**Everything runs on one GCE VM.** Full provisioning/ops detail: [deployment.md](deployment.md).

| Item | Value |
|---|---|
| Cloud | Google Cloud Platform |
| Compute | 1× GCE VM `aloud` — `e2-small` (2 vCPU shared, 2 GB RAM) + 2 GB swap, 20 GB disk, Debian 12 |
| Zone | `us-west1-b` (Oregon — close to user, protects the 3s latency budget) |
| IP / DNS | reserved static external IP; A record for `work-aloud.com` (Cloudflare DNS, grey-cloud/DNS-only — proxy would break UDP) |
| GCP services | Compute Engine; IAP (SSH tunnel — port 22 not public); Cloud Logging (container logs shipped by the VM's **Ops Agent**, JSON parsed into `jsonPayload`); Cloud Monitoring (host metrics from the same agent; `aloud-healthz` uptime check; email alert policies: uptime failure + root disk >80%). Nothing else |
| Firebase | project `aloud-c74f5` — Auth only (no Hosting/Firestore): Google + Email/Password providers enabled, email-link off. Service-account key: `backend/<name>.json` locally, `~/aloud/firebase-service-account.json` on the VM (mounted read-only into the backend container) |
| TLS | Let's Encrypt via Caddy, auto-renewed, cert persisted in a Docker volume |
| Firewall | tcp 80/443 (web+signaling) · udp 1–65535 (WebRTC media) · tcp 22 from IAP range only · **nothing else** (Postgres/backend ports unreachable from internet) |
| Cost | ~$14–15/mo (VM+disk+IP) + ~$10/yr domain + per-use provider APIs |

**On the VM — four containers** (`docker-compose.prod.yml`), all
`network_mode: host` because WebRTC binds unpredictable UDP ports that Docker
bridge networking can't forward:

| Container | Image | Role |
|---|---|---|
| caddy | caddy:2-alpine | :80/:443 — TLS, path routing (`/api/*,/start,/sessions/*,/healthz` → backend, rest → frontend). The old site-wide `basic_auth` gate is removed — per-user auth lives in the backend |
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
panel, document upload; redirects to /login when signed out) · `/login`
(FR-30: one form for sign-in/sign-up + Google) · admin pages (FR-41, all
URL-addressable, tab bar + breadcrumbs, admin claim required): `/admin`
(overview: live sessions, sessions/users today+7d, estimated spend, latency
p50/p95 + NFR-1 breaches, GCP link-outs) · `/admin/users` (searchable/
sortable/paginated user list + disable/enable) · `/admin/users/{uid}`
(session history) · `/admin/sessions/{id}` (per-turn latency + cost
drill-down — usage only, never content, NFR-9).

**Backend endpoints** (`backend/app/main.py`, `app/admin.py`): `GET /healthz`
(unauthenticated infra probe) · `POST /api/auth/email-check` (unauthenticated
by necessity, rate-limited — the signup availability pre-check, a documented
FR-26 enumeration exception) ·
`POST /documents` · `POST /start` (provisions the users row, mints the
session, `check_revoked`) ·
`POST|PATCH /sessions/{id}/api/offer` (WebRTC signaling, `check_revoked`,
session-ownership enforced; the sessionless `/api/offer` variant was removed
with the prebuilt debug client — it had no ownership to enforce) · `GET /sessions/{id}/alive` (session-scoped
liveness for the client's while-active poll; owner-scoped, DB-backed) ·
admin API (admin claim; cross-user reads via
FR-38's read-only `admin_scoped_session`): `GET /api/admin/users`,
`GET /api/admin/users/{uid}/sessions`, `GET /api/admin/sessions/{id}`,
`GET /api/admin/overview`, `POST /api/admin/users/{uid}/disable|enable`.
Plus the WebRTC UDP media path (not HTTP).

## 7. Operational processes (runbooks)

| Process | How | Status |
|---|---|---|
| Deploy / promote / roll back | Actions → **"Deploy to production"** → Run workflow. Blank ref = tip of `main`; an older main SHA = rollback (same button). Moves the `prod` pointer, IAP-tunnels to the VM (tunnel-only deployer SA), hard-resets + rebuilds, health-checks the site. Manual path still works ([deployment.md §13](deployment.md)). Rollback does not reverse DB migrations | live |
| Promote code | PR → `main` (CI + review gate) → test on workbench → run the deploy workflow (it moves `prod` itself) | live |
| Logs / debugging | Primary: [Logs Explorer](https://console.cloud.google.com/logs/query?project=aloud-498522) — the VM's Ops Agent ships every container's stdout, our JSON parsed into queryable fields. The on-call query: `logName="projects/aloud-498522/logs/docker_containers" AND jsonPayload.session_id="<id>"` (also filter `jsonPayload.event`, `severity>=ERROR`). Verified 2026-08-29 with spike containers. Local/immediate: `docker compose logs -f backend` on the VM still works ([deployment.md §12](deployment.md)) | live |
| Error triage | Exceptions with stack traces group in [Error Reporting](https://console.cloud.google.com/errors?project=aloud-498522) (verified: traceback-bearing ERROR entries create groups; plain one-line ERRORs — e.g. NFR-1 latency breaches — do NOT, by Error Reporting's format rules: find those in Logs Explorer `severity>=ERROR`, and breach counts on the `/admin` overview) | live |
| DB access | `psql` in the db container, or SSH port-forward for a GUI — 5432 is never public | live |
| Add / remove an admin | from `backend/`: `python scripts/grant_admin.py <email>` (or `--revoke`) with the service-account key present — sets the custom claim, refuses unverified emails, revokes the target's tokens so it lands promptly | live |
| Disable / re-enable a user | `/admin/users` page → disable: sets Firebase `disabled` + revokes refresh tokens; new sessions blocked immediately, other API access dies ≤1h, a live session survives until it ends (FR-29) | live |
| "User X says it broke at 3pm" | `/admin/users` → the user → their sessions (end reason, latency, usage) → the session's per-turn table; for stack traces, Logs Explorer filtered by the `session_id` (FR-36/39) | live (in-app half) |
| Usage / cost review | `/admin` overview (spend 7d/30d, estimated at current `.env` rates — provider consoles are the invoice truth, FR-34); per-user costs on `/admin/users` | live |
| Ship logs off the VM | GCP Ops Agent (v2.70) on the VM tails `/var/lib/docker/containers/*/*-json.log`; config at `/etc/google-cloud-ops-agent/config.yaml` (parse Docker wrapper → parse inner app JSON → promote `severity`). The `gcplogs` Docker driver was spiked and rejected — it ships our JSON as an unparsed string. Reinstall/reconfigure: rerun the setup (idempotent). **Pre-deploy check (owner-run — Claude never opens `.env`):** the VM's `~/aloud/.env` must NOT pin `LOG_LEVEL=DEBUG` (an explicit line beats the INFO code default and would ship transcript text to Cloud Logging, NFR-9). Fix from your machine: `gcloud compute ssh aloud --zone=us-west1-b --tunnel-through-iap --command="sed -i '/^LOG_LEVEL=/d' ~/aloud/.env"` — deletes the line so the INFO fallback rules; takes effect at the next deploy | live (installed 2026-08-29); .env checked & cleaned 2026-08-30 (a stale `LOG_LEVEL` line existed and was deleted via content-blind `sed`) |
| Uptime alerting | Cloud Monitoring uptime check `aloud-healthz` (HTTPS `work-aloud.com/healthz`, all regions, 5-min cadence) → alert policy "Uptime failure" → email channel (admin gmail). Disk: "Disk > 80%" policy on `agent.googleapis.com/disk/percent_used` (`/dev/sda1`, state=used, 15-min sustained), same channel (FR-40). Fire-test: verified 2026-08-29 — the check initially shipped with a shell-mangled path, genuinely failed from all regions, and the failure email arrived within minutes; path fixed, incident resolved | live |
| First prod deploy of auth | copy the service-account JSON to `~/aloud/firebase-service-account.json` (chmod 600) on the VM before `docker compose -f docker-compose.prod.yml up -d --build` | one-time |
| Pause spend | `gcloud compute instances stop aloud --zone=us-west1-b` | live |

### The deploy button, in detail

What happens, start to finish, when you click **Actions → "Deploy to
production" → Run workflow** (`.github/workflows/deploy.yml`):

1. **Resolve & validate.** The workflow takes your `ref` input (blank = tip
   of `main`) and checks `git merge-base --is-ancestor <SHA> main`: the
   commit must be *on main's history*. This is the gate-integrity rule —
   nothing that skipped PR review/CI can reach production, and no arbitrary
   branch can be deployed.
2. **Reach the VM.** The runner authenticates to GCP as the
   `aloud-deployer` service account and opens an **IAP tunnel** to the VM's
   port 22 (the same Google-authenticated path used for manual SSH — port 22
   is never open to the internet). It then SSHes through the tunnel as the
   normal VM user (`jigne`) using a dedicated deploy key.
3. **Update the box.** On the VM, first disk hygiene: `docker image prune -f`
   + `docker builder prune -f --keep-storage=4GB`. Run *before* the build on
   purpose: the outgoing release is still tagged and running at that moment,
   so only images at least two releases old (untagged, unused) qualify —
   deploying vN keeps vN-1's layers around and sweeps vN-2 and older. Both
   prunes are garbage collectors (in-use/tagged images skipped, nothing-to-do
   = clean no-op), so the step is idempotent. Then `git fetch`,
   `git checkout prod`, `git reset --hard <SHA>` — *reset*, not *pull*,
   because a pull can only move forward; reset makes the working copy exactly
   the chosen commit in either direction (this is what makes rollback the
   same button). Then `docker compose -f docker-compose.prod.yml up -d
   --build`: only images whose inputs changed rebuild; Postgres data and the
   TLS cert live in named volumes and are untouched.
4. **Verify.** The workflow polls `https://work-aloud.com/healthz` until it
   returns 200 (or fails the run with a rollback hint after ~2 minutes).
5. **Move the `prod` pointer and tag the deploy — last, only after
   verification.** `git push origin +<SHA>:refs/heads/prod` — the `+` is a
   force-push, deliberately: `prod` is not a line of development, it's a
   **bookmark meaning "this exact commit is verified live."** Deploying
   forward moves it forward; rolling back moves it backward; a failed run
   leaves it untouched. Either way, `git log prod` on any machine tells you
   what production is running. The same step pushes an immutable
   `deploy-YYYYMMDD-HHMMSS` (UTC) tag at the commit — the pointer's
   **permanent history**: `git tag -l 'deploy-*'` lists every verified
   deploy ever, which is where you find the SHA to roll back to (the
   Actions run history has the richer per-deploy record — logs, health
   check — but its retention is a rolling window; tags are forever).

**The pieces and where they live:**

| Piece | What it is | Where |
|---|---|---|
| `deploy.yml` | the workflow itself (steps above) | `.github/workflows/`, versioned like all code |
| `aloud-deployer` | GCP service account, deliberately minimal: `iap.tunnelResourceAccessor` + `compute.viewer` ONLY — it can open the tunnel but cannot modify, stop, or reconfigure the VM | GCP IAM (project `aloud-498522`) |
| `GCP_DEPLOYER_SA_KEY` | that service account's JSON key | GitHub repo secret |
| `VM_SSH_PRIVATE_KEY` | a dedicated ed25519 deploy key (generated for CD, not your personal key) | GitHub repo secret; its public half is one line in `~/.ssh/authorized_keys` on the VM (comment `aloud-cd-deploy`) |
| Concurrency guard | `concurrency: production-deploy` — two clicks can't deploy simultaneously | in the workflow |
| GCP services involved | Compute Engine (the VM) + IAP (the tunnel) — nothing new was provisioned for CD beyond the service account | — |

**Rollback**: same button, paste an older `main` SHA — find it with
`git tag -l 'deploy-*'` (every tag = a health-checked deploy, timestamped
UTC in the name). Two caveats: DB schema
migrations are **not** reversed (rolling back past a release that changed the
schema may need manual DB attention first), and in-flight voice sessions die
when the backend container restarts (true of every deploy).

**Revoking CD access** if a secret ever leaks: delete the two GitHub secrets,
delete the `aloud-deployer` SA (GCP console → IAM → Service Accounts), and
remove the `aloud-cd-deploy` line from the VM's `~/.ssh/authorized_keys`.

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
