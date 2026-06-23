# Aloud — Requirements Document

## 1. Product Purpose

Aloud is a voice-first thinking partner for people who process ideas best by talking out loud. You speak; it listens, asks sharp questions, and helps you think more clearly — while you're walking, commuting, or anywhere else you don't have your hands free.

It is not a search engine, a task manager, or a note-taking app. It is a conversational agent that makes your thinking better in real time.

---

## 2. Target User

Someone who regularly needs to work through ideas, plans, or problems — founders, product people, designers, engineers, writers, anyone who thinks out loud — and wants a capable sounding board available on demand without needing to sit down at a desk.

---

## 3. Core Use Cases

### UC-1: Brainstorming
User has a rough idea and wants to develop it. They talk through it; the agent asks probing questions, surfaces assumptions, and helps flesh it out into something more concrete.

### UC-2: Strategy / Plan Review
User explains a plan or strategy. The agent identifies gaps, blind spots, contradictions, or unstated assumptions the user hasn't considered.

### UC-3: Quick Clarifying Question
User needs a fast answer to a specific question mid-thought. The agent answers concisely and returns control to the user.

### UC-4: Thought Organization
User has been rambling through a messy idea. On request, the agent organizes and reflects back a structured version of what was said.

### UC-5: On-the-Go Capture
User is walking and wants to capture and develop a fleeting thought before it disappears. The session is low-friction — start talking immediately, no setup.

### UC-6: Suggestion Mode
User explicitly asks for the agent's opinion, alternatives, or next steps. The agent makes concrete suggestions rather than just asking more questions.

---

## 4. Functional Requirements

### 4.1 Voice Interaction
- **FR-1** The app must support hands-free voice input and voice output. No typing required.
- **FR-2** The user must be able to start a session with a single tap and begin speaking immediately.
- **FR-3** Turn detection is automatic via Voice Activity Detection (VAD). The agent responds when the user stops speaking; no button press is required to signal end-of-turn.
- **FR-4** Voice output must feel natural and conversational, not robotic.

### 4.2 Session Controls & UI
- **FR-5** The app must have a single prominent button that manages session state:
  - **Idle / ready:** green "Talk" button. Tapping starts a session.
  - **Booting up or shutting down:** grey, non-interactive. Indicates the system is connecting or cleaning up.
  - **Active session:** red "End" button. Tapping ends the session.
- **FR-6** While a session is active, the UI must indicate the current state via an animated waveform bar below the button:
  - **Listening** — waveform animates to the user's mic amplitude (user sees their own voice as bars).
  - **Thinking** — three pulsing dots (typing-indicator style).
  - **Speaking** — waveform animates to the agent's audio amplitude, in a distinct color from the listening state.

### 4.3 Agent Behavior
- **FR-7** By default, the agent is reactive: it responds when the user speaks.
- **FR-9** The agent must ask one question at a time. It must not overwhelm the user with multiple questions or unsolicited lists of suggestions.
- **FR-11** When asked, the agent must be able to summarize the current session's key ideas, decisions, and open questions.
- **FR-12** When asked, the agent must be able to produce a written artifact: a structured summary, a list of action items, or a cleaned-up version of the user's idea.

*FR-8 (proactive flagging) and FR-10 (brainstorm/critique modes) are demo stretch goals — moved to §6 Out of Scope. Requirement numbering stays stable; removed numbers are not reused.*

### 4.4 Barge-In
- **FR-13** The user must be able to start speaking while the agent is mid-response. The agent must stop speaking immediately, discard the remainder of its current response, and process the new input. The user can use this to redirect the conversation, add context, or correct the agent without waiting for it to finish.

### 4.5 Memory
- **FR-14** Within a session, the agent must remember everything said. It must be able to reference specific details from earlier in the same session.

*FR-15–FR-17 (cross-session memory: retention across sessions, recall of past sessions, memory correction/deletion) — moved to §6 Out of Scope.*

