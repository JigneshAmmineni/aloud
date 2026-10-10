# Aloud — Roadmap

A loosely guiding document: intended feature order, vision, and direction —
**not** a source of truth for what gets implemented. Each feature starts only
on the author's explicit instructions, and its contract is the FR section
added to [REQUIREMENTS.md](REQUIREMENTS.md) at that point (numbered FRs with
acceptance criteria). PRs reference the FRs they implement; reviews check the
diff against those FRs, not against this document. Items marked *optional*
here are aspirational notes and carry no weight in specs or reviews.

## Vision

Aloud is a voice-first work assistant for people who think and work by
talking out loud. The finished product: you open it on any device and your
work becomes a conversation with an assistant that does the tiny details.
Reading becomes an audiobook — or listening to an expert who has read what
you need to read and can summarize, recite, or answer questions about it.
Writing becomes talking your ideas through, letting your train of thought
run, and bouncing them off an assistant that puts them down coherently —
you review the write-up, go over specific lines or phrasing as needed, as
hands-on or as imprecise as you want. The agent — which remembers your
past sessions, your documents, and your open threads — structures your
creativity, and asks sharp questions or pressure-tests your plans when you
invite it to. Multi-user, private by design: every user's conversations,
memories, and documents are theirs alone.

## Feature order

The order is intentional: auth establishes `user_id`, which everything after it
hangs on (per-user metrics, per-user documents, per-user memory isolation).
The agentic loop (3) lands before the artifacts rework (4) so the document
workspace can be defined as **tools of the agent** rather than bolted onto
the pipeline's built-in function calling. The context engine (5) lands before
memory storage/retrieval (6), because retrieval injection and
memory-management writes both depend on a context window we control
section-by-section — and it builds inside the loop feature 3 establishes.

### 1. Auth — individual accounts

Replace the single site-wide password gate with real per-user accounts.

- Every request resolves to a verified `user_id` at the HTTP boundary via one
  FastAPI dependency (`get_current_user_id`) — the only auth-aware code in the
  backend. Repo functions take `user_id: str` and never know where it came from.
- `user_id` always originates from server-side credential verification, never
  from anything the client sends in a payload.
- Provider: Firebase Auth — Google sign-in + email/password, open signup.
  Bearer ID tokens verified server-side via firebase-admin inside the
  dependency; the seam keeps the provider swappable.
- Admin controls (gated to admin accounts only): list users and
  disable/enable them. Usage views arrive with feature 2, which grows this
  admin surface.
- Postgres row-level security turns on when multi-user lands — not deferred.
- *Optional:* account-linking UI — a settings flow to link/unlink providers on
  one account (e.g., add a password to a Google account). Aspirational note
  only; not part of any spec or review until explicitly instructed.

### 2. Observability — per-user usage & admin view

An admin-only surface answering "who uses this, and what does it cost?"

- Searchable list of all users.
- Per-user token/usage metrics for each pipeline stage: STT (audio minutes),
  LLM (input/output tokens), TTS (characters) — and the cost they imply.
- Standard product/ops metrics per user: session counts, session durations,
  last-active, error rates, per-stage latency percentiles (the instrumentation
  already required by CLAUDE.md becomes queryable here).
- Exact metric set is decided when this feature is specced.

### 3. Agentic loop — the agent owns its turn

Replace the pipeline's single-shot LLM stage with an agent loop we control:
within a turn the agent can reason, call tools, observe results, and chain
further calls — instead of today's one LLM call whose function calling is
handled inside Pipecat's LLM service.

- STT / TTS / transport stay on Pipecat; the loop replaces the LLM stage
  only. (This resolves the spec-time decision previously parked under the
  context engine; the retired SDD-v2.md in git history is background.)
- Tools become the loop's contract: today's `create_artifact` migrates onto
  it, and feature 4 then defines the document workspace as tools of this
  agent (list / read / create / edit).
