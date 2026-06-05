# Aloud — Sprint Plan

Solo project. 1-week sprints. Build vertically (full working slice each sprint, not layers).

## Current sprint

**Sprint 1** — Voice → Gemini Live → text response in browser

---

## Backlog

| Sprint | Goal | Done when |
|---|---|---|
| 1 | Voice in → Gemini Live → text response | User speaks, sees transcript, reads agent reply in browser |
| 2 | Full voice loop + journaling companion persona | Voice-in/voice-out works; system prompt shapes agent tone correctly |
| 3 | User auth + session persistence (no encryption) | Sessions saved to DB; user can log in and see past sessions |
| 4 | Memory layer v1 — end-of-session summary | After each session, LLM extracts and stores a summary |
| 5 | Cross-session context injection | Agent meaningfully references things from past sessions |
| 6 | Sentiment tracking + trend visualization | User can see emotional arc across sessions |
| 7 | Anthropic branch — swap LLM provider | `feature/anthropic` branch works end-to-end; compare quality |
| 8+ | Server-side encryption, deployment hardening, polish | — |

---

## Sprint log

### Sprint 1
- [ ] Scaffold Next.js frontend + FastAPI backend
- [ ] Connect Gemini 2.5 Flash Live API (WebSocket)
- [ ] Browser mic capture → stream to backend → stream response back
- [ ] Display transcript in UI
