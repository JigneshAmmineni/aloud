# Aloud — Software Design Document (v2: Fully Hand-Rolled Pipeline)

**Status:** Draft · **Branch:** `cascade-architecture` · **Date:** 2026-06-10
**Companion docs:** [REQUIREMENTS.md](REQUIREMENTS.md) · [SDD.md](SDD.md) (Pipecat version)

This is the alternative design where **no orchestration framework is used**. Same requirements, same provider choices (Deepgram Flux, Gemini Flash, Cartesia Sonic-3), but every piece a framework would provide — transport framing, audio capture/playback, turn-taking, barge-in cancellation, streaming concurrency, instrumentation — is built by hand on FastAPI + asyncio.

**Why this document exists:** maximum learning value. The hard parts of voice agents (the turn-taking state machine, cancellation races, audio buffering) are exactly the parts frameworks hide. Build this version when time permits; ship the SDD.md version when speed matters.

**What is identical to SDD.md and not repeated here:** agent behavior & prompt design (SDD §2.5), memory tiers & tools (SDD §2.6), DB schema (SDD §3), artifacts, privacy/encryption plan, traceability of those requirements.

---

## 0. Decision Record (deltas from v1)

| Decision | Choice | Rationale / tradeoff |
|---|---|---|
| Orchestration | Hand-rolled asyncio orchestrator | The point of v2. Weeks, not days; every race condition is ours. |
| Transport | **Raw WebSocket** (binary PCM + JSON control) | Hand-rolling WebRTC serving is a project in itself. WebSocket keeps the transport understandable. Cost: TCP head-of-line blocking on lossy mobile networks — audio stutters where WebRTC would conceal loss. Acceptable for a learning build; `aiortc` is the upgrade path. |
| Echo cancellation | Browser AEC via `getUserMedia` constraints | Same requirement as v1 (barge-in dies without it), but nothing supervises it for us — it must be verified manually per device. |

---

## 1. Architecture

```
┌──────────────────────── Browser (Next.js) ────────────────────────┐
│ AudioWorklet capture (16 kHz PCM16, 20 ms frames)                  │
│ Playback queue (AudioContext, scheduled buffers, flushable)        │
│ Button FSM · waveform (AnalyserNode on mic + playback)             │
└──────────────┬─────────────────────────────────────────────────────┘
               │ WebSocket: binary audio frames + JSON control msgs
┌──────────────▼──────────────── FastAPI ────────────────────────────┐
│ /ws — one SessionOrchestrator per connection                       │
│                                                                     │
│  client_rx ──► audio_q ──► stt_io ◄──wss──► Deepgram Flux           │
│      │                        │ turn events                         │
│      │                  ┌─────▼──────┐                              │
│      │                  │ TurnManager │  (the FSM, §4)              │
│      │                  └─────┬──────┘                              │
│      │                        │ run/cancel (generation counter)     │
│      │                  agent_runner: Gemini stream → sentence      │
│      │                    chunker → Cartesia wss → out_q            │
│      └── control msgs    out_q ──► client_tx ──► browser            │
│                                                                     │
│  SessionManager (resume grace) · MemoryService · TranscriptWriter   │
└──────────────┬──────────────────────────────────────────────────────┘
         PostgreSQL (schema: SDD.md §3)
```

One asyncio TaskGroup per session; tasks communicate only through bounded queues and the TurnManager. No task writes to the socket except `client_tx` (single-writer rule).

---

## 2. Wire Protocol (browser ↔ server)

Versioned, documented, and logged — this is the contract that makes the system debuggable. Binary frames are audio; everything else is JSON text frames `{v: 1, type, ...}`.

**Client → server:**

| Type | Payload | Notes |
|---|---|---|
| *(binary)* | 16 kHz mono PCM16, 20 ms = 640 B/frame | streamed continuously while session active |
| `session.start` | `{session_id?}` | absent = new; present = resume (§7) |
| `session.end` | — | graceful end |
| `ping` | `{t}` | RTT measurement for the dev overlay |

**Server → client:**

