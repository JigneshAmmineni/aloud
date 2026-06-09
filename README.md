# Aloud

A voice-first journaling companion. You speak, an AI listens and asks one gentle guiding question to help you think more deeply. Not a therapist — a thinking partner.

---

## Status

**Sprint 1 — in progress.** Core voice loop is working for a single turn. Multi-turn (speaking again after the agent responds) is blocked by a Gemini Live API bug described below.

---

## Architecture

```
Browser (Next.js)
  │
  │  WebSocket (binary PCM + JSON signals)
  │
FastAPI (Python)
  │
  │  Gemini Live WebSocket (google-genai SDK)
  │
Gemini 2.5 Flash Native Audio (Vertex AI)
```

### Audio pipeline

| Direction | Format | Sample rate |
|---|---|---|
| Browser → Backend | 16-bit PCM, mono | 16 kHz |
| Backend → Gemini | 16-bit PCM blob, `audio/pcm;rate=16000` | 16 kHz |
| Gemini → Backend | 16-bit PCM | 24 kHz |
| Backend → Browser | raw PCM bytes | 24 kHz |

### Push-to-talk protocol

The browser buffers mic audio locally using an `AudioWorklet` (Float32 → Int16 PCM). On **Stop**:
1. Browser sends one binary WebSocket frame containing the full PCM blob
2. Browser sends `{"type": "end_of_turn"}` JSON frame
3. Backend sends `activity_start` → audio blob → `activity_end` to Gemini
4. Gemini streams audio response back as PCM chunks
5. Backend forwards each chunk as a binary WebSocket frame to the browser
6. When Gemini signals `turn_complete`, backend sends `{"type": "turn_complete"}` JSON to browser
7. Browser plays back audio, then returns to idle (WebSocket stays open for next turn)

The WebSocket connection maps 1:1 to a Gemini Live session. Ending the session (or reloading the page) closes both.

### Key files

| File | Purpose |
|---|---|
| `backend/agent/companion.py` | `CompanionAgent` — all Gemini Live logic lives here |
| `backend/routes/session.py` | FastAPI WebSocket endpoint |
| `frontend/lib/session.ts` | Browser-side session: mic capture, WS, playback, state machine |
| `frontend/app/page.tsx` | UI state machine (`disconnected → idle → recording → processing → speaking → error`) |
| `frontend/components/VoiceButton.tsx` | Visual states for each session status |
| `frontend/public/audio-processor.js` | AudioWorklet: Float32 → Int16 PCM |

---

## Running locally

**Prerequisites:** Docker Desktop, Google Cloud project with Vertex AI API enabled, Application Default Credentials.

```bash
# Authenticate
gcloud auth application-default login

# Copy and fill in env vars
cp .env.example .env

# Start everything
docker compose up --build
```

- Frontend: http://localhost:3000
- Backend: http://localhost:8000

---

## What's implemented (Sprint 1)

- [x] Next.js frontend with push-to-talk UI
- [x] FastAPI backend with WebSocket proxy to Gemini Live
- [x] `CompanionAgent` abstraction (provider-agnostic interface, Gemini internals isolated)
- [x] Browser mic capture via AudioWorklet at 16 kHz
- [x] Push-to-talk: audio buffered locally, sent as one blob on Stop
- [x] Agent audio response played back in browser at 24 kHz
- [x] `turn_complete` signaling from backend → browser (unblocks idle state)
- [x] Multi-state UI: connecting / idle / recording / processing / speaking / error
- [x] End Session button (explicit WebSocket teardown)
- [x] Docker Compose dev environment with hot reload
- [x] Gemini journaling companion system prompt

---

## Known issue: multi-turn bug

**Symptom:** Turn 1 works end-to-end. On Turn 2, the browser sends audio and `activity_end` to the backend, the backend forwards both to Gemini, but Gemini returns no response. The frontend stays stuck in "Thinking..." indefinitely.

**What we've ruled out:**
- Frontend state machine (correct — audio is sent, `processing` state is set)
- Backend forwarding (confirmed via logs — audio bytes and `activity_end` reach Gemini)
- Context compression / session resumption config (removed, issue persists)
- VAD conflict (switched from `audio_stream_end` to `activity_start`/`activity_end` with `automatic_activity_detection: disabled`, issue persists)

**Current hypothesis:** Gemini Live `gemini-live-2.5-flash-native-audio` may not reliably respond to `activity_end` after the first turn in a persistent session when audio is sent as a single large batch rather than streamed in real time. The model was designed for real-time streaming; push-to-talk with a single large blob may be hitting an undocumented edge case.

**Next step:** Build backend integration tests with pre-recorded PCM files to isolate exactly where the handshake breaks on Turn 2, and test alternative turn-signaling approaches.
