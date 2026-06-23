# Aloud — Software Design Document (v1: Pipecat Cascade)

**Status:** Draft · **Branch:** `cascade-architecture` · **Date:** 2026-06-10
**Companion docs:** [REQUIREMENTS.md](REQUIREMENTS.md) · [SDD-v2.md](SDD-v2.md) (fully hand-rolled alternative)

This document describes how every requirement in REQUIREMENTS.md is implemented using a **cascaded voice pipeline** (turn detection → STT → LLM → TTS as separate streaming stages) orchestrated by **Pipecat**.

---

## 0. Decision Record

| Decision | Choice | Alternatives considered | Rationale |
|---|---|---|---|
| Architecture | Cascade (STT → LLM → TTS) | Speech-to-speech (Gemini Live) | Conversation state is text on our server: fixes the audio-context memory problem hit on `main`, enables provider swap (C-2) and the deferred cross-session memory layer. 3s budget (NFR-1) absorbs the extra latency. |
| Orchestration | **Pipecat** (v1.x, Python) | LiveKit Agents, custom FastAPI | Pipeline-first and Python like our backend; barge-in/turn-taking solved; 60+ swappable services (= C-2); built-in per-stage metrics + OpenTelemetry (our observability requirement); no extra media server to operate. |
| Transport | **WebRTC** (Pipecat SmallWebRTC) | Raw WebSocket | Target user is on a phone, walking: WebRTC degrades gracefully under packet loss and integrates with browser echo cancellation, which barge-in (FR-13) depends on. No Daily/LiveKit account needed. |
| STT | **Deepgram Flux** | ElevenLabs Scribe v2 RT, AssemblyAI | Purpose-built for voice agents: transcription **and** end-of-turn detection in one model, lowest end-of-speech latency in current benchmarks. |
| LLM (default) | **Gemini 2.5 Flash, thinking disabled** (`GoogleLLMService`) | Claude Sonnet 4.6 / Haiku 4.5, GPT-x | Fastest + cheapest; GCP billing already set up. Thinking is disabled — with it on, Flash TTFB (~1.9 s) blows the §5 budget on its own. Downgrade to Flash-Lite later only with side-by-side testing. Swappable in one factory function if probing-question quality disappoints (see §11 Risks). |
| TTS | **Cartesia Sonic-3** | ElevenLabs Flash v2.5 | ~90–190 ms to first audio, built for realtime agents, cheaper at scale, natural enough for FR-4. |
| DB | PostgreSQL | — | Already chosen; schema designed for post-MVP encryption (NFR-6). |
| Frontend | Next.js + Pipecat JS client SDK | — | Existing stack; SDK handles transport, tracks, and bot/user speaking events. |

---

## 1. System Overview

```
┌────────────────────────── Browser (Next.js) ──────────────────────────┐
│  Mic (AEC on) ─┐                                       ┌─ Speaker     │
│                ├─ PipecatClient + SmallWebRTCTransport ─┤              │
│  Session UI: button FSM (FR-5) · waveform bar (FR-6) · artifacts list │
└───────────────┬───────────────────────────────────────────────────────┘
                │  HTTPS: POST /api/offer (SDP signaling)
                │  WebRTC: audio both ways + data channel (JSON events)
┌───────────────▼────────────────── FastAPI backend ─────────────────────┐
│  SessionManager — lifecycle, registry (session_id → state)              │
│                                                                         │
│  Per session: Pipecat Pipeline                                          │
│   transport.input() → STT (Deepgram Flux) → user ctx aggregator         │
│     → LLM (Gemini Flash) → text sanitizer → TTS (Cartesia)              │
│     → transport.output() → assistant ctx aggregator                     │
│   observers: UserBotLatencyObserver · transcript tap (FR-20)            │
│                                                                         │
│  ArtifactService — written artifacts (FR-12)                            │
└───────────────┬─────────────────────────────────────────────────────────┘
                │
        ┌───────▼────────┐   external APIs: Deepgram (wss) · Google (https)
        │   PostgreSQL   │                  Cartesia (wss)
        └────────────────┘
```