| Type | Payload | Notes |
|---|---|---|
| *(binary)* | 24 kHz mono PCM16 agent audio | client schedules into playback queue |
| `state` | `{state: listening\|thinking\|speaking}` | drives waveform UI (FR-6) |
| `playback.clear` | `{gen}` | **barge-in:** drop queue + stop current source NOW |
| `transcript` | `{role, text, final}` | dev overlay only; not a user-facing feature |
| `artifact.created` | `{id, title}` | FR-12 |
| `metrics` | `{turn_id, eot_to_first_audio_ms, stages: {...}}` | per-turn, feeds dev overlay |
| `session.resumed` / `error` | `{...}` | lifecycle |

---

## 3. Frontend Audio Engine (hand-rolled)

- **Capture:** `getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}})` → `AudioWorkletNode` resampling to 16 kHz PCM16, posting 20 ms frames → WebSocket. (AEC is non-negotiable: it's what keeps the agent's voice out of its own ears — FR-13.)
- **Playback:** incoming binary frames are converted to `AudioBuffer`s and scheduled back-to-back against `AudioContext.currentTime` with a small (~60 ms) jitter reserve. The queue object tracks every scheduled source so `playback.clear` can `stop()` the live source and drop the rest within one frame — this is what makes barge-in *feel* instant even though the server also stops sending.
- **Waveform (FR-6):** one `AnalyserNode` on the mic stream (listening color), one on the playback gain node (speaking color); thinking = pulsing dots between `state: thinking` and first audio frame.
- **Button FSM (FR-5):** identical to SDD.md §2.1, plus `reconnecting` driven by WebSocket close events.

---

## 4. TurnManager — the core state machine

This is the heart of the system and the main thing v2 teaches.

```
            user speaks                Flux EndOfTurn
   ┌────────────────────►USER_SPEAKING────────────────►THINKING
   │                          ▲                            │ first TTS audio queued
 IDLE                         │ Flux StartOfTurn           ▼
   ▲                          │ (BARGE-IN: cancel gen,  SPEAKING
   │   agent audio done       │  playback.clear)           │
   └──────────────────────────┴────────────────────────────┘
```

**States:** `IDLE` (session open, silence) · `USER_SPEAKING` · `THINKING` (EOT received, agent generating, no audio out yet) · `SPEAKING` (agent audio flowing).

**Inputs:** Flux turn events (`StartOfTurn`, `EndOfTurn`, optionally `EagerEndOfTurn`/`TurnResumed`), agent-runner events (`first_audio`, `done`, `failed`), client events (`session.end`).

**Rules that prevent the classic bugs:**

1. **Only the TurnManager changes state**, and every state change emits a `state` message to the client and a log line. No other task touches state.
2. **Generation counter.** Every agent run gets `gen = n+1`. Every audio frame queued to `out_q` is tagged with its gen. `client_tx` drops frames whose gen ≠ current. This closes the race where a cancelled TTS task has frames in flight: they arrive tagged stale and die in the queue, not in the user's ear.
3. **Barge-in** (`StartOfTurn` while `THINKING|SPEAKING`): bump gen → `cancel()` the agent task → send Cartesia its cancel message for the active context → send `playback.clear {gen}` → transition `USER_SPEAKING`. The new user turn's transcript then proceeds normally. Target: < 250 ms from speech onset to silence.
4. **Cancellation is cooperative and verified:** the agent task catches `CancelledError`, closes its Cartesia context, abandons the Gemini stream (no cancel API needed — just stop reading), logs `turn.interrupted` with how far it got.

---

## 5. Agent Runner (LLM → TTS chain)

Per turn (one task, cancellable as a unit):

1. Append final transcript to `ConversationContext` (a plain list of `{role, text}` — our own class, ~50 lines; full session history retained, FR-14).
2. Open Gemini stream (`google-genai` SDK, streaming generate with system prompt + context + tool schemas).
3. **Sentence chunker:** accumulate text deltas, split on sentence boundaries, feed each completed sentence to Cartesia immediately (NFR-2 — speak while still generating). First sentence latency is the metric that matters.
4. Tool-call handling: if Gemini returns a tool call (`create_artifact`, `recall_memories`, …), execute, append result, continue the stream loop. Tools are the same implementations as SDD.md §2.6.
5. Cartesia wss client streams PCM back; frames are tagged with gen and queued to `out_q`.

**Provider clients** (`stt/flux_client.py`, `llm/gemini_client.py`, `tts/cartesia_client.py`) are thin, hand-written wrappers, each behind a small ABC (`STTClient`, `LLMClient`, `TTSClient`) — C-2 holds in v2 as well: swapping a provider means writing one new client class, the orchestrator never changes. Each handles its own reconnect-with-backoff and surfaces structured connection-state logs.

