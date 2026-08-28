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

### 4.8 Authentication & Accounts

Provider decision: **Firebase Auth** — Google sign-in + email/password, open
signup. Session credential: Firebase ID tokens attached as `Authorization:
Bearer` on every API request (the client SDK silently refreshes them hourly);
verified server-side with the `firebase-admin` SDK. Replaces the site-wide
Caddy `basic_auth` gate, which is removed at rollout.

- **FR-22** Anyone can create an account, no approval step, via (a) "Continue
  with Google" or (b) email + password signup.
- **FR-23** Every backend API request resolves to a verified `user_id` through
  a single FastAPI dependency (`get_current_user_id`): verify the Bearer ID
  token (signature, issuer, audience, expiry) via `firebase-admin`; `user_id`
  = the token's `uid`. Missing/invalid token → 401. A `user_id` is never read
  from a request body, query param, or client-set header. Repo functions take
  `user_id: str` with no default value.
- **FR-24** A `users` row keyed by the Firebase `uid` is auto-provisioned in
  Postgres at `/start` — the one endpoint that begins creating user-owned
  rows (the upload-time document store is in-memory; no row needed) — not
  inside `get_current_user_id`, which stays a pure verifier with no DB writes
  (no per-request write amplification). The `uid` is the foreign key
  for all user-owned data. Provisioning is an atomic upsert keyed on the
  unique `uid`, so concurrent first requests cannot race into duplicates or
  errors — and when the verified token carries profile fields, the upsert
  backfills them **fill-only**
  (`ON CONFLICT DO UPDATE` with `COALESCE`: a provided value fills a missing
  one; an absent value never overwrites a stored one). Neither ordering —
  nameless provision first or named signup first — can drop or null the name.
  The row stores the user's preferred name, read from the verified ID
  token's `name` claim — set as the Firebase profile `displayName` by the
  signup form (FR-30; the client refreshes its token after signup so the
  claim appears) or by Google's own profile. The name is never read from a
  request body — same provenance rule as `user_id`. (Feeding the name into
  the agent's system prompt is deferred — see §6.)
- **FR-25** Email/password signup sends Firebase's verification link, but
  access is **not** gated on it: unverified accounts are fully functional
  (smooth-UX decision for the demo). Accepted demo-scale consequences:
  (a) an unverified password account later claimed by a Google sign-in on the
  same address loses its password per FR-26(c); (b) the sharper version: an
  attacker can pre-register someone else's email (unverified but fully
  functional) and accumulate data under that `uid` — the real owner's later
  Google sign-in inherits that polluted account. Accepted only while access
  is a closed demo; **before any public launch**, an FR-26(c) takeover of a
  never-verified account must purge (or quarantine) that account's prior
  user-owned rows. The admin grant script still refuses unverified targets
  per FR-28. Google sign-ins are verified from the start.