One Pipecat pipeline runs per active session. All provider-specific construction is isolated in one module (§2.4), satisfying C-2.

---

## 2. Component Design

### 2.1 Frontend

Single-page app. Components: `SessionButton`, `WaveformBar`, `ArtifactsPanel` (minimal list, appears only when an artifact exists).

**Client connection** (`@pipecat-ai/client-js` + `@pipecat-ai/small-webrtc-transport`):

```ts
const client = new PipecatClient({
  transport: new SmallWebRTCTransport({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] }),
  enableMic: true,
  enableCam: false,
  callbacks: { /* state + track events drive the UI, see below */ },
});
await client.connect({ webrtcUrl: "/api/offer" });
```

**Button state machine (FR-5):**

| State | Render | Transition |
|---|---|---|
| `idle` | green "Talk" | tap → `connecting`, call `client.connect()` |
| `connecting` | grey, disabled | transport connected → `active`; error → `idle` + toast |
| `active` | red "End" | tap → `ending`, call `client.disconnect()` |
| `ending` | grey, disabled | disconnected → `idle` |

**Waveform bar (FR-6):** one component, three modes, driven by SDK callbacks:

- **Listening** (default while active): `AnalyserNode` on the local mic track → amplitude bars.
- **Thinking**: entered on the user-stopped-speaking event; rendered as three pulsing dots.
- **Speaking**: entered on bot-started-speaking; `AnalyserNode` on the remote (bot) audio track → bars in a distinct color. Exits to Listening on bot-stopped-speaking.