---

## 6. Instrumentation (fully manual)

What Pipecat's observers gave us free in v1 is a `TurnClock` here — and building it is half the educational payoff.

```python
@dataclass
class TurnClock:
    turn_id: str
    t_first_user_audio: float | None = None
    t_eot: float | None = None            # Flux EndOfTurn received
    t_llm_request: float | None = None
    t_llm_first_token: float | None = None
    t_first_sentence: float | None = None
    t_tts_request: float | None = None
    t_tts_first_audio: float | None = None
    t_first_audio_sent: float | None = None   # left the server
```

Every stage stamps its slot; at turn end the orchestrator logs the derived durations (`stt_finalize_ms`, `llm_ttfb_ms`, `tts_ttfb_ms`, `eot_to_first_audio_ms`), writes `turn_metrics`, and sends the `metrics` message to the client dev overlay. Same thresholds as v1: **WARN > 1 s per stage (C-1), ERROR > 3 s total (NFR-1).** Same structured-log schema and boundary log points as SDD.md §4, written by hand at each queue boundary.

---

## 7. Lifecycle & Resume (FR-18/19, NFR-4)

Same design as SDD.md §2.8, mechanically simpler with WebSocket: on socket close, the orchestrator parks (tasks paused, provider connections closed, context retained) for `SESSION_RESUME_GRACE_S`; a reconnect with `session.start {session_id}` re-attaches, reopens Flux/Cartesia connections lazily, and replies `session.resumed`. Grace expiry → normal end path (tier-1 summary job fires).

---

## 8. Where the Bugs Will Live (read before debugging at 2 a.m.)

A field guide to the failure modes this architecture invites — each is a deliberate learning objective:

1. **Echo loop** — agent hears itself, barge-in fires forever. Cause: AEC off or playback routed outside the echo-cancelled path. First thing to check on every new device.
2. **Cancellation races** — stale TTS audio plays after barge-in. The gen counter (§4) is the defense; any frame path that skips tagging reintroduces it.
3. **Playback underrun** — choppy agent audio. Jitter reserve too small, or sentence chunks too short for Cartesia to stream smoothly.
4. **Backpressure** — unbounded queues hide a slow consumer until memory climbs. All queues bounded; a full queue logs WARN with its name.
5. **TCP head-of-line blocking** — on packet loss, *everything* stalls (audio and control share one socket). Symptom: bursts of late frames after a gap. This is the WebSocket tax; the fix is the aiortc upgrade, not tuning.
6. **Double-talk misclassification** — brief user backchannel ("mm-hm") triggers full barge-in. Tune via Flux thresholds / minimum-speech duration before treating `StartOfTurn` as interruption.
7. **Worklet/main-thread jank** — capture gaps when the tab repaints. Keep the worklet allocation-free; never touch the DOM from audio callbacks.

---

## 9. Effort & Comparison vs SDD.md (v1)

| | v1 Pipecat | v2 hand-rolled |
|---|---|---|
| Time to first conversation | days | weeks |
| Barge-in, turn-taking | framework-provided | §4, built + debugged by us |
| Per-stage metrics | built-in observers | TurnClock, built by us |
| Transport quality on mobile | WebRTC (loss-tolerant) | WebSocket (HOL blocking) |
| Provider swap (C-2) | factory one-liner | new ~150-line client class |
| Learning value | moderate | the whole point |
| Surface area we own forever | small | all of it |

Shared and identical across both: requirements coverage, prompts, memory design, DB schema, privacy plan. The two designs are deliberately swappable at the product level — same UI contract, same data model.

---

## 10. Build Order (suggested)

1. Wire protocol + echo server; browser audio engine talking to a loopback (you hear yourself) — proves capture/playback/AEC.
2. Flux client + TurnManager with a canned-response "agent" — proves the FSM and barge-in with zero LLM cost.
3. Real agent runner (Gemini → chunker → Cartesia).
4. TurnClock + structured logs + dev overlay.
5. Transcript writer, memory tiers, tools, artifacts (port directly from v1 design).
6. Resume grace. 7. Device matrix testing (AEC, iOS quirks).