### 4.6 Session Management
- **FR-18** A session begins when the user taps "Talk" and the connection is established. It ends when the user taps "End" or the connection is lost.
- **FR-20** Full session transcripts must be stored in the backend database as an operational log. This is for internal review and debugging — it is not user-facing. The transcript is not injected into the agent's context.

*FR-19 (resume after connection drop) — moved to §6 Out of Scope. A dropped connection simply ends the session.*

### 4.7 Documents
- **FR-21** Before a session, the user may attach one or more documents (plain text, Markdown, or PDF). The agent reads the attached documents and can reference and discuss them during the session. Attached documents are held in memory for that session only and are not persisted — see §6 (persistence is deferred to the future memory layer).

---

## 5. Non-Functional Requirements

### 5.1 Latency
- **NFR-1** Time from end-of-user-speech to first audio from the agent must be under 3 seconds under normal network conditions.
- **NFR-2** Audio must stream as it is generated. The agent must not wait until its full response is ready before speaking.

### 5.2 Availability & Reliability
- **NFR-3** The app is a web application. It must function correctly in mobile browsers on iOS and Android, and in desktop browsers. No native app installation required.

*NFR-4 (session state recovery after connection drop) — moved to §6 Out of Scope along with FR-19; they were the same requirement.*

### 5.3 Privacy
- **NFR-5** All voice data and transcripts are processed server-side. The privacy policy must disclose this clearly.
- **NFR-6** Sensitive session content (transcripts, artifacts, future memory entries) must live in dedicated database columns, separable from session metadata, so that encryption at rest can be added post-MVP without schema rework. The encryption itself is deferred — see §6 Out of Scope.
- **NFR-7** The user must be able to delete all their data.

---

## 6. Out of Scope

The following are explicitly not part of this product:

- **Web search / real-time information.** The agent does not look things up. It works with what the user brings to the conversation.
- **Task management / reminders.** Action items surface in conversation but Aloud does not manage follow-through.
- **Emotional support / mental health.** The agent is a thinking tool, not a wellbeing companion. It must never present itself as a therapist or suggest therapeutic interpretations.
- **Collaboration.** Sessions are single-user. No shared sessions or multi-user features.
- **Link ingestion & non-text documents.** The agent reads attached text, Markdown, and PDF documents (FR-21), but it cannot fetch URLs the user shares, and it cannot read scanned/image-only PDFs (no OCR).

### Deferred — planned, but out of scope for the MVP demo

- **Cross-session memory** (formerly FR-15–FR-17). The agent starts every session fresh; memory is in-session only. Planned later following the MemGPT framework, possibly integrating RAG with clever indexing and semantic vector search, depending on performance.
- **Streaming memory processing.** When cross-session memory lands, it must run in parallel while the user is still speaking — context editing during input, not after the session ends.
- **Document persistence.** Uploaded documents (FR-21) are ephemeral for the MVP — held in memory for the session and discarded when the process restarts. When cross-session memory lands, documents will persist alongside the conversation.
- **Proactive flagging** (formerly FR-8; demo stretch goal). The agent surfacing gaps, contradictions, or connections unprompted, with a user-configurable on/off setting.
- **Brainstorm/critique mode inference** (formerly FR-10; demo stretch goal). Distinct generative vs. analytical behavior, inferred from context or set explicitly.
- **Session resume** (formerly FR-19 / NFR-4). A dropped connection ends the session; the user starts a new one.
- **Encryption at rest** (deferred from NFR-6). The schema keeps sensitive content in dedicated columns so encryption can be added post-MVP without rework; the encryption itself is not in the MVP.

---

## 7. Constraints

- **C-1** Voice I/O pipeline latency is the binding constraint on model and architecture choices. The total budget from end-of-speech to first audio is 3 seconds. Any single component consuming more than ~1 second of that budget is a candidate for replacement.
- **C-2** The LLM provider must be swappable without rewriting session logic, memory, or API routes. Provider-specific code is isolated to a single agent class.
- **C-3** The product must never describe itself or its agent as a therapist, counselor, or mental health resource — in UI copy, system prompts, or onboarding.

---