**Echo cancellation (critical for FR-13):** mic is captured with `echoCancellation: true, noiseSuppression: true, autoGainControl: true` (WebRTC defaults — verify, don't assume). Without AEC the mic hears the agent's own voice and barge-in self-triggers.

**Known platform limitation (flagged in REQUIREMENTS review):** iOS kills web-app audio when the screen locks or Safari is backgrounded. UC-5 works screen-on only; a native wrapper is the only fix and is out of scope.

### 2.2 Signaling & transport

- Signaling follows the Pipecat client contract (three routes, all in `app/main.py`): `POST /start` (session bootstrap, returns `sessionId`), `POST /api/offer` (SDP exchange via `SmallWebRTCRequestHandler`, also at `/sessions/{id}/api/offer` — the path Pipecat clients use after `/start`), and `PATCH /api/offer` (trickle ICE candidates). The frontend always goes through the session-scoped path (`/start` then `/sessions/{id}/api/offer`) so a session can carry attached-document ids; the unscoped `/api/offer` stays for the prebuilt debug UI.
- **Document upload (FR-21):** `POST /documents` (multipart) accepts a `.txt`/`.md`/`.pdf`, extracts its text (`app/documents.py`; PDF via `pypdf`), stashes it in an ephemeral per-process `DocumentStore`, and returns `{id, filename, char_count}`. The frontend passes the chosen ids in the `/start` body (`{body: {document_ids: [...]}}`); `session_offer` resolves them via the store and hands the `Document`s to `CompanionAgent`. The store's `add`/`get` surface is the swap point for a DB-backed document repo when the memory layer lands (§2.6).
- **The MVP demo runs on localhost** (browser and backend on the same machine / LAN), so NAT traversal is a non-issue. STUN-only config stays for LAN testing; TURN and deployment networking are deferred until the app moves off localhost (§11).

### 2.3 The pipeline

```python
stt = DeepgramFluxSTTService(
    api_key=settings.DEEPGRAM_API_KEY,
    settings=DeepgramFluxSTTService.Settings(eot_threshold=settings.FLUX_EOT_THRESHOLD),
)
llm = GoogleLLMService(
    api_key=settings.GOOGLE_API_KEY,
    settings=GoogleLLMService.Settings(
        model=settings.LLM_MODEL,    # gemini 2.5 flash
        # thinking OFF — it adds ~1s+ of TTFB (§5)
        thinking=GoogleLLMService.ThinkingConfig(thinking_budget=0),
    ),
)
tts = CartesiaTTSService(
    api_key=settings.CARTESIA_API_KEY,
    settings=CartesiaTTSService.Settings(
        model="sonic-3",
        voice=settings.CARTESIA_VOICE_ID,
        generation_config=GenerationConfig(speed=settings.CARTESIA_SPEED),  # 1.0 = normal
    ),
    text_filters=make_text_filters(settings.TTS_SANITIZE_ENABLED),  # sanitizer, see below
)

context = LLMContext(tools=tool_schemas)          # in-session memory (FR-14)
user_agg, assistant_agg = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
)

pipeline = Pipeline([
    transport.input(),
    stt,
    user_agg,
    llm,
    tts,
    transport.output(),
    assistant_agg,
])

task = PipelineTask(
    pipeline,
    params=PipelineParams(
        enable_metrics=True,
        enable_usage_metrics=True,
        observers=[latency_observer, transcript_observer],
    ),
    enable_tracing=settings.OTEL_ENABLED,
    enable_turn_tracking=True,
    conversation_id=session_id,
)
```

**Turn detection (FR-3):** Flux detects end-of-turn natively. `eot_threshold` (env `FLUX_EOT_THRESHOLD`, default 0.8; library default 0.7) is the end-of-turn *confidence* required — higher = softer turn-taking, waiting through brief pauses before replying. It's confidence-based, not a silence timer, and it has a cliff: set it too high (0.9 was perpetual-listening in testing) and Flux never gets confident enough to end the turn. Nudge up in small steps; tune by ear. Flux manages its own turn events, so the pipeline uses `ExternalUserTurnStrategies` per Pipecat's Flux docs — no separate VAD needed. `eager_eot_threshold` (speculative early end-of-turn → speculative LLM call) is a latency optimization left **off** initially; enable behind a config flag if measured EOT latency is the budget's long pole.

**Barge-in (FR-13):** Flux emits start-of-turn when the user speaks over the bot; Pipecat's interruption handling cancels the in-flight LLM and TTS generation and flushes queued output audio. This is framework-provided; our only job is to keep AEC working (§2.1) so it doesn't false-trigger.

**TTS text sanitization:** prompt instructions alone won't stop the LLM leaking markdown (asterisks, list markers, headings) into its output, and the TTS reads those characters aloud. The sanitizer (`agent/sanitizer.py`) is implemented as Pipecat TTS *text filters* rather than standalone frame processors: streaming LLM frames can split a token (e.g. `**`) across two frames, while text filters run on sentence-aggregated text where rewriting is reliable. Two filters: `MarkdownTextFilter` (strips markdown; toggleable via `TTS_SANITIZE_ENABLED` for A/B listening) and `IdentifierTextFilter` (always on — turns snake_case identifiers like `create_artifact` into spoken words so the voice doesn't say "create underscore artifact").

### 2.4 CompanionAgent & provider isolation (C-2)

```
backend/
  app/main.py            # FastAPI app, routes: /documents, /start, /api/offer, /healthz
  app/documents.py       # document text extraction + ephemeral DocumentStore (FR-21)
  agent/companion.py     # CompanionAgent: builds + runs one session's pipeline
  agent/providers.py     # THE swap point: make_stt(), make_llm(), make_tts()
  agent/prompts.py       # system prompt builder (identity, spoken style)
  agent/sanitizer.py     # TTSTextSanitizer: strips markdown before TTS (§2.3)
  agent/tools.py         # LLM tools: create_artifact
  db/                    # SQLAlchemy models; create_all at boot (Alembic when schema churns)
  obs/                   # logging setup, latency observer wiring
```

`agent/providers.py` is the **only** file that imports provider SDK service classes. Each factory reads its provider name from config (`STT_PROVIDER=deepgram_flux`, `LLM_PROVIDER=google`, `TTS_PROVIDER=cartesia`) and returns a Pipecat service. Swapping Gemini → Claude is: add a branch in `make_llm()`, set an env var. Session logic, memory, routes, prompts untouched.