- Provider-agnostic like everything else: tool schemas and loop control stay
  behind the provider seam — no SDK calls outside `agent/providers.py`.
- The voice hot path keeps NFR-1: the first spoken sentence must not wait on
  a slow tool chain — speak-while-working semantics are a spec-time
  decision.
- Exact loop semantics, tool set, and failure behavior are decided when this
  feature is specced.

### 3.1 System instructions fine-tuning — the agent's conversational character

Next up. Small, prompt-only PR (plus tests pinning the new behaviors); no
loop or pipeline changes. Live testing of feature 3 showed the agent
steering the conversation too strongly — its default is pressure-testing
and challenging everything said, which makes it dominate.

Target character: **an intelligent, productive partner in conversation —
an assistant / junior exec the user works through — never the one leading
it.**

- Challenging and pressure-testing is something the agent does when
  *invited* ("poke holes in this"), not its default posture.
- During a brain dump / train of thought, minimal acknowledgments are the
  right move: "hmm", "that makes sense", "go on" — and it's fine to say
  nothing substantive until the user asks for thoughts.
- But direct questions and direct address always get a real answer:
  minimal acknowledgments are only for continuing *the user's* thread,
  never a substitute for responding when spoken *to*. (Live-test bug:
  "how are you" got "hm" — the current prompt gives the model a binary
  "brain dump → say hmm" with no guidance on telling direct address
  apart from a train of thought.)
- A pause to think is not an invitation: the agent must not fill the
  user's thinking pauses with questions or suggestions. (The prompt can
  only shape what it says when a turn does fire; if pause tolerance needs
  tuning at the turn-detection layer, `FLUX_EOT_THRESHOLD` is the knob —
  measure before touching it.)
- Questions stay sharp but rationed: asked when the agent genuinely has
  one, at most one at a time (FR-9 holds), never to keep the conversation
  moving.
- Where the pieces live is mapped in `agent/prompts.py` (base prompt,
  documents block, fallback/filler/greeting lines, wrap-up instruction)
  and `agent/tools.py` (tool descriptions — the model reads these too).
  All of them get vetted in this pass, with A/B listening tests against
  real brain-dump sessions as the acceptance bar — the test plan, scripted
  scenarios, and results live in
  [docs/evals/2026-09-16-system-prompt-ab.md](docs/evals/2026-09-16-system-prompt-ab.md).
  STATUS (2026-10-08): one listening iteration ran; its fixes shipped as
  prompt v2 with the 3.1/3.2 PR, and the comparative A/B is PAUSED — UX
  fine-tuning, not a blocker; resume from the eval doc.

### 3.2 Product repositioning — from "thinking partner" to work assistant

Not part of the feature-3 PR (identity cuts across prompts, docs, and
user-facing copy; its prompt half belongs with 3.1's pass — implement the
two together or back-to-back). The user's brief, captured verbatim in
intent:

> An experience where your work becomes a conversation with someone that
> is doing the tiny details. Your reading becomes an audio book, or
> listening to an expert who read what you need to read and can
> summarize, recite, or answer questions. Writing becomes discussing your
> ideas, letting your train of thought run, and having an assistant you
> can bounce ideas off of, who will put them down in writing in a
> coherent way — and you can review the writing, go over specific lines
> or phrasing as needed. You can be as hands-on or as imprecise as you
> want, and the assistant will structure your creativity.

The rules:

- Every description of the agent's overall purpose/experience changes
  from "thinking partner" (and any "journal" framing, should it appear)
  to **work assistant** in the sense above: the user works out loud, the
  assistant does the tiny details and structures the creativity.
- Pressure-testing and guiding questions REMAIN capabilities — do not
  remove them or disclaim them — but they are no longer the identity or
  the selling point, so no headline description should lead with them.
