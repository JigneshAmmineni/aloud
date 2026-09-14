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
3. **Capture never touches the voice hot path.** The rule is
   architecture-agnostic (NFR-10): the hot path only ever *enqueues* —
   an instant, in-memory operation — while separate background writers
   batch the queue into the database, and a failed write logs and drops
   rather than retrying into the conversation. Dropped-batch error logs
   carry `session_id` (and `turn_id` where applicable) per CLAUDE.md's
   instrumentation rule. Today's tap points are pipeline observers (the
   transcript writer is the exemplar); in a future custom agent loop they
   become direct recorder calls — the mechanism changes, the rule doesn't.

- **FR-32** Every session records its raw usage in an append-only
  `usage_events` table (metadata only — no sensitive columns), batch-written
  through the user-scoped path (per-session, so per-user; RLS applies).
  Capture points: LLM usage is recorded by the agent loop directly from
  each provider response once §4.10 lands (FR-47 — before that, by the
  metrics-frame observer); TTS stays on the metrics-frame observer; STT
  stays session-level. The recorded units: LLM prompt and completion tokens per inference,
  TTS characters per utterance, and an `artifact_created` event
  (`stage = 'artifact'`, `unit = 'count'`, `quantity = 1`, `detail` = the
  artifact's `kind` — never title/content) whenever the create_artifact
  tool succeeds, so admin views can count artifacts without touching the
  content-bearing table (FR-38). STT usage is recorded at session end as the
  session's audio duration (connect → disconnect, in seconds; the session
  row's start/end timestamps are written in the pipeline's cleanup path, so
  ungraceful disconnects are covered the same as a clean "End" tap) — a
  proxy for Flux's streamed-time billing, and flagged as such wherever
  displayed.
  Required dimensions per event: `user_id`, `session_id`, `turn_id`
  (nullable — session-level events like the STT record have none),
  timestamp, `stage` (the event's *source* — `stt` | `llm` | `tts` |
  `artifact`; a source label, not strictly a pipeline stage), `unit`,
  `quantity`, and a nullable `detail` field for event-specific metadata
  (the artifact `kind` lives here; never content). `turn_id` is what makes
  per-turn cost visible (FR-36).
  Crash behavior: because LLM/TTS events are written in ~1-second batches
  throughout the session, a process death (crash, OOM, deploy restart)
  loses only the final unflushed batch. Sessions orphaned by such a death
  are recovered by a **boot-time sweep**: on backend startup, any session
  still marked `active` is closed with `end_reason = 'interrupted'` (the
  backend went away — routine deploys cause this too, so it is deliberately
  NOT labeled an error and does not pollute FR-37's error signal, which
  counts only `end_reason = 'error'`), its
  `ended_at` inferred deterministically as the maximum timestamp across
  that session's `transcript_events`, `usage_events`, and `turn_metrics`
  rows (falling back to `started_at` when none exist), and its STT usage
  event emitted from that inferred duration. The sweep is correct only
  while the backend runs as a single instance (today's deployment — a
  booting instance can safely assume every `active` session is orphaned);
  a scaled-out backend must scope the sweep to sessions the booting
  instance owns, which joins the session-affinity work already noted in
  CURRENT-ARCHITECTURE.md's scaling notes. Accepted residual loss: the final in-flight batch and the
  imprecision of the inferred crash time — acceptable because usage here is
  best-effort telemetry, not a billing record (provider consoles remain the
  invoice truth; FR-34's figures are labeled estimates).
- **FR-33** Per-turn latency is persisted, not just logged: a `turn_metrics`
  table (`user_id`, `session_id`, `turn_id`, timestamp, end-of-speech →
  first-audio ms, per-stage TTFB ms) via the same background-writer
  pattern. Writer: the latency observer's breakdowns today; once §4.10
  lands, the observer only *measures* (first-audio ms + the turn number
  at measurement time) and the agent loop flushes the single per-turn
  row at turn end with step-indexed stage keys (FR-47).
  Turn identity — currently a schema aspiration nothing populates — is
  sourced from Pipecat's turn tracking, which the pipeline already enables
  (`enable_turn_tracking=True`): its tracker emits numbered turn
  start/end events that an observer maps onto every captured row, and turn
  boundaries under barge-in follow the tracker's own semantics (an
  interruption ends the turn). The implementation starts with a spike
  confirming the tracker's events carry a usable turn number; if they
  don't, the fallback is a per-session monotonic counter incremented on the
  tracker's turn-start event. This is
  the queryable substrate for percentiles (FR-37) and session drill-down
  (FR-36). Barge-in semantics: a turn interrupted *before* any agent audio
  produces no `turn_metrics` row (there is no end-of-speech → first-audio
  to measure), but any LLM tokens it consumed — and any TTS characters
  already submitted for synthesis (sentence-level chunks may be billed
  before the interruption lands) — ARE still recorded in `usage_events`:
  the rule is *spent is recorded*, whichever stage spent it. FR-36's
  per-turn table must therefore be driven from the union of both tables
  (latency may be absent for a turn that still has cost), never an inner
  join from latency. A turn interrupted mid-response records normally — its
  first-audio moment already happened.
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
  The Firebase merge is batched — one paginated `list_users` traversal per
  admin-list API request, joined in memory — never a per-row lookup (a
  hidden N+1 against a quota'd API). Search mechanics follow from that: email/name matching runs
  over the fetched-and-merged in-memory set (Postgres has no email column),
  which is fine at current account counts; mirroring emails into `users`
  is the revisit if the account list ever outgrows a single fetch.
- **FR-36** Session history and drill-down: per user, a sessions list
  (started, duration, end reason, artifact count, per-stage usage, median and
  worst turn latency); per session, a turn-by-turn table joining latency
  (FR-33) with that turn's usage and estimated cost (FR-32's `turn_id`
  events) — "this question cost 2,400 tokens and took 1.8s" — plus session
  usage totals. The drill-down answers "user X says it broke at 3pm"
  without ever showing what was said (NFR-9).
