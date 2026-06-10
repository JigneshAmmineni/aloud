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
- **FR-3** The user must be able to signal end-of-turn (stop speaking) and receive a response without additional taps.
- **FR-4** Voice output must feel natural and conversational, not robotic.

### 4.2 Agent Behavior
- **FR-5** By default, the agent is reactive: it responds when the user speaks.
- **FR-6** When the agent detects something worth flagging — a gap in reasoning, a contradiction, a strong connection between ideas — it may proactively surface it without waiting to be asked. This behavior must be configurable (on/off).
- **FR-7** The agent must ask one question at a time. It must not overwhelm the user with multiple questions or unsolicited lists of suggestions.
- **FR-8** The agent must distinguish between modes: brainstorming (generative, expansive) vs. critique (analytical, skeptical). It should infer the mode from context; the user may also set it explicitly.
- **FR-9** When asked, the agent must be able to summarize the current session's key ideas, decisions, and open questions.
- **FR-10** When asked, the agent must be able to produce a written artifact: a structured summary, a list of action items, or a cleaned-up version of the user's idea.

### 4.3 Memory
- **FR-11** Within a session, the agent must remember everything said. It must be able to reference specific details from earlier in the same session.
- **FR-12** Across sessions, the agent must retain a memory of past conversations — key topics discussed, decisions made, recurring themes — and use that context to inform future sessions.
- **FR-13** The user must be able to ask about past sessions ("what did we discuss about X last week?") and get a meaningful answer.
- **FR-14** The user must be able to correct or delete stored memories.

### 4.4 Session Management
- **FR-15** A session begins when the user opens the app and connects. It ends when the user explicitly ends it or closes the app.
- **FR-16** The app must handle interruptions gracefully: network drops, backgrounding the app, and resuming mid-session should not lose context.
- **FR-17** Sessions must be logged and retrievable. The user must be able to review past sessions.

---

## 5. Non-Functional Requirements

### 5.1 Latency
- **NFR-1** Time from end-of-user-speech to first audio from the agent must be under 1 second under normal network conditions. This is the single most important feel metric — delays above 1s break the conversational illusion.
- **NFR-2** Audio must stream as it is generated. The agent must not wait until its full response is ready before speaking.

### 5.2 Availability & Reliability
- **NFR-3** The app must function on mobile (iOS and Android) and desktop (web browser).
- **NFR-4** Session state must be recoverable after a connection drop without losing the conversation so far.

### 5.3 Privacy
- **NFR-5** All voice data and transcripts are processed server-side. The privacy policy must disclose this clearly.
- **NFR-6** Sensitive session content (transcripts, summaries, memory entries) must be stored encrypted at rest.
- **NFR-7** The user must be able to delete all their data.

---

## 6. Out of Scope

The following are explicitly not part of this product:

- **Web search / real-time information.** The agent does not look things up. It works with what the user brings to the conversation.
- **Task management / reminders.** Action items surface in conversation but Aloud does not manage follow-through.
- **Emotional support / mental health.** The agent is a thinking tool, not a wellbeing companion. It must never present itself as a therapist or suggest therapeutic interpretations.
- **Collaboration.** Sessions are single-user. No shared sessions or multi-user features.
- **Document ingestion.** The agent cannot read files, PDFs, or links the user shares. Voice only.

---

## 7. Constraints

- **C-1** Voice I/O pipeline latency is the binding constraint on model and architecture choices. Any component that adds more than ~200ms of overhead is a candidate for replacement.
- **C-2** The LLM provider must be swappable without rewriting session logic, memory, or API routes. Provider-specific code is isolated to a single agent class.
- **C-3** The product must never describe itself or its agent as a therapist, counselor, or mental health resource — in UI copy, system prompts, or onboarding.
