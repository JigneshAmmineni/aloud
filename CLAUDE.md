# Aloud — Project Context for Claude

## What this is

A voice-first journaling companion. Users speak, the app listens, and an AI agent responds with guiding questions to encourage reflection and deeper introspection. The agent notices patterns across sessions and gently surfaces them.

**It is NOT a therapist. Never use the word "therapy" in the product, UI copy, or system prompts.**

---

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js + React |
| Backend | Python + FastAPI |
| LLM (prototype) | Gemini 2.5 Flash via Live API |
| LLM (future) | Claude (Anthropic) — see migration note below |
| DB | PostgreSQL |
| Auth | TBD (likely Supabase Auth or Auth.js) |

---

## Key architectural decisions

### LLM provider abstraction (required, do not skip)

All LLM interactions must go through a single `CompanionAgent` class in the Python backend. The provider-specific code (SDK calls, message formatting, audio pipeline) is isolated inside that class. Everything else — memory layer, session logic, DB, API routes — is provider-agnostic.

**Why this matters:** the prototype uses Gemini Live API (which handles STT + LLM + TTS natively in one WebSocket connection). The Claude branch will swap in Whisper (STT) + Claude API + ElevenLabs or browser TTS as three separate calls. The abstraction layer is what makes this a branch swap, not a rewrite.

### Gemini prototype model

Use **Gemini 2.5 Flash** via the Live API. Verify availability in Google AI Studio before starting — Live API support may lag for 2.5 Pro.

### Future Claude migration (feature/anthropic branch)

When ready to migrate:
- Swap `CompanionAgent` internals only
- STT: Whisper WASM (browser, local) or Whisper API
- LLM: Claude Sonnet (best quality/cost for emotionally nuanced conversation)
- TTS: ElevenLabs or browser `speechSynthesis`
- System prompt will need retuning — Claude has a different default personality than Gemini

### Memory layer (two-tier)

**Tier 1 — Session summary (runs at end of each session):**
A second LLM call reads the full session transcript and extracts: key themes, emotional tone, things the user seems to be processing. Stored in a `session_memories` table. This is the agent manually deciding what's worth noting.

**Tier 2 — Cross-session patterns (runs periodically):**
Aggregates tier-1 summaries to detect recurring topics and sentiment trends over time. This powers the "noticing patterns" feature.

Sentiment analysis runs as a parallel numeric signal (separate lightweight pass) for visualization/trend tracking — not a replacement for the agent's qualitative judgment.

Reference: MemGPT/Letta paper for tiered agent memory design.

### Encryption (add after MVP)

Cloud LLM is fine — privacy policy will disclose that transcripts are processed by Google/Anthropic.

Server-side encryption (key held by us) will be added post-MVP. To make this painless:
- **Keep sensitive columns clearly separated from metadata at schema design time.** Transcripts, session summaries, and memory entries go in their own columns/tables. Timestamps, user IDs, and session metadata stay unencrypted.
- Adding `pgcrypto` or app-level AES encryption to bounded columns later is straightforward if the schema is clean.

---

## Hard constraints

- Never describe the app as therapy or the agent as a therapist — in code, copy, system prompts, or documentation.
- Always route LLM calls through `CompanionAgent`. No direct SDK calls in route handlers.
- DB schema: sensitive content columns must be separable from metadata. Design for encryption even if you don't implement it yet.

---

## Docs

- [PLAN.md](PLAN.md) — sprint breakdown and current progress