- **FR-37** Admin overview tab, the at-a-glance view: live sessions right
  now (in-process count; resets on deploy — accepted), sessions and unique
  users today / last 7 days, estimated spend by provider (7d / 30d),
  turn-latency p50/p95 over 24h, and count of NFR-1 budget breaches over
  24h. For error inspection it links out to Cloud Logging / Error Reporting
  (FR-39) rather than rebuilding them in-app. Deliberate narrowing of the
  roadmap's "per-user error rates": the in-app signal is each user's count
  of `end_reason = 'error'` sessions (visible in FR-36's session list);
  finer-grained error analysis lives in Cloud Logging, and richer per-user
  error attribution waits for LLM tracing (feature 6).
- **FR-38** Admin cross-user reads use an explicit, auditable, and
  **narrow** RLS escape:
  - Scope: the `OR current_setting('app.is_admin', true) = 'true'` clause
    lives on a **dedicated `FOR SELECT` policy** — never on the policies
    governing writes — and only on the tables the admin surface needs:
    `sessions`, `usage_events`, `turn_metrics`. The INSERT/UPDATE/DELETE
    policies keep their user-only predicates. This split exists because
    Postgres consults only `USING` for `DELETE` (never `WITH CHECK`), so an
    admin clause on a generic all-commands policy would let admin-context
    deletes pass RLS. With the split, every write command under admin
    context fails at two independent layers — the untouched write policies
    and the transaction's `READ ONLY` mode (below) — neither a single point
    of failure for the other. The content-bearing tables
    (`transcript_events`, `artifacts`) never receive it: even with the admin
    context set, a query against them returns zero rows, giving NFR-9 the
    same DB-level backstop that FR-31 gives NFR-8. Where admin views need
    metadata *about* content rows (FR-36's artifact count), the count comes
    from FR-32's `artifact_created` usage events — never from the
    `artifacts` table. (A metadata-only database view was considered and
    rejected: Postgres views execute with owner privileges by default —
    a superuser-owned view silently bypasses RLS for every caller — and
    `security_invoker` views would inherit the caller's zero-row policy;
    counting from the already-admin-scoped events table avoids the entire
    ownership-semantics class of bugs.)
  - The setting is applied transaction-locally (like `app.user_id`) only by
    an `admin_scoped_session()` helper whose admin-only constraint is
    structural, not conventional: its required argument is the `AuthedUser`
    produced by FR-28's `get_current_admin`, and it raises unless
    `is_admin` is true — a forgotten gate is a loud error, mirroring the
    no-default-`user_id` discipline.
  - Read-only is DB-enforced, not conventional: `admin_scoped_session()`
    issues `SET TRANSACTION READ ONLY`, so an accidental write through the
    admin context fails at the database.
  - Admin query functions are a distinct family from the user-scoped repo
    layer — CLAUDE.md's "repo signatures take `user_id: str`, no default"
    rule applies to the user-scoped family and is not misapplied here.
  - Tests must prove ("context" below = a database transaction, not a voice
    session): (a) user-scoped contexts remain isolated exactly as before,
    (b) a context with neither setting still returns zero rows, (c) the
    admin context reads across users on the scoped tables, (d) the admin
    context gets zero rows from `transcript_events` and `artifacts`, and
    admin API responses never serialize content columns, (e) writes
    attempted through the admin context fail — covering INSERT, UPDATE,
    **and DELETE** — and, on a non-READ-ONLY transaction with
    `app.is_admin` set, a cross-user INSERT/UPDATE is rejected by
    `WITH CHECK` and a cross-user DELETE matches zero rows (proving the
    policy layer guards every command independently of READ ONLY),
    (f) `app.is_admin` cannot leak across pooled-connection reuse (the
    FR-31 pooled-reuse test, repeated for this setting — a leak here
    grants cross-user reads).
- **FR-39** Ops — logs leave the box: the VM's containers ship stdout to
  GCP Cloud Logging (Docker `gcplogs` logging driver in the prod compose;
  the structured-log JSON was designed for this — its `severity` field maps
  to Cloud Logging levels, `session_id`/`event`/`user_id` become queryable
  `jsonPayload` fields). ERROR-severity entries surface in GCP Error
  Reporting. Implementation must begin with a spike verifying BOTH claims
  this flow rests on: that the driver promotes our JSON into queryable
  `jsonPayload` fields (fallback: the GCP Ops Agent with a structured-log
  parser config), and that our ERROR-severity entries actually surface in
  Error Reporting, which has its own format expectations (fallback: a
  log-based alert on `severity >= ERROR`, which needs no special format).
  The verified on-call flow (Logs Explorer queries for a session_id, Error
  Reporting triage) is then documented in CURRENT-ARCHITECTURE.md.
  Because shipped logs are persistent and external, INFO-only is enforced
  by default, not by a hand-typed env line: this FR flips the code's
  `LOG_LEVEL` fallback from DEBUG to INFO (and `.env.example` to match) —
  DEBUG becomes the explicit dev opt-in, so the safe state is the default
  state and no forgotten prod `.env` entry can ship transcript text
  (NFR-9's shipped-logs half).
- **FR-40** Ops — uptime alerting: a GCP Monitoring uptime check probes
  `https://work-aloud.com/healthz` (multi-region, 5-minute cadence) with an
  alert policy that emails the admin when it fails. Configuration recorded
  in CURRENT-ARCHITECTURE.md, including how to verify the alert actually
  fires (a deliberate one-time test).
- **FR-41** Admin UI structure — every view is a URL-addressable page (deep
  links are how ops work gets shared; no modals or expanding rows for
  primary navigation):
  - `/admin` — the Overview tab (FR-37), the default landing view.
  - `/admin/users` — the user list (FR-35): a search input at the top
    (filters as you type, debounced), a table with sortable column headers
    (click to sort, click again to reverse), pagination controls below.
    Each row keeps FR-29's disable/enable button.
  - Clicking anywhere else on a user row opens `/admin/users/{uid}` — that
    user's session history (FR-36): account summary at top (email, name,
    status, totals), sessions table below, newest first.
  - Clicking a session row opens `/admin/sessions/{session_id}` — the
    drill-down (FR-36): session summary (duration, end reason, usage,
    estimated cost) and the per-turn latency table, with turns exceeding
    the NFR-1 budget visually flagged.
  - A persistent tab bar (Overview | Users) on all admin pages plus a
    breadcrumb trail (Users → {email} → session) for the drill-down path;
    "← back" affordances follow the breadcrumb. The "Admin" nav entry from
    FR-30 points at `/admin`.
  - Empty, loading, and error states exist on every view; all admin pages
    are gated exactly as FR-30's admin nav (cosmetic client check; server
    enforcement is FR-28's `get_current_admin` on every endpoint, with
    FR-38 scoping what those endpoints can read). Finer visual design is
    not specified; NFR-3 (mobile browsers) applies, though admin pages are
    desktop-first — tables may scroll horizontally on phones rather than
    reflow.

Retention: `usage_events` and `turn_metrics` are kept indefinitely — at
current scale they grow by kilobytes per session, and raw usage is the audit
trail. Revisit trigger (recorded here deliberately): when either table
passes ~1M rows or the database exceeds ~1 GB, add monthly rollups and purge
raw rows older than 90 days. Both tables are user-keyed and therefore join
NFR-7's delete-all-my-data cascade whenever that FR is implemented.

---

### 4.10 Agentic Loop

Purpose: take ownership of the agent's turn. Today the pipeline's built-in
LLM stage makes one call per turn and Pipecat's framework machinery handles
the single tool it knows about; this feature replaces that stage with an
**agent loop we control** — within one turn the agent can reason, call
tools, observe their results, and chain further calls before and while
speaking. It is the foundation the next features build on: the artifacts
workspace (roadmap feature 4) becomes tools of this agent, the context
engine (5) replaces this feature's deliberately-trivial context assembly,
and memory (6) plugs into the seams both establish. Three design principles
govern every FR below:

1. **One hot-path LLM call in the common case.** The turn's critical path
   (end-of-speech → first audio, NFR-1) contains exactly one LLM call — the
   one whose first streamed sentence goes to TTS. Everything else is either
   deterministic code (context assembly, token math — microseconds, no LLM)
   or overlaps speech that is already playing. A "decider" LLM call in
   front of the speaking call is explicitly rejected: it is serial latency
   with no offsetting benefit when the speaking model (Gemini Flash) already
   selects tools and produces text in the same call.
2. **The loop is the brain; Pipecat stays the chassis.** Transport, audio,
   STT/turn detection, TTS, sentence aggregation, and barge-in interruption
   propagation remain Pipecat's — solved problems, irrelevant to agent
   quality. Only the LLM stage is replaced. The loop itself is plain Python
   behind our own seams: if Pipecat is ever retired, the loop walks away
   intact and only the audio chassis is rebuilt.
3. **Tools are the contract; context sits behind a seam.** The loop never
   assembles its own messages (it asks a context provider) and never
   hardcodes a tool (it consults a registry). Features 4–6 extend the
   registry and swap the provider without touching loop control flow.

Out of scope for this feature, deliberately: context sections/budgets/
compression (feature 5), memory tools and retrieval (feature 6),
second-model delegation for background jobs (§6), and proactive tool use —
FR-7 still governs: the agent acts when the user asks.

- **FR-42** The loop replaces the pipeline's LLM stage: a custom Pipecat
  processor (`agent/loop.py`) sits exactly where the provider LLM service
  sat — user-turn text in, streamed text frames out to the existing
  sanitizer → TTS chain. Loop semantics (normative; the reference
  pseudocode below is part of this FR): each **step** builds messages via
  the context provider (FR-44), makes one streaming LLM call, and forwards
  text deltas downstream *as they arrive*; if the step ends with tool
  calls, the loop executes them (concurrently when there is more than one),
  appends the calls and their results to the context, and begins the next
  step; a step ending with text only ends the turn. The **greeting** is a
  named entry point, not an accident: on client connect the loop runs one
  step with no user turn appended (today's `LLMRunFrame` semantics) — the
  agent speaks first. Hard bounds, enforced in code and surfaced as named
  constants: **max steps per turn** (default 5; the true ceiling is
  MAX_STEPS + 1 LLM calls, because hitting the cap triggers one forced
  final step — made with **tool selection forbidden, tools still
  declared** (`tool_choice: none` / mode `NONE`, translated per provider
  by the FR-48 factory — the portable form: by cap time the context
  necessarily holds tool-call/result turns, and Anthropic rejects such a
  request with no tools declaration, so "omit the tools" works on Gemini
  and 400s on exactly the swap C-2 names), its "wrap up now" instruction
  injected into that one call's message list and never appended to the
  context, so later turns aren't built atop a stale wrap-up order) and **per-tool timeout**
  (default 10s — a timeout is a tool *result* saying so, fed back to the
  model, never an exception). Failure discipline: any loop-internal error
  (LLM call failure, tool handler crash) degrades to a brief spoken
  fallback and a clean end of turn — a broken tool must never kill the
  session. **An empty step is a failure, not an ending**: a response with
  neither text nor tool calls (a blocked or truncated generation —
  Gemini's SAFETY/RECITATION/MAX_TOKENS-with-no-parts outcomes are
  returns, not exceptions, so the error path above never fires on its
  own) takes the same spoken-fallback path, distinguished via the
  **finish reason FR-48's client exposes**, and **no empty assistant
  message is ever appended** — an empty-content message 400s every later
  request on the Claude swap. Mandated tests: (a) a model that keeps calling tools terminates
  at the cap with a spoken, text-only turn — never silence, never an
  unbounded chain; (b) a handler hanging past the timeout yields a timeout
  result the next step sees, and the turn still speaks; (c) a handler that
  raises → spoken fallback, clean end of turn, and the session takes the
  next turn normally (the primary error path); (d) the loop's direct
  recorder calls pass NFR-10's fake-queue/no-database test split, which
  names this loop as its intended future subject; (e) the greeting —
  client connect with no user turn appended → one step runs and speech is
  produced (the silent-session regression is otherwise invisible until
  launch); (f) a blocked/empty response → the fallback line is spoken,
  nothing is appended to the context, and the session takes the next
  turn normally.
  Implementation MUST begin with a **spike** proving the stage swap: a
  custom processor in the LLM slot streaming text downstream with sentence
  aggregation, TTS, and barge-in interruption all intact. That is the
  load-bearing assumption of principle 2; if Pipecat's interruption
  machinery fights a custom stage, the documented fallback is running the
  loop as a session task outside the pipeline with a thin bridge processor,
  and the spike decides.
- **FR-43** Speak-while-working: multi-step work must never delay first
  audio. The first streamed sentence of step 1 reaches TTS immediately
  (NFR-2) and tool execution overlaps speech already playing. The system
  prompt directs the model to say a short, natural acknowledgment before
  invoking anything slow ("let me write that up—") — but the prompt is
  guidance, not the guarantee: Flash routinely emits a tool call as its
  entire first chunk, and a prompt-only rule would leave the user in
  silence for up to a full tool timeout. The guarantee is a
  **deterministic backstop in the loop**, and its trigger must beat the
  slow part: a tool call's *arguments* stream too — `create_artifact`
  carries the whole artifact body, 2–6s of generation — so waiting for
  the completed tool round is already too late. The backstop fires on
  whichever comes first: the **first tool-call delta** in the stream (the
  call's name surfaces before its arguments finish — *if* the provider
  streams calls incrementally, which the FR-48 spike must determine; if
  Gemini delivers the `functionCall` atomically, the deadline is the only
  real backstop and the spec says so), or a **deadline** —
  `FILLER_DEADLINE_MS` (default 700) after the call starts with nothing
  yet forwarded downstream. And the condition is **state-based, not
  step-numbered**: a filler is owed whenever a tool round is beginning
  and no speech is playing or queued — a tool-only step 3 after step 1's
  sentence finished playing is the same dead air as a silent step 1.
  "Playing or queued" has a named source, because the loop sits
  *upstream* of TTS and cannot know it for free: the loop derives
  playback state from the text it has forwarded downstream and the
  bot-started/stopped-speaking frames flowing back through the pipeline —
  an explicit FR-42 spike criterion, not an assumption. The
  filler comes from a small named set (`FILLER_LINES`, varied to avoid
  repetition; generic and topic-agnostic by design), travels **through
  the same downstream text-frame path as model text** — never a side
  channel to TTS, so FR-20's transcript log and FR-46's spoken-prefix
  stream both see it — and enters the context as the **prefix of that
  step's `step_text` in `append_step`** (never a separate append: two
  consecutive assistant messages are rejected by Anthropic and silently
  merged by Gemini; prefixing keeps one assistant message per step, which
  is also how the next step knows it already said "let me pull that up").
  The filler counts as first audio for NFR-1 — dead air is what the user
  experiences — but it must not launder slow turns as healthy: the FR-47
  row records a filler-led turn (a `step.N.filler` entry), and **FR-36
  and FR-37 are amended accordingly** (not merely enabled): the
  drill-down surfaces filler-led turns, and the breach analysis
  distinguishes "spoke fast" from "spoke a canned line while 6 seconds
  of tool work ran." Mandated test — against a *slow*
  stream, not an instant fake: filler audio is emitted while the step-1
  stream is still producing tokens. The no-tool turn — the
  overwhelming common case — must behave exactly as today: one LLM call,
  no added synchronous work (NFR-10 applies to the loop itself; anything
  the loop does beyond the call and the enqueue-only recorders is hot-path
  budget). Later steps' text streams the same way; NFR-1's 3s budget binds
  end-of-speech → *first* audio, and per-step latency is instrumented
  (FR-47) so C-1's one-second-per-stage advisory extends to each step and
  each tool.
- **FR-44** Context behind a seam: the loop obtains its messages
  exclusively from a **context provider** (`agent/context.py`) with a
  narrow interface — build the message list; append a user turn; append a
  completed **step** (the assistant message with its text *and* its tool
  calls, plus every call's result — one atomic append, see FR-42's
  pseudocode); **update a previously appended tool result in place** (the
  path by which FR-46's late write completion lands without a tail
  append); append the final assistant text. The v1 implementation is
  deliberately trivial and behavior-identical to today: system prompt
  (plus the FR-21 documents block) followed by the linear in-session
  conversation (FR-14). What retires and what stays is precise, because
  the aggregators do two different jobs: **`LLMContext` and the
  assistant-side aggregator retire** (context bookkeeping — the provider
  owns it now); **the user-side aggregator stays** — it is *turn
  assembly*, which principle 2 assigns to Pipecat: it carries
  `ExternalUserTurnStrategies` because Flux does its own end-of-turn
  detection, and it is what folds a stream of transcription frames (late
  finals included) into one user message. The reset (below) needs a
  boundary against those late finals: a straggler final arriving *after*
  the turn was consumed and the scratch reset is **dropped and logged**,
  never emitted as a second fragment-only user turn. One conversation
  store, not two: the retained aggregator's `LLMContext` (it is constructed from
  one) is **turn-assembly scratch only** — the loop reads the assembled
  user message off the frame the aggregator emits and never reads that
  context as history; nothing else may read it either, **and it is reset
  after each consumed turn** — read discipline stops divergence, only the
  reset stops the session from silently carrying a second, unbounded copy
  of the conversation in sensitive text nothing owns or trims. The FR-42
  spike names the frame
  type the loop consumes — that is its pass/fail criterion for this
  wiring. Mandated tests: (i) after a tool step, `context.build()` places
  the assistant message carrying the calls immediately before its
  results, one result per issued call, in step order — the message-shape
  rule the C-2 swap depends on, no provider needed; (ii) a no-tool built
  context matches today's `LLMContext` message list — the
  behavior-identical claim, asserted rather than assumed. This
  seam is exactly where feature 5 installs sections, budgets, and
  compression and feature 6 fills a retrieved-memory slot — with no change
  to loop control flow. The provider logs a **locally computed, explicitly
  approximate** token estimate of every built context (character
  heuristic — never a provider `count_tokens` call, which is a network
  round trip per step on the hot path), as a structured `context.built`
  event carrying `session_id`, `turn_id`, step number, and
  `approx_tokens` — so growth is visible long before feature 5 manages
  it.
- **FR-45** Tools are a provider-neutral registry (`agent/tools.py`): each
  tool = name, JSON-schema parameters, an async handler, and a **read or
  write** classification (consumed by FR-46). `agent/providers.py`
  translates the registry to the LLM's native tool-calling format — C-2
  holds: swapping the LLM touches one factory, zero tools. Handlers
  receive from session state — never from the model — the verified
  `user_id`/`session_id` **and a loop-injected server-message emit
  callback** (the seam for `create_artifact`'s panel announce: today it
  reaches the data channel through the LLM service this feature deletes;
  the callback keeps Pipecat types out of `agent/tools.py` and gives the
  post-interruption announce a defined path instead of an assumed one).
  On `user_id`, the provenance rule holds —
  **never from model-supplied arguments**: the model chooses *which*
  artifact, the handler resolves it through the user-scoped query (RLS
  backstop), and NFR-8's negative test is mandated — a handler handed
  another user's artifact id reports not-found. The queries themselves live
  in `db/` (an artifacts repo with the standard discipline — `user_id: str`,
  no default), not in the tool module: tools stay schema-shaped, the repo
  layer owns filtering, ordering, and caps. The v1 inventory (the
  minimum that makes multi-step real):
  - `create_artifact` — migrated from the current implementation with
    behavior unchanged: artifact row + `artifact_created` usage event in
    one transaction (FR-32), RTVI announce to the panel (FR-12), verbal
    confirmation.
  - `list_artifacts` — the user's recent artifacts across sessions: id,
    kind, created_at, **and title — which is content-class, not metadata**
    (the schema marks it 🔒, and §4.9 keeps it off every admin surface):
    it flows only into the owner's own model context, never into logs.
    Newest first, capped (default 20). This is deliberately the agent's
    first cross-session capability: it costs nothing and previews the
    memory feature's value.
  - `read_artifact` — the full content of one owned artifact, truncated at
    `READ_ARTIFACT_MAX_CHARS` (default 8,000 — roughly a long artifact,
    safely inside the model's window) with an explicit "[truncated]"
    marker.
  Tool results are structured JSON. Artifact titles/content flowing into
  the model's context is the owner's own data in the owner's own session
  (NFR-5 covers the processing disclosure); logs carry tool names, ids,
  durations, and outcomes — never arguments or content (NFR-9 discipline).
- **FR-46** Barge-in vs. in-flight work (extends FR-13): on interruption,
  the current LLM stream is cancelled and queued speech discarded (today's
  behavior); **pending and in-flight read tools are abandoned — their
  results discarded, not force-cancelled mid-statement** (cancelling a
  coroutine inside a DB query can hand a poisoned connection back to the
  pool; v1's reads are millisecond queries whose results are merely
  unwanted, so abandon-and-discard is simpler and equally correct);
  **an in-flight write tool runs to completion** — a half-done write is
  worse than a moot one, and `create_artifact`'s transaction is atomic
  either way. "Runs to
  completion" has a named owner, or it is unimplementable: **write
  handlers execute in tasks the loop creates *outside* the pipeline's
  cancellation scope** (End-tap, disconnect, and barge-in all reach
  `task.cancel()`, and a processor-owned task would take a
  `CancelledError` mid-commit — rolling back the very write the rule
  protects), tracked in a **session-level in-flight-write set**;
  interruption cancels only the read set, and teardown awaits the write
  set. **Where the late result lands is defined by the atomicity rule,
  not against it**: at interruption the step is appended immediately with
  a `{"status": "in_progress"}` placeholder for the still-running write
  (cancelled reads get `{"status": "cancelled"}`), and the write's
  completion **updates that result in place** via the FR-44 interface —
  never a tail append, which after the user's next turn would create
  exactly the orphaned `tool_result` the atomic-step rule forbids. A
  post-interruption write completion spawns **no further LLM step**; the
  panel announce fires through the FR-45 emit callback **while the client
  is still connected** (the barge-in case) — on the teardown paths the
  client is gone, so the announce is skipped, and the callback must be
  **safe to call from a detached write task after the pipeline closed**
  (a silent no-op, never an unretrieved exception in a task nothing
  awaits). **What
  the context holds after an interruption is defined, not inherited**:
  the assistant's entry records the *spoken prefix*, marked interrupted —
  taken from the sentence-level synthesis stream (the TTS service's
  output frames, the same stream FR-20 logs as `agent_text`), **with the
  filler excluded from this capture** — the filler's context entry is
  owned by FR-43's prefix rule alone, so an interrupted filler-led step
  records it once, never twice — which is an
  **explicit over-approximation**: it is what was *sent for synthesis*,
  not what was *heard* — playback lags it by the buffered audio that
  barge-in discards, so it can overstate by up to the flushed
  sentence(s). Accepted at sentence granularity for v1 (never the full
  generated text, never nothing); the word-timestamped TTS frame stream
  is the named upgrade path if the overstatement proves to matter. And
  **every issued tool call gets a terminal-or-placeholder result** — the
  context is never left in a half-round state no provider will accept.
  **Session teardown gets the same write protection as barge-in**, and
  the grace is enforced where cancellation actually happens — with **one
  budget, consumed once, never composed**: `WRITE_GRACE_S` (default 5s)
  is a single global allowance. On End-tap/disconnect, per-session
  teardown spends it awaiting the in-flight-write set *before* the
  recorders and transcript writer stop (a completed write's
  `artifact_created` event must land in a live queue — FR-38's artifact
  counts read that event, not the content table). On the SIGTERM path
  **the drain itself owns the wait, and the ORDER is the load-bearing
  part**: goodbye → **await every session's in-flight-write set** (via
  the session-level write registry, which is drain-reachable by
  construction) under the one total budget → **only then**
  `task.cancel()` — because the drain's own cancel is what *triggers*
  per-session teardown, and waiting after cancelling would enqueue the
  completed write's `artifact_created` into recorders already stopping:
  a silent, unlogged drop. Per-session teardowns after the drain **do
  not wait again**; a write the expired budget abandons is **cancelled
  and logged at WARNING with its `session_id`** — its only trace must
  never be asyncio's destroyed-task noise. And the window is
  provisioned by arithmetic, not assertion — every teardown database
  operation is **deadline-bounded** (each background writer's stop
  flushes under `FLUSH_TIMEOUT_S`; `end_session_row` gets an explicit
  bound of 2s), and the worst-case sum is the spec for the window:
  0.5s goodbye + 5s grace + ~11s of bounded flushes (two writers, two
  flush attempts each in the DB-degraded case) + bounded row closes
  ≈ 19s → `docker-compose.prod.yml` sets **`stop_grace_period: 30s`**
  (margin included; Docker's 10s default is what SIGKILL — the outcome
  this clause exists to prevent — was measured against). Usage follows FR-33's rule — *spent is recorded* —
  including partial usage of cancelled steps when the provider reports
  it. Mandated tests: interrupt mid-chain — the write completes, the
  context records the spoken prefix, the cancelled reads' terminal
  results, and the write's in-place-updated result, and no further LLM
  call is issued; End-tap mid-write — the artifact row and its usage
  event both land; **SIGTERM mid-write (the twin)** — the drain awaits
  before cancelling, and the artifact row and its usage event both land.
- **FR-47** The loop instruments itself (this is §4.9 principle 3's
  anticipated shift from observers to direct recorder calls): per-step
  structured logs (`agent.step`: session_id, turn_id, step number, TTFB ms,
  tool-call count; `tool.invoked`: session_id, turn_id, name, duration ms,
  ok/error/timeout) and LLM usage recorded per call directly from the
  provider's response through the existing enqueue-only recorder (NFR-10
  unchanged; FR-32 is amended to match). LLM usage is recorded in
  **exactly one place** — the loop; the metrics-frame observer keeps TTS
  only, so nothing double-counts. The stage swap's effect on FR-33 is
  stated positively, not assumed away: the end-of-speech → first-audio
  measurement survives because the latency observer reads *speech* frames,
  which the swap does not touch — but `stages_ms`' LLM and tool components
  today come from the very LLM service being removed, so **the loop owes
  the FR-33 row its per-step LLM TTFBs and per-tool durations — and the
  row has exactly ONE writer, with a stated handoff** (the row can't be
  split: `eot_to_first_audio_ms` is NOT NULL and only the latency
  observer measures it, at first audio mid-turn; the stages exist only in
  the loop, at turn end): the observer's role shrinks to *measuring* —
  on first audio it hands its e2e milliseconds **and the turn number
  current at measurement time** (the tracker advances before an
  interrupted flush, so the number must be captured with the
  measurement, not at write time) into the recorder's per-turn buffer;
  the **loop flushes that buffer as the single `turn_metrics` write at
  turn end** — **gated on a measurement existing, not on audio**: the
  greeting turn has first audio but no end-of-speech to measure (the
  same guard today's observer carries), so it writes no row, exactly
  like a turn interrupted before audio; both leave their steps in
  `agent.step` logs only, and the NOT NULL column is never fabricated
  as 0 (which would poison FR-37's p50 and breach count) nor violated
  (which would take the whole write batch down). **The buffer's reset
  boundary is explicit**: every stage entry carries the turn number
  current at its step; the flush writes only entries matching the
  measured turn; and the buffer clears at every turn end — row-writing
  or not — so an interrupted no-row turn can never credit the next
  turn with a tool it never called. **FR-33 is amended to match** (its "written from the breakdowns the latency
  observer already computes" described the old writer), exactly as FR-32
  was. Stage entries use **step-indexed keys** (`step.1.ttfb.llm`,
  `step.2.tool.read_artifact`, `step.N.filler` when the FR-43 backstop
  led) so multi-step turns and repeated tools cannot silently overwrite
  each other in a flat dict. FR-33's no-first-audio rule is unchanged (a
  turn interrupted before any audio still writes no row; its step
  timings live in `agent.step` logs only). **CLAUDE.md's escalation rule
  transfers with the measurement, not silently dropped with the
  observer**: any single step's LLM TTFB or any tool duration over 1s
  logs **WARNING** naming the guilty stage (C-1's threshold, today
  computed by the very breakdown handler being displaced), and the
  end-to-end over-3s ERROR stays with the observer. "Missing must not
  read as healthy" has a render-site mechanism, not an intention:
  FR-36's drill-down shows an absent expected key as "—" (missing data),
  never as zero or omitted-therefore-fine — and the render distinguishes
  key **vocabularies**, not just presence: rows written before §4.10
  carry the old flat keys (`ttfb.llm`, `tool.{name}`) and render under
  that legacy vocabulary; "expected but absent → —" applies within the
  step-indexed vocabulary only, so history doesn't retroactively render
  as broken. Pipecat's turn tracker — the
  source of every `turn_id` via `on_turn_started` — is frame-flow-driven
  and must survive; both survivals are verified in the FR-42 spike, not
  assumed. Mandated tests (this FR's failure modes are all silent):
  (i) a 3-step turn with a repeated tool produces ONE row whose
  step-indexed keys cover every step and both tool invocations; (ii) a
  turn interrupted before first audio writes no row but its steps appear
  in `agent.step` logs; (iii) a tool duration over 1s logs WARNING;
  (iv) LLM usage for a multi-step turn lands exactly once per call —
  no double-count with the metrics-frame observer.
- **FR-48** The loop calls the LLM through a thin provider-agnostic
  streaming client built by an `agent/providers.py` factory, exposing
  four things: text deltas, tool calls, usage counts, and a **finish
  reason** (normal stop vs. blocked vs. truncated — without it the loop
  cannot tell an empty ending from a blocked generation, FR-42's
  empty-step rule). Gemini first
  (the provider SDK lives inside the factory, per the hard constraint);
  Claude remains the runner-up swap. Implementation MUST begin with a
  **spike** proving streamed text + native function calling + usage
  metadata through this client against the live Gemini API before the
  loop is built on it — and the spike must additionally **determine
  whether function calls are delivered incrementally or atomically** in
  the stream: FR-43's first-delta trigger exists only if the former; if
  the `functionCall` arrives whole, `FILLER_DEADLINE_MS` is the only
  real backstop and the implementation leans on it knowingly.

**How the loop works — reference pseudocode and knobs.** Normative for
FR-42; kept here so the mechanism is editable knowingly.

```
on user turn (assembled by the user-side aggregator, FR-44):
    context.append_user(turn_text)
    run_turn()
on client connect (the greeting — no user turn appended):
    run_turn()

run_turn():
    for step in 1..MAX_STEPS:
        messages = context.build()            # deterministic, no LLM
        stream  = llm(messages, tools)        # ONE call: text and/or tool calls
        forward text deltas downstream        # TTS starts at first sentence
        # FR-43 backstop, state-based: on the FIRST tool-call delta (if
        # the provider streams calls incrementally — FR-48 spike), or
        # FILLER_DEADLINE_MS with nothing forwarded — if no speech is
        # playing or queued, emit a FILLER_LINES entry THROUGH the normal
        # downstream text-frame path (FR-20/FR-46 both read it) and
        # prefix it onto this step's step_text (one assistant message per
        # step — never a separate append). Tool ARGUMENTS stream too (an
        # artifact body is seconds of generation) — waiting for the full
        # call is too late.
        if stream yielded tool calls:
            results = run handlers            # concurrent; TOOL_TIMEOUT_S each
                                              # writes: loop-owned tasks OUTSIDE
                                              # pipeline cancellation (FR-46)
            context.append_step(step_text, calls, results)
            #   ^ ATOMIC and COMPLETE: the assistant message carrying this
            #     step's text AND its tool calls, then every call's result —
            #     one assistant message per step, in step order. Results
            #     without their owning assistant message are unsendable
            #     (Claude rejects orphan tool_results; Gemini defines
            #     functionResponse relative to the functionCall turn).
            continue                          # next step sees the results
        context.append_assistant(final_text)  # the closing text-only step
        return
    # cap hit: one forced final call — tools DECLARED but selection
    # forbidden (tool_choice: none; portable, FR-42) — ephemeral wrap-up
    # instruction (never appended) → append_assistant(final_text)

at turn end (ANY path): if a MEASUREMENT exists (end-of-speech → first
    audio; the greeting and pre-audio interruptions have none → no row,
    steps to logs only), flush the recorder's per-turn buffer
    (observer-measured e2e ms + turn number captured at measurement,
    plus the loop's step timings for THAT turn) as ONE turn_metrics
    row, step-indexed keys — then CLEAR the buffer either way (a
    no-row turn must not credit the next turn's row) (FR-47).

on interruption (FR-46): cancel stream + read tools; writes finish
    (loop-owned tasks, outside pipeline cancellation). Append the step
    NOW: spoken prefix (sentence-granularity over-approximation, marked
    interrupted), {"status": "cancelled"} for reads,
    {"status": "in_progress"} for the running write — whose completion
    UPDATES that result in place (FR-44). Never a tail append, never a
    half-round context.
on session end: ONE WRITE_GRACE_S budget, spent once — End-tap/disconnect:
    per-session teardown awaits the in-flight-write set BEFORE the
    recorders/writer stop; SIGTERM: the drain owns the wait across all
    sessions and per-session teardowns do NOT wait again. The whole
    shutdown fits stop_grace_period=15s (FR-46).
```

| Knob | Default | Where |
|---|---|---|
| `MAX_STEPS` per turn | 5 | `agent/loop.py` |
| `TOOL_TIMEOUT_S` per tool | 10 | `agent/loop.py` |
| `LIST_ARTIFACTS_CAP` | 20 | `db/` artifacts repo (FR-45 assigns caps to the repo layer) |
| `READ_ARTIFACT_MAX_CHARS` | 8,000 | `agent/tools.py` (result truncation — a tool concern, not a query one) |
| `FALLBACK_LINES` (spoken failure/empty-step lines) | small named set | `agent/prompts.py` |
| `FILLER_LINES` (speak-first backstop; generic, topic-agnostic by design) | small named set | `agent/prompts.py` |
| `FILLER_DEADLINE_MS` (speak-by deadline, any silent tool round — state-based per FR-43) | 700 | `agent/loop.py` |
| `WRITE_GRACE_S` (teardown wait for writes) | 5 total | `agent/companion.py` (the code that awaits: session teardown + the SIGTERM drain; the loop only owns the in-flight set) |
| Model / provider | `LLM_MODEL` / `LLM_PROVIDER` env | `agent/providers.py` |
| Voice + tool-use behavior | system prompt | `agent/prompts.py` |

---

## 5. Non-Functional Requirements

### 5.1 Latency
- **NFR-1** Time from end-of-user-speech to first audio from the agent must be under 3 seconds under normal network conditions.
- **NFR-2** Audio must stream as it is generated. The agent must not wait until its full response is ready before speaking.
- **NFR-10** Measurement must be free: capture (§4.9) adds no synchronous work to the voice hot path. The principle, independent of pipeline architecture: code on the hot path only ever enqueues to memory; background writers batch queues into the database; a failed write logs an error and drops the batch rather than disturbing a live session. Test approach: the same split the transcript-writer tests use — observers/recorders are tested against a fake queue with no database available (proving the enqueue path touches nothing), and writers are exercised separately, including the broken-database case completing without raising. (The FR-20 transcript writer is the exemplar of this discipline, not its definition — it applies equally to a future custom agent loop, where observers become direct recorder calls.)

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

- **Cross-session memory** (formerly FR-15–FR-17). The agent starts every session fresh; *automatic* recall is in-session only. Planned later following the MemGPT framework, possibly integrating RAG with clever indexing and semantic vector search, depending on performance. One deliberate carve-out (§4.10, FR-45): the explicit, user-asked artifact tools (`list_artifacts`/`read_artifact`) do reach the user's own artifacts from past sessions — that is a narrow, on-request read, not memory.
- **Streaming memory processing.** When cross-session memory lands, it must run in parallel while the user is still speaking — context editing during input, not after the session ends.
- **Document persistence.** Uploaded documents (FR-21) are ephemeral for the MVP — held in memory for the session and discarded when the process restarts. When cross-session memory lands, documents will persist alongside the conversation.
- **Proactive flagging** (formerly FR-8; demo stretch goal). The agent surfacing gaps, contradictions, or connections unprompted, with a user-configurable on/off setting.
- **Brainstorm/critique mode inference** (formerly FR-10; demo stretch goal). Distinct generative vs. analytical behavior, inferred from context or set explicitly.
- **Session resume** (formerly FR-19 / NFR-4). A dropped connection ends the session; the user starts a new one.
- **Encryption at rest** (deferred from NFR-6). The schema keeps sensitive content in dedicated columns so encryption can be added post-MVP without rework; the encryption itself is not in the MVP.
- **Name personalization** (deferred from FR-24). The user's stored preferred name is injected into the agent's system prompt at session start so the agent addresses them by name. Small change once auth lands: `/start` already resolves the user, and the system prompt is already built per session.
- **Second-model delegation** (noted in §4.10). Cheap/fast models for background jobs — summarization, memory extraction, long artifact drafting — where latency doesn't matter and cost does. Arrives with the features that create those jobs (context engine, memory); the speaking call stays on the main model either way.

---

## 7. Constraints

- **C-1** Voice I/O pipeline latency is the binding constraint on model and architecture choices. The total budget from end-of-speech to first audio is 3 seconds. Any single component consuming more than ~1 second of that budget is a candidate for replacement.
- **C-2** The LLM provider must be swappable without rewriting session logic, memory, or API routes. Provider-specific code is isolated to a single agent class.
- **C-3** The product must never describe itself or its agent as a therapist, counselor, or mental health resource — in UI copy, system prompts, or onboarding.
- **C-4** Auth secrets (the Firebase service-account key) live outside the repo. Env var names are documented in `.env.example`; values are never committed.

---