- **FR-26** Sign-in/sign-up behavior per method, under Firebase's default
  one-account-per-email policy with email-enumeration protection ON:
  - (a) Google, new email → account created and signed in.
  - (b) Google, existing Google account → signs into the same account.
  - (c) Google, where an email+password account already holds that gmail →
    signs into the **same `uid`** (user data intact). If that account was
    never verified, Firebase removes its password credential (the documented
    trusted-provider takeover rule — see
    https://firebase.google.com/docs/auth/web/google-signin and the
    "verified email addresses" section of
    https://firebase.google.com/docs/auth/users); if it was verified, both
    providers coexist and the
    password survives. Either way the app treats this as a normal sign-in,
    not an error.
  - (d) Email+password sign-in against an account with no password credential
    (Google-born), or with wrong credentials → generic failure
    (`auth/invalid-credential`). The UI shows one non-enumerating message for
    all failed sign-ins (e.g. "Sign-in failed. Check your credentials, or try
    continuing with Google.") and never reveals whether an email is registered
    or which methods it uses.
  - (e) Email+password sign-up with an email already in use →
    `auth/email-already-in-use`, surfaced as "already registered — sign in
    instead."
  - (f) Google sign-in asserting a non-gmail address that belongs to an
    existing account → `auth/account-exists-with-different-credential`; v1
    shows "sign in with your original method" (no automatic linking).
  - (g) Email+password sign-in with correct credentials on an account that
    holds a password → signed in (the base case, stated for completeness).
  - Accepted enumeration exceptions: (e) and (f) necessarily reveal that an
    email is already registered. Firebase does not suppress either error
    (documented behavior — enumeration protection covers sign-in, not signup
    or OAuth collisions), so these are deliberate, documented exceptions to
    (d)'s non-enumeration rule — not oversights. The signup availability
    pre-check endpoint (FR-30) surfaces the same fact as (e) one click
    earlier; it is unauthenticated by necessity (the caller has no account
    yet) and rate-limited per caller, accepted on the same grounds.
- **FR-27** The user can sign out, landing back on `/login`. Password accounts
  can reset their password via Firebase's emailed reset link; the
  reset-request confirmation is non-enumerating ("If an account exists for
  this email, a reset link has been sent") regardless of whether the email is
  registered.
- **FR-28** Admin access is granted by the Firebase custom claim
  `admin: true`, checked server-side on every admin request by a second
  dependency (`get_current_admin`; 403 otherwise). Claims are granted/revoked
  only by a committed script run locally with the service-account credential;
  the script must refuse a target account whose `email_verified` is false. No
  admin identifier (email or uid) lives in the repo, env, or DB.
- **FR-29** Admin capabilities in this feature: list accounts (uid, email,
  providers, created, disabled, last sign-in) and disable/enable an account.
  Disabling also revokes the user's refresh tokens. Deliberate v1 disable
  semantics, in effect-order:
  - New sessions are blocked immediately: `/start` and the session-establishment
    signaling endpoints (`/api/offer`, `/sessions/{id}/api/offer`) verify with
    `check_revoked=True` — an accepted extra network round trip on session
    bootstrap (off the NFR-1 hot path), paid so lockout is instant and a
    disable landing mid-handshake cannot still complete a session.
  - Other endpoints verify locally, so remaining API access dies when the
    current token expires (≤1h).
  - An already-connected voice session is not terminated; it runs until it
    ends naturally, and no new session can follow it.
  (Per-user usage metrics belong to the observability feature, not this one.)
- **FR-30** Frontend: a `/login` page; unauthenticated visits redirect there.
  Layout, top to bottom: email field; password field; two side-by-side buttons
  directly under the password field — "Sign in" (left) and "Sign up" (right),
  their combined width equal to the field width; below them a
  "Sign in with Google" button with the Google logo, same width as the fields.
  One form serves both sign-in and sign-up. Behavior:
  - Errors render inline and never clear the form — both fields keep their
    values on any failed attempt (mistaken "Sign up" on an existing account
    shows FR-26(e)'s small error with everything still filled in).
  - "Forgot password?" is small hyperlink text (not a button) under the
    fields, always visible while the form is in sign-in mode (FR-27's reset
    entry point; signup mode shows "Already have an account?" instead —
    recovery belongs to sign-in).
  - "Sign up" first checks availability via the rate-limited, unauthenticated
    `/api/auth/email-check` endpoint (an Admin SDK lookup; a documented FR-26
    enumeration exception): if the email is registered, FR-26(e)'s inline
    error shows and the form does NOT expand. Only an available email
    switches the form to signup mode — credentials stay in place, a
    "Preferred name" field appears, and the side-by-side buttons are replaced
    by a single explicit "Create account" action; nothing is created until
    that click. On success the user is signed
    in and taken into the app (verification email sent per FR-25); the
    preferred name — length-limited and escaped wherever displayed — is saved
    to the user's profile at creation.
  - In signup mode, "Already have an account?" hyperlink text sits at the
    bottom of the form; clicking it reverts to sign-in mode with the email and
    password fields keeping whatever is already typed.
  - Empty or malformed inputs are rejected inline before any Firebase call;
    Firebase-side errors (e.g., password too short) surface inline the same
    way, keeping field values.
  - Already-signed-in visits to `/login` redirect into the app. If `/login`
    supports a post-login redirect parameter, it accepts only same-origin
    paths (no open redirect).
  Testable UI notes: signed-out / loading / error states exist; sign-in error
  copy follows FR-26(d); an "Admin" nav item renders only when the token
  carries the admin claim (cosmetic — the server enforces regardless). Finer
  visual design is not specified; NFR-3 (mobile browsers) applies.
- **FR-31** Postgres row-level security is enabled on every table holding
  user-owned rows (sessions, transcript events, artifacts, and any table this
  feature adds), with policies restricting access to rows matching the
  request's verified `user_id` (communicated to Postgres per request via
  `SET LOCAL app.user_id` in the DB session factory — `SET LOCAL` specifically
  because it is transaction-scoped: it must run inside the same transaction as
  the queries it scopes and resets at commit, so a pooled connection can never
  carry a stale `user_id` into another request. The FR-31 test must cover
  pooled-connection reuse). Writers that bypass the
  HTTP layer — the per-session background transcript writer today, the memory
  loop later — set `app.user_id` the same way, from the session state they
  were created with at `/start`; they are per-session, so a write batch never
  spans users. RLS only binds if the connecting role cannot bypass it: the
  application connects as a dedicated `NOSUPERUSER` role with `NOBYPASSRLS`
  that does not own the tables (or the tables set `FORCE ROW LEVEL
  SECURITY`) — the compose default `aloud` user is a Postgres superuser,
  which silently bypasses every policy. The FR-31 test must run through the
  application's actual connection role. The implementation PR must include a test
  proving cross-user rows are not returned even when application-level
  scoping is bypassed (i.e., a query missing its `WHERE user_id` filter comes
  back empty, not with another user's rows).

### 4.9 Observability & Admin Usage

Purpose: answer the administrator's questions — *who uses this, what does it
cost, and what happened in that session?* — and the on-call engineer's
questions — *is it down, what's erroring, is it slow?* Three design
principles govern every FR below:

1. **Raw units are the record; cost is a view.** The database stores what was
   consumed (audio seconds, tokens, characters) as immutable facts; dollar
   figures are computed at display time from a rates config and labeled
   *estimated* — provider prices change, stored costs rot.
2. **Usage and metadata, never content.** No admin surface — page, endpoint,
   or shipped log at production settings — exposes transcript text, artifact
   content, or document content (NFR-9).
3. **Capture never touches the voice hot path.** All measurement follows the
   transcript writer's pattern: pipeline observers enqueue, background
   writers batch, write failures log and drop (NFR-10).

- **FR-32** Every session records its raw usage in an append-only
  `usage_events` table (metadata only — no sensitive columns), captured by a
  per-session pipeline observer from the usage metrics the pipeline already
  emits and batch-written through the user-scoped path (per-session, so
  per-user; RLS applies): LLM prompt and completion tokens per inference,
  TTS characters per utterance. STT usage is recorded at session end as the
  session's audio duration (connect → disconnect, in seconds) — a proxy for
  Flux's streamed-time billing, and flagged as such wherever displayed.
  Required dimensions per event: `user_id`, `session_id`, timestamp, stage,
  unit, quantity.
- **FR-33** Per-turn latency is persisted, not just logged: a `turn_metrics`
  table (`user_id`, `session_id`, `turn_id`, timestamp, end-of-speech →
  first-audio ms, per-stage TTFB ms) written from the breakdowns the latency
  observer already computes, via the same background-writer pattern. This is
  the queryable substrate for percentiles (FR-37) and session drill-down
  (FR-36).
- **FR-34** Cost derivation: a provider-rates config (documented in
  `.env.example`: STT per audio-minute, LLM per 1M input and output tokens,
  TTS per 1M characters) converts raw units to dollars at read time. No cost
  is ever stored. Displays label figures "estimated"; historical usage is
  always priced at *current* rates (accepted simplification — re-pricing
  history correctly would require dated rate records).
- **FR-35** Admin user list (`/api/admin/users`, extending FR-29): search by
  email/name/uid substring, sortable, paginated. Per-user columns: account
  status (from Firebase), sessions count, total audio minutes, LLM tokens
  in/out, TTS characters, estimated cost, last-active (from DB aggregates).
- **FR-36** Session history and drill-down: per user, a sessions list
  (started, duration, end reason, artifact count, per-stage usage, median and
  worst turn latency); per session, the turn-by-turn latency series and
  usage totals. The drill-down answers "user X says it broke at 3pm"
  without ever showing what was said (NFR-9).
- **FR-37** Admin overview tab, the at-a-glance view: live sessions right
  now (in-process count; resets on deploy — accepted), sessions and unique
  users today / last 7 days, estimated spend by provider (7d / 30d),
  turn-latency p50/p95 over 24h, and count of NFR-1 budget breaches over
  24h. For error inspection it links out to Cloud Logging / Error Reporting
  (FR-39) rather than rebuilding them in-app.
- **FR-38** Admin cross-user reads use an explicit, auditable RLS escape:
  every FR-31 policy is extended with
  `OR current_setting('app.is_admin', true) = 'true'`, and that setting is
  applied (transaction-locally, like `app.user_id`) only by an
  `admin_scoped_session()` helper that only admin routes (behind
  `get_current_admin`) may use. Admin sessions are read-only by
  construction. Tests must prove: (a) normal user-scoped sessions remain
  isolated exactly as before, (b) a session with neither context still
  returns zero rows, (c) the admin context reads across users.
- **FR-39** Ops — logs leave the box: the VM's containers ship stdout to
  GCP Cloud Logging (Docker `gcplogs` logging driver in the prod compose;
  the structured-log JSON was designed for this — its `severity` field maps
  to Cloud Logging levels, `session_id`/`event`/`user_id` become queryable
  `jsonPayload` fields). ERROR-severity entries surface in GCP Error
  Reporting automatically. The on-call flow (Logs Explorer queries for a
  session_id, Error Reporting triage) is documented in
  CURRENT-ARCHITECTURE.md. Prod stays at `LOG_LEVEL=INFO`, which carries no
  transcript text (NFR-9's shipped-logs half).
- **FR-40** Ops — uptime alerting: a GCP Monitoring uptime check probes
  `https://work-aloud.com/healthz` (multi-region, 5-minute cadence) with an
  alert policy that emails the admin when it fails. Configuration recorded
  in CURRENT-ARCHITECTURE.md, including how to verify the alert actually
  fires (a deliberate one-time test).

Retention: `usage_events` and `turn_metrics` are kept indefinitely — at
current scale they grow by kilobytes per session, and raw usage is the audit
trail. Revisit trigger (recorded here deliberately): when either table
passes ~1M rows or the database exceeds ~1 GB, add monthly rollups and purge
raw rows older than 90 days.

---

## 5. Non-Functional Requirements

### 5.1 Latency
- **NFR-1** Time from end-of-user-speech to first audio from the agent must be under 3 seconds under normal network conditions.
- **NFR-2** Audio must stream as it is generated. The agent must not wait until its full response is ready before speaking.
- **NFR-10** Measurement must be free: usage and metrics capture (§4.9) adds no synchronous work to the voice hot path — observers enqueue, background writers batch, and a failed metrics write logs an error and drops the batch rather than disturbing a live session (the FR-20 transcript-writer discipline, applied to all capture).

### 5.2 Availability & Reliability
- **NFR-3** The app is a web application. It must function correctly in mobile browsers on iOS and Android, and in desktop browsers. No native app installation required.

*NFR-4 (session state recovery after connection drop) — moved to §6 Out of Scope along with FR-19; they were the same requirement.*

### 5.3 Privacy
- **NFR-5** All voice data and transcripts are processed server-side. The privacy policy must disclose this clearly.
- **NFR-6** Sensitive session content (transcripts, artifacts, future memory entries) must live in dedicated database columns, separable from session metadata, so that encryption at rest can be added post-MVP without schema rework. The encryption itself is deferred — see §6 Out of Scope.
- **NFR-7** The user must be able to delete all their data.
- **NFR-8** User isolation: no authenticated user can read or write another
  user's data. Every query on user-owned tables is scoped by the verified
  `user_id`, with Postgres row-level security enabled on those tables as
  defense-in-depth (FR-31). Auth/scoping changes require a negative test
  (user A cannot reach user B's data).
- **NFR-9** Admin sees usage, never content: no admin surface — page,
  endpoint, or log shipped at production settings — exposes transcript text,
  artifact content, or document content. Admins can see that a user talked
  for forty minutes; never what was said. (Transcript text appears in logs
  only at DEBUG level, which production does not run.)

---

## 6. Out of Scope

The following are explicitly not part of this product:

- **Web search / real-time information.** The agent does not look things up. It works with what the user brings to the conversation.
- **Task management / reminders.** Action items surface in conversation but Aloud does not manage follow-through.
- **Emotional support / mental health.** The agent is a thinking tool, not a wellbeing companion. It must never present itself as a therapist or suggest therapeutic interpretations.
- **Collaboration.** No shared or multi-participant sessions: a session belongs to exactly one account. (Individual accounts themselves are in scope — §4.8.)
- **Link ingestion & non-text documents.** The agent reads attached text, Markdown, and PDF documents (FR-21), but it cannot fetch URLs the user shares, and it cannot read scanned/image-only PDFs (no OCR).

### Deferred — planned, but out of scope for the MVP demo

- **Cross-session memory** (formerly FR-15–FR-17). The agent starts every session fresh; memory is in-session only. Planned later following the MemGPT framework, possibly integrating RAG with clever indexing and semantic vector search, depending on performance.
- **Streaming memory processing.** When cross-session memory lands, it must run in parallel while the user is still speaking — context editing during input, not after the session ends.
- **Document persistence.** Uploaded documents (FR-21) are ephemeral for the MVP — held in memory for the session and discarded when the process restarts. When cross-session memory lands, documents will persist alongside the conversation.
- **Proactive flagging** (formerly FR-8; demo stretch goal). The agent surfacing gaps, contradictions, or connections unprompted, with a user-configurable on/off setting.
- **Brainstorm/critique mode inference** (formerly FR-10; demo stretch goal). Distinct generative vs. analytical behavior, inferred from context or set explicitly.
- **Session resume** (formerly FR-19 / NFR-4). A dropped connection ends the session; the user starts a new one.
- **Encryption at rest** (deferred from NFR-6). The schema keeps sensitive content in dedicated columns so encryption can be added post-MVP without rework; the encryption itself is not in the MVP.
- **Name personalization** (deferred from FR-24). The user's stored preferred name is injected into the agent's system prompt at session start so the agent addresses them by name. Small change once auth lands: `/start` already resolves the user, and the system prompt is already built per session.

---

## 7. Constraints

- **C-1** Voice I/O pipeline latency is the binding constraint on model and architecture choices. The total budget from end-of-speech to first audio is 3 seconds. Any single component consuming more than ~1 second of that budget is a candidate for replacement.
- **C-2** The LLM provider must be swappable without rewriting session logic, memory, or API routes. Provider-specific code is isolated to a single agent class.
- **C-3** The product must never describe itself or its agent as a therapist, counselor, or mental health resource — in UI copy, system prompts, or onboarding.
- **C-4** Auth secrets (the Firebase service-account key) live outside the repo. Env var names are documented in `.env.example`; values are never committed.

---