### 2.5 Agent behavior (FR-7, FR-9, FR-11, FR-12)

The system prompt is assembled per session by `prompts.py` from blocks:

1. **Identity** — a thinking partner that sharpens the user's ideas. Hard ban (C-3): the words therapist/therapy/counselor/mental-health never appear in any prompt block.
2. **Spoken-output style** — responses are read aloud by TTS: short sentences, no markdown, no lists, no emoji. One question at a time (FR-9); never stack questions or volunteer lists of suggestions. The sanitizer (§2.3) is the enforcement backstop for the no-markdown rule.
3. **Attached documents (FR-21)** — when the user attached documents before the session, their extracted text is appended as a second `system` message after the identity/style blocks (`build_document_context_block`), instructing the agent to acknowledge them in its greeting and reference them by name. With no documents, the context is just the base prompt — the FR-20 guard test (`test_pipeline_setup`) pins this so injection never happens by accident.

FR-8 (proactive flagging) and FR-10 (modes) are demo stretch goals (REQUIREMENTS §6); each returns as one additional prompt block when picked back up.

- **FR-11 (session summary on request):** in-context ability of the LLM; no machinery.
- **FR-12 (written artifact):** LLM tool `create_artifact(title, kind, content)` → row in `artifacts` + `artifact.created` event over the data channel → frontend shows it in `ArtifactsPanel`. The agent confirms verbally ("I've written that up — it's on your screen").

### 2.6 Memory (FR-14)

- **In-session (FR-14):** the `LLMContext` holds the full text history of the session. Text is cheap; no truncation within a session.
- **Cross-session memory is deferred** (REQUIREMENTS §6): the planned direction is a MemGPT-style framework, possibly RAG with semantic vector search, processed in parallel while the user is still speaking. Nothing in this build depends on it; tables are added when it lands.
- **Attached documents (FR-21):** ephemeral for the MVP — held in the in-process `DocumentStore` (`app/documents.py`) and injected into the session's `LLMContext` at startup, never persisted. The `add`/`get` surface is where a DB-backed repo (a `documents` table FK'd to the session, mirroring `sessions_repo.py`) slots in when cross-session memory lands, so documents persist alongside the conversation. The schema is untouched until then.
- **FR-20 compliance:** raw transcripts are never injected into context.

### 2.7 Transcript ops log (FR-20)

A pipeline observer taps final user transcriptions and assistant output text and enqueues `transcript_events` rows. A background writer task batches inserts — **DB latency must never sit on the audio path**. Not user-facing; no API exposes it.

### 2.8 Session lifecycle (FR-18)

`SessionManager` keeps `session_id → {pipeline task, LLMContext, status}`.

- **Start:** tap → `/api/offer` → create session row + pipeline → SDP answer → active.
- **End (user tap):** graceful teardown → mark session ended.
- **Drop:** a transport disconnect ends the session the same way (`end_reason = disconnect`). Resume-after-drop is deferred (REQUIREMENTS §6); the user starts a new session.

---

## 3. Data Model (PostgreSQL)

Sensitive content columns (🔒 = encrypt post-MVP, NFR-6) are kept separate from metadata, per the standing schema constraint.

```sql
users            (id, created_at)                       -- auth TBD; MVP runs single-user

sessions         (id, user_id, started_at, ended_at, status,        -- active|ended
                  end_reason)                                       -- user|disconnect|error

transcript_events(id, session_id, ts, role,             -- user|agent
                  kind,                                 -- final_transcript|agent_text|event
                  text 🔒, turn_id, latency_ms)

artifacts        (id, session_id, user_id, created_at, kind,
                  title 🔒, content 🔒)

turn_metrics     (id, session_id, turn_id, ts,          -- optional, see §4
                  eot_to_first_audio_ms, stt_ms, llm_ttfb_ms, tts_ttfb_ms)
```

