# Aloud

**A voice-first thinking partner for people who process ideas best by talking out loud.**

You tap one button and start talking. Aloud listens, asks sharp questions — one at a time — and helps you brainstorm, pressure-test plans, and untangle messy thoughts. Ask it to *"write that up"* and a structured artifact appears on screen while the conversation keeps moving. Interrupt it mid-sentence and it stops immediately.

It is not a note-taking app, not a search engine, and not a wellbeing companion. It is a conversational agent that makes your thinking better in real time.

---

## How it works

A cascaded voice pipeline — conversation state is **text on the server**, which is what makes provider swapping, the ops log, and the future memory layer possible:

```
browser mic ── WebRTC ──▶ Deepgram Flux ──▶ Gemini 2.5 Flash ──▶ Cartesia Sonic-3 ──▶ speaker
                          (STT + native      (thinking off,        (~150ms to
                           end-of-turn)       tool calling)         first audio)
```

- **Turn detection** is native to Flux — no push-to-talk, no VAD tuning. Barge-in (interrupting the agent mid-response) is handled by the pipeline.
- **Latency** is treated as a product requirement: per-stage budgets, structured per-turn breakdowns in the logs, WARN at >1s per stage, ERROR at >3s end-of-speech → first audio. Measured: ~1–1.5s typical.
- **Every provider is swappable** behind factory functions in [`backend/agent/providers.py`](backend/agent/providers.py) — the only file that imports provider SDKs.
- **Transcripts** are stored in Postgres as an operational log only; they are never injected into the agent's context and never shown to the user.

| Layer | Choice |
|---|---|
| Voice orchestration | [Pipecat](https://github.com/pipecat-ai/pipecat) (Python) |
| Transport | WebRTC (Pipecat SmallWebRTC) |
| STT + turn detection | Deepgram Flux |
| LLM | Gemini 2.5 Flash (swappable) |
| TTS | Cartesia Sonic-3 |
| Backend | FastAPI + SQLAlchemy + Postgres |
| Frontend | Next.js + Pipecat JS SDK |

## Run it

Everything runs in Docker — no local Python or Node needed.

```bash
cp .env.example .env   # then fill in the three API keys
docker compose up -d
```

Open **http://localhost:3000**, tap **Talk**, allow the mic, and start thinking out loud.

You'll need API keys (each has a free tier) for:
[Deepgram](https://console.deepgram.com) · [Gemini](https://aistudio.google.com) · [Cartesia](https://play.cartesia.ai)

### Tests

```bash
docker compose run --rm backend pytest tests/ -q
```

The suite pins the MVP's behavioral contracts — pipeline assembly, signaling API, prompt rules, latency thresholds, transcript flow, schema shape — so future features fail loudly if they break core functionality.

## Project layout

```
backend/
  app/        FastAPI app + signaling routes (/start, /api/offer)
  agent/      CompanionAgent, provider factories, prompts, tools, sanitizer
  db/         models, transcript ops log, session lifecycle
  obs/        structured JSON logging + per-turn latency observers
  tests/      the regression suite
frontend/     Next.js app — session button FSM, live waveform, artifacts panel
```

## Docs

- [REQUIREMENTS.md](REQUIREMENTS.md) — what the product must do (and what's deliberately out of scope)
- [SDD.md](SDD.md) — the software design this branch implements
- [SDD-v2.md](SDD-v2.md) — an alternative fully hand-rolled design, kept for learning value
- [PLAN.md](PLAN.md) — scrap notes and future directions (deployment, memory layer)

## Status

Working MVP demo (localhost). Deliberately deferred: cross-session memory (MemGPT-style design planned), session resume, proactive flagging, deployment off localhost. See [REQUIREMENTS.md §6](REQUIREMENTS.md) for the full out-of-scope list and PLAN.md for where this goes next.