- Where the old identity lives (the full inventory, main tree):
  - `backend/agent/prompts.py:7` — the system prompt's identity line
    (the agent's self-concept; coordinate with 3.1's behavior pass)
  - `CLAUDE.md:5` — the "What this is" paragraph
  - `README.md:3` — the tagline
  - `REQUIREMENTS.md:5` — the purpose statement
  - `ROADMAP.md:13` — the Vision section (rewrite around the brief above)
  - `frontend/app/layout.tsx:21` — the user-facing meta description
  - `.github/workflows/claude-code-review.yml:38` — the reviewer's
    product context. Applied DIRECTLY ON MAIN after the 3.1/3.2 PR
    merges, not in it: the review action refuses to run on a PR that
    edits its own workflow (tamper guard), so carrying this line in the
    PR silently skips its entire review.
  - `docs/notes/memory.md` — passing mention in a scaling note
- C-3 still binds everywhere: never therapy/therapist/counselor.

### 4. Documents & artifacts rework

From single upload-at-start + copy-paste artifacts to a real document workspace.

- Two side-by-side scrollable lists: **user-uploaded** documents and
  **agent-produced** documents.
- Clicking a document opens a preview.
- The agent can edit these documents, and the user sees changes live in the
  preview. Markdown first; other file types as the engineering allows.