- Cross-session memory tables (session summaries, user digest) and `user_settings` (proactivity toggle) arrive with the deferred features (REQUIREMENTS §6).
- Encryption plan (deferred, NFR-6): app-level AES-GCM (or pgcrypto) on 🔒 columns only; all timestamps/IDs/status stay plaintext so queries and ops dashboards keep working.
- **NFR-7:** `DELETE /api/me/data` — cascading hard delete across all tables for the user, plus a log-scrubbing note in the privacy policy (transcript text appears in logs only at DEBUG, §4).

---

## 4. Observability — data flow & latency instrumentation

Design principles: **every boundary crossing is loggable; every stage is timed; one `turn_id` ties a user turn together end-to-end.**

**Correlation IDs:** `session_id` = Pipecat `conversation_id`; `turn_id` comes from `enable_turn_tracking=True`. Both appear on every log line and span.

**Structured logs** (JSON via loguru sink): `{ts, level, session_id, turn_id, component, event, duration_ms, data}`. Transcript/LLM text is logged **at DEBUG only**; INFO logs carry lengths and counts, not content (keeps sensitive text out of routine log retention).

**Boundary log points:**

| Boundary | Event (INFO) | Fields |
|---|---|---|
| Browser → server | `transport.connected / .disconnected` | ice state, resume? |
| STT → pipeline | `stt.final_transcript` | char count, ms since turn start |
| Pipeline → LLM | `llm.request` | context turns, est. tokens, tools enabled |
| LLM → pipeline | `llm.first_token` / `llm.complete` | ttfb_ms, output chars, tool calls |
| Pipeline → TTS | `tts.request` | sentence count |
| TTS → transport | `tts.first_audio` | ttfb_ms, audio seconds generated |
| Tools | `tool.invoked` | tool name, duration_ms |
| Barge-in | `turn.interrupted` | ms into bot response |

**Per-turn latency:** `UserBotLatencyObserver` with an `on_latency_breakdown` handler logs the full chronological breakdown per turn (service TTFBs, aggregation latency, turn duration) and optionally writes `turn_metrics`. Two threshold alerts, straight from the requirements: **WARN when any stage > 1s (C-1); ERROR when end-of-speech → first audio > 3s (NFR-1).** A latency regression therefore names its guilty stage in the logs without any extra tooling.

**Tracing (optional, dev):** `setup_tracing()` + `enable_tracing=True` exporting OTLP to a Jaeger container in docker-compose. Off in the default config; metrics + logs above are the baseline.

---

## 5. Latency Budget (NFR-1, NFR-2, C-1)

| Stage | Target | Measured by |
|---|---|---|
| Flux end-of-turn detection | ≤ 500 ms | turn breakdown |
| STT final transcript | ≤ 300 ms | `stt.final_transcript` |
| LLM first token | ≤ 700 ms | `llm.first_token` |
| TTS first audio | ≤ 300 ms | `tts.first_audio` |
| Transport + playout | ≤ 200 ms | client-side dev overlay |
| **End-of-speech → first audio** | **≤ 1.8 s typical / 3 s hard (NFR-1)** | latency observer |

The LLM ≤ 700 ms target assumes Gemini **thinking is disabled** (§0, §2.3); with default thinking on, Flash TTFB is ~1.9 s and blows the budget on its own.

NFR-2 (stream, don't wait) is inherent: LLM tokens stream into TTS sentence-by-sentence, TTS audio streams to the transport as generated.

---

## 6. Error Handling

- **Provider connection failure mid-session** (Deepgram/Cartesia wss drop): Pipecat services reconnect; if a turn is lost, log ERROR with turn_id. Repeated failure → end session with UI error toast.
- **LLM timeout:** `on_completion_timeout` handler → log ERROR, agent stays silent rather than blocking the pipeline; user naturally re-prompts.
- **Backend crash:** sessions are lost (in-memory pipelines), but transcripts up to the last batch write survive.

---

## 7. Configuration

All via environment (documented in `.env.example`, never read from `.env` by tooling):

```
DEEPGRAM_API_KEY=            CARTESIA_API_KEY=           GOOGLE_API_KEY=
CARTESIA_VOICE_ID=           LLM_MODEL=<gemini 2.5 flash model id>
STT_PROVIDER=deepgram_flux   LLM_PROVIDER=google         TTS_PROVIDER=cartesia
DATABASE_URL=postgresql://...
TTS_SANITIZE_ENABLED=true    EAGER_EOT_ENABLED=false
CARTESIA_SPEED=0.85          FLUX_EOT_THRESHOLD=0.8
OTEL_ENABLED=false           OTEL_EXPORTER_OTLP_ENDPOINT=
```

---

## 8. Testing Strategy

- **Unit:** prompt builder blocks (incl. C-3 word-ban test), tools, sanitizer (markdown in → clean spoken text out).
- **Pipeline integration:** Pipecat pipeline with mocked STT/LLM/TTS services — assert frame ordering, interruption cancels generation, transcript observer writes rows.
- **Latency:** scripted session against real providers; assert `on_latency_breakdown` totals < 3 s (run manually / pre-release, not in CI).
- **Manual voice script:** in-session recall of earlier details, barge-in mid-sentence, artifact request, sanitizer on/off A-B listen.

---

## 9. Requirements Traceability

| Req | Where |
|---|---|
| FR-1, FR-2 | §2.1 client, §2.2 one-tap connect |
| FR-3 | §2.3 Flux turn detection |
| FR-4 | §0 TTS choice (Sonic-3) |
| FR-5 | §2.1 button FSM |
| FR-6 | §2.1 waveform modes |
| FR-7, FR-9 | §2.5 prompt blocks |
| FR-11, FR-12 | §2.5; artifacts table §3 |
| FR-13 | §2.3 barge-in + §2.1 AEC |
| FR-14 | §2.6 LLMContext |
| FR-18 | §2.8 lifecycle |
| FR-20 | §2.7 transcript observer; §2.6 non-injection |
| NFR-1, NFR-2 | §5 budget; streaming pipeline |
| NFR-3 | web app; §2.1 mobile notes |
| NFR-5, NFR-7 | §3 deletion endpoint; privacy disclosure |
| NFR-6 | §3 sensitive-column separation (encryption itself deferred) |
| C-1 | §4 per-stage WARN threshold |
| C-2 | §2.4 providers.py |
| C-3 | §2.5 block 1 + word-ban test |

Deferred requirements (FR-8, FR-10, FR-15–FR-17, FR-19, NFR-4) are listed in REQUIREMENTS §6 Out of Scope.

---

## 10. Build Order (suggested)

1. Pipeline skeleton: offer endpoint + pipeline with the three services + sanitizer, hardcoded prompt — talk to it end-to-end.
2. Button FSM + waveform UI.
3. Observability: metrics observer, structured logs, thresholds.
4. Transcript ops log + DB models.
5. Barge-in verification on real phones (AEC behavior differs per device).
6. Artifacts.

---

## 11. Risks & Open Items

- **Gemini 2.5 Flash (thinking disabled) probing-question quality** — the product *is* question quality. If it underwhelms, the swap lever is `make_llm()` (Claude Sonnet was the runner-up). Downgrade to Flash-Lite only with side-by-side testing. Decide after dogfooding, not benchmarks.
- **Barge-in depends on browser AEC** — known to be flaky on mobile Safari speakerphone/Bluetooth. Troubleshoot at build step 5 on real phones.
- **iOS screen lock** kills web audio — UC-5 is screen-on only. Native wrapper is the eventual fix; out of scope now.
- **Deployment networking** — demo is localhost; moving to a deployed environment needs a host with publicly reachable UDP (or a managed WebRTC transport) and likely a TURN server (§2.2).
- **Flux pricing/quotas** at sustained usage — verify before any public launch.
- **Eager EOT** (speculative LLM calls) trades cost for latency — only if measurements demand it.
- **Auth is TBD** (Supabase Auth vs Auth.js); MVP runs single-user with a stub user row. Schema already keys everything by `user_id`.