- **Workspace v2 (own spec, §4.12 — first follow-up after the feature-4 PR
  lands; decided 2026-10-10):** Claude-desktop-style file UI and direct
  editing. Names-only file list (metadata columns from v1 retire; folders
  maybe, as a nullable `folder` column); double-click opens a document in a
  docked editor pane, multiple open documents become tabs; preview/source
  toggle; CodeMirror 6 for editing md/text (pdf stays read-only); while the
  workspace is open the Talk orb docks into the bottom ~30% of the list
  pane, voice fully live. Floating drag/resize windows considered and
  rejected (web-MDI UX trap, dies on phones). PDF *output* ("make it a
  pdf") is its own small follow-up: client-side md→pdf export at download.
  - **Concurrency model (VS Code-style, decided over CRDT):** the server
    row is the buffer; the editor autosaves debounced (~1s), so the agent
    always reads live state; agent edits stay content-anchored
    (`str_replace`'s occurrence predicate is the version check); announces
    re-render the open editor. CRDT (Yjs) rejected: cheap client-side but
    the server would become a CRDT peer (update-log storage, agent ops
    translated) — replaces the storage model for a rare case.
  - **Race cases and their required resolutions** (the spec's checklist):
    1. User types, agent idle → autosave writes; no race.
    2. Agent edits, user buffer clean → announce re-renders; no race.
    3. Agent edits inside the user's unsaved window (≤1s + RTT) → client
       3-way merge (base = last synced, mine = buffer, theirs = announce);
       disjoint regions merge silently, overlapping regions get an
       explicit "agent edited this — apply / keep mine" banner. Blind
       apply loses keystrokes; blind ignore lets the next autosave erase
       the agent's edit — both forbidden. Granularity (decided
       2026-10-10): LINE-level diff3 — overlapping changed line-hunks
       conflict, so the same line touched by both sides banners even
       when the words differ; word-level merging was considered and
       rejected (auto-stitching one sentence from two authors produces
       fluent nonsense; noisier banners are the honest trade).
       **Explicit spec decision: on conflict, USER edits take
       precedence.** Stated cost: the agent's edit landed server-side
       and reported success, so user-wins undoes it unless the
       banner's "apply theirs" is taken; the spec states this and the
       agent rediscovers on its next read.
    4. Autosave races the agent's UPDATE at the server. Autosave-first:
       the agent's `old_str` no longer matches → its edit refuses and the
       model re-reads (no loss). Agent-first: a naive full-content
       autosave would silently erase the agent's edit — so the save
       carries compare-and-swap on the base `updated_at`; a moved base
       refuses the save, the client merges (case 3) and retries.
    5. Two of the user's own tabs editing one document → same CAS + merge
       as case 4.
    6. Truly simultaneous same-region typing (user + agent in the same
       second) → degrades to case 3's banner; the only case real CRDT
       would fix, accepted as out of scope.
- Later, in two stages with different prerequisites (decided 2026-10-08):
  - **Mid-session uploads with on-request reads** — add a document while
    talking; the agent reaches it only when asked ("read the file I just
    uploaded"), through the same registry tools as any document row. NOT
    predicated on the context engine: once §4.11's documents table and
    upload persistence land, this needs only an in-session upload
    affordance (plus optionally a server-message nudge so the agent knows
    it arrived). First follow-up after feature 4 v1.
  - **Proactive pickup** (depends on the context engine, feature 5): the
    agentic loop notices a new document on its own, indexes it, and
    retrieves from it as the conversation calls for it. This is what
    replaces today's upload-before-session flow, which injects whole
    documents into the context window at session start and gets
    deprecated once this lands.

### 5. Context engine — structured, owned context window

Replace the loop's monolithic context with a context window we fully
control — the foundation the memory layer builds on. Builds inside the
agent loop (feature 3), which already owns context assembly per turn.

- Distinct, individually budgeted sections: system prompt, core memory
  (durable facts about the user), retrieved memory (populated by feature 6),
  and the current conversation.
- Auto-compression of the conversation section under memory pressure
  (recursive summarization: oldest turns compressed into summaries, raw turns
  evicted) — per docs/notes/memory.md's MemGPT-style sketch.
- Full programmatic control of context assembly each turn, with per-section
  token accounting and instrumentation.

### 6. Memory layer — cross-session memory

Tiered, MemGPT-style memory so the agent remembers past sessions and documents.
Builds on the context engine (feature 5): retrieval fills its retrieved-memory
section; the memory-management loop consumes its summaries and evictions.

- Storage/indexing structure and the retrieval mechanism are designed
  **together** — the structure is judged by the latency and quality of
  retrieval, including how retrieval triggers mid-speech without touching the
  voice hot path.
- **Documents arrive un-indexed (noted 2026-10-09):** §4.11 stores every
  document as plain extracted text in the `documents.content` column — no
  chunking, no embeddings, no vector index (FR-53 states this boundary;
  search is a literal substring scan). This feature derives chunks and
  embeddings *from* that text into its own index — a backfill over all
  existing rows plus indexing on write for new ones — not a migration: the
  plain-text column stays the source of truth, and any derived index (e.g.
  pgvector columns/tables) must be rebuildable from it.
- Strict per-user isolation: every memory row is scoped by `user_id` and
  covered by RLS; one user's agent can never retrieve another user's data.
- Retrieval quality is measured by evals (golden query→memory datasets), not
  just unit tests.

### 7. LLM tracing

Every LLM call recorded as a trace: full input messages, output, model, token
counts, latency, purpose (voice turn / memory loop / compression), linked to
`session_id`/`turn_id`.

- The substrate for evals (golden datasets, LLM-as-judge, provider
  comparisons) and for debugging the agent loop, context engine, and memory
  loop — complements feature 2's aggregate usage metrics.
- Trace content columns (inputs/outputs) separate from metadata, per the
  encryption rule.
- A minimal version may be pulled forward if debugging features 3–6 demands
  it.

### 8. Noisy-environment robustness — turn detection under real-world audio

Found live during 3.1 testing (2026-09-17): continuous background noise (a
treadmill) kept Flux convinced the turn was still open — end-of-turn never
fired, the agent never responded. Real-world usage means gyms, streets,
cafés; the harder variant is background *speech* between other people, not
addressed to the agent. Pinned for later; the layered plan from the
brainstorm, cheapest first:

1. **Diagnose the signature** per incident: turn open with ZERO transcribed
   words (pure detection stall) vs. garbage words (context pollution too) —
   `transcript_events` + logs already hold the evidence.
2. **Client mic constraints**: explicit `noiseSuppression`/
   `echoCancellation`, and experiment with `autoGainControl: false` — AGC
   amplifying the noise floor while the user is quiet is a prime suspect
   for keeping the turn open. (Today the client passes bare
   `enableMic: true`.)
3. **Flux knobs**: `min_confidence` (keep garbage words out of the
   context), `eot_timeout_ms` tuning — bounds borderline turns but cannot
   end a turn Flux still classifies as speech.
4. **Our own turn watchdog** (likely the durable fix, provider-agnostic):
   a turn open N seconds with zero (or no NEW) transcribed words is
   declared noise — reset it, or force the end-of-turn with what exists;
   plus a hard max-turn-duration ceiling against any pathological stall.
5. **Parallel Silero VAD cross-check**: Silero is speech-specific
   (treadmill = non-speech); "Flux says speaking, Silero says silence for
   X s" overrides. CPU cost on the e2-small is the constraint.
6. **Voice isolation** — the only real answer to background *speech*:
   Krisp-class primary-speaker isolation (client SDK offloads CPU) or,
   later, speaker enrollment/diarization.
7. **Product escape hatches**: push-to-talk / hold-to-talk mode for noisy
   environments; a visible "still listening…" state so a stalled turn is
   legible instead of feeling dead.

### 9. Prompt injection & PII protection layer

Placeholder by decision (2026-10-08): this layer needs to exist; its
exact requirements and implementation will be researched and specced
when picked up. The named concerns it must cover:

- **Prompt injection**: untrusted text reaching the model's context —
  uploaded documents, artifact content read back by tools, and any
  future retrieved memory — carrying adversarial instructions. Today's
  only mitigations are behavioral (the loop refuses tool calls on
  forbidden-selection calls; tool results are structured JSON).
- **PII protection**: what leaves the system and what is retained —
  provider egress (Deepgram/Google/Cartesia already disclosed per
  NFR-5), logs and traces (NFR-9 and the 🔒-column discipline exist;
  this layer decides detection/redaction on top), and any future
  third-party surface.

## Process infrastructure

Per-PR CI (ruff + pytest, frontend build) and the tailored Claude review are
live. Still to add, roughly when its prerequisite feature lands:

- **Promotion gate (main → prod):** before promoting, run the heavy suite
  per-PR CI deliberately skips — golden-audio E2E through the real providers
  (recorded utterances with known content; assert loosely: transcript
  keywords, a response produced, tool fired, latency within budget) — plus a
  manual talk-through. (The deploy itself is done: the "Deploy to
  production" workflow ships any chosen main commit — and rolls back — at a
  click; this gate would run inside it before the VM step.)
- **Evals** (retrieval recall, response quality) join the scheduled/nightly
  lane once features 6–7 provide the data they run on.

## Known risks

Carried over from the retired SDD's risk register — still live, revisit as
features land:

- **LLM question quality** — the product *is* question quality. Gemini Flash
  (thinking disabled) is the bet; if it underwhelms, the swap lever is
  `make_llm()` (Claude Sonnet was the runner-up). Decide after dogfooding,
  not benchmarks.
- **Barge-in depends on browser echo cancellation** — known flaky on mobile
  Safari speakerphone/Bluetooth. Verify on real phones when touching
  turn-taking.
- **iOS screen lock kills web audio** — on-the-go use is screen-on only; a
  native wrapper is the eventual fix, out of scope for now.
- **Deepgram Flux pricing/quotas** at sustained usage — verify before any
  public launch.
- **Eager end-of-turn** (speculative LLM calls) trades cost for latency — a
  lever to pull only if measurements demand it.

## Working notes

[docs/notes/auth.md](docs/notes/auth.md) and
[docs/notes/memory.md](docs/notes/memory.md) are brainstorming/learning
notes, not specs — useful background when speccing features 1 and 4, but
REQUIREMENTS.md is what implementation and review are held to.
