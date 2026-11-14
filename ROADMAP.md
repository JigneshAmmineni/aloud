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
  documents block, fallback/greeting lines, wrap-up instruction)
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

### 3.3 Thinking-partner mode — proactive reasoning & ambient capture

**Specced 2026-11-13 as REQUIREMENTS.md §4.15 (FR-62–FR-73), from a
design session with the owner.** Where the spec departs from the notes
below, the spec is the state and these notes are the journey:

- **Jev first, not Haiku.** The checks run on TypeSafe's Jev behind a
  provider factory; Haiku is only a reference column in the bench.
  Reason: the checks sit in front of every turn, and Haiku measured
  0.7–1.9s per call.
- **Wait, then call**, not binding at dispatch: the main model is
  called only after the answers are in, so a turn the agent should
  not take costs no Claude call. Starting Claude at once and applying
  the verdict to its output was considered and rejected (wasted
  tokens, a refusal round, more moving parts). It stays the fallback
  if only a slow model turns out accurate enough.
- **Keep-listening is a text check after Flux's end of turn**, not an
  audio-layer change. DuplexJev is ruled out (non-commercial weights,
  needs a GPU). `FLUX_EOT_THRESHOLD` is untouched.
- **Silence, with no time limit and no "Hmm."** for a mid-thought turn
  and for a turn that asks for nothing. No canned replies. The owner
  wants the session to stay open through long gaps; the user asks
  again when they want an answer. "Hmm." leaves the prompt while both
  checks are on (with either off, today's rule stays, so a trailing
  thought is never answered with advice in between).
- **Changed in the label review, 2026-11-14:** mode 1 is "affirm" (five
  words or fewer: "Done.", "Great.", "Sounds good."), the spoken
  question is a three-way choice (nothing / affirm / answer) so the
  mode has a mechanism behind it, and the PR #25 rule that dropped a
  "Done." is removed in favour of a one-second pause between two
  spoken steps of a turn. Dropping the sentence before a tool call
  was considered and rejected by the owner as a hack that depends on
  what the model says; the narration's verbosity is accepted for now.
- **Two leans:** toward silence when unsure whether the user is done;
  toward allowing the edit when unsure whether one was asked for.
- **The create gate stays on Haiku** (3.3.1 below is the follow-up),
  so there is no consolidated decision bundle yet.
- **Answer modes** are FR-72, as instructions to Claude; the summary
  bound is tightened to one or two sentences, ideally one. Claude, not
  the checks, picks between summary and explanation.
- **Accepted limit, not on the roadmap by the owner's choice:** a
  listening sound ("mm-hmm") while the agent is talking still stops
  it, and the reply is not resumed.
- **Parked:** preparing the context, or looking documents up, while
  the user is still speaking. The checks are plain text-in,
  verdict-out calls so that this stays possible (feature 5, feature
  10).
- **Before the spec's review rounds are spent:** a throwaway bench of
  Jev on about 100 of the owner's recorded turns, as soon as there is
  an API key — the design rests on a response time and an accuracy
  nobody has measured from here.

**Decisions 2026-11-12 (research: docs/notes/turn-decision-jev.md):**
the per-turn INTENT decider (spoken-substance? × write-asked? → ack /
silent-execute / discuss-with-read-tools / both) runs on **Claude Haiku
5.5 through the FR-48 provider seam** — mechanical bindings only (write
tools stripped for discuss, turn suppression, canned replies; reads
NEVER stripped). **Swap-if-slow is pre-decided:** the decider sits
behind a factory, so if its measured stage latency (step.N.* metrics)
becomes a problem, switch the factory to TypeSafe Jev (~150–300ms
faster, needs a privacy-policy amendment as a 4th processor) — one
client + env var, no loop changes. End-of-turn detection is the AUDIO
layer, not Jev: tune Flux eot_threshold first, DuplexJev
(open-weights, self-hosted) as the upgrade. The FR-60 create-gate
judge moves to the same Haiku client immediately; CONSOLIDATION
end-state: the gate's "explicit new-doc ask?" becomes a third question
in the one pre-call decision bundle, and the gate keeps its fail-closed
enforcement point but consults the bundled verdict instead of making
its own mid-turn call.

**Measured since (2026-11-12, first Claude session), reopening the
Haiku-vs-Jev choice:** the Haiku judge took 0.7–1.9s per call (685,
976, 1230, 1884 ms live; ~0.7–0.85s in the smoke test), well over the
assumption behind "Haiku first". A decider that runs BEFORE every main
call at that speed adds about a second to every turn. Two consequences
for the spec: (1) bench Jev and Haiku side by side from our own
environment before choosing (latency p50/p95 and accuracy on a labelled
set pulled from our transcript_events) — Jev first is now a live
option, not just the fallback; (2) consider binding the verdict at tool
DISPATCH instead of at the request: start the main call and the decider
together, and let the decider's verdict refuse write tools when they
arrive (the create gate's existing shape) rather than strip them from
the request. The decider then costs no serial latency on turns that
never call a write tool. Owner's fallback if gating disappoints: no
decider, plain Sonnet deciding as today (it already judged
keep-listening vs. respond well in that session).

**Answer modes — match how hands-on the user wants to be (owner spec,
2026-11-12; implement with the decision gates).** The agent gauges the
level of involvement the user wants and operates there: the less
hands-on they are, the more autonomous and end-to-end it works; the more
hands-on, the more detail it gives. Three modes:

1. **Silent execution.** Do the work; at most "Done."
2. **Summary (the default for a question).** Two sentences, three at
   most, done in a few seconds, feels like conversation. Ideally it ends
   there. A short spoken list of relevant points is allowed ONLY when
   really needed (the user wants to hear five relevant things): up to
   four or five points, each very concise — one clause, at most two.
   The sentence bound does not apply to the list; the per-point bound
   does. May close with an offer to go deeper.
3. **Explain / elaborate / recite.** Only on an explicit ask ("explain",
   "go into detail", "read me that section"). The agent takes its time
   and goes into depth while staying conversational, and may recite the
   requested parts of a document verbatim.

Prompt changes this implies: drop "and completely" from "answer directly
and completely" (Sonnet resolves it against "keep replies brief" in
favour of long answers — the 2026-11-12 supplier answer ran 344 tokens);
replace "keep replies brief" with the mode-2 bound; relax "never read a
document's content aloud" for mode 3; and say how a list is SPOKEN (the
voice has no bullet points: short separate sentences, no markdown).
Enforcement is instructions only — no deterministic check on the output
and no retry, since either adds latency. Which mode a turn gets is
exactly what the intent decider above classifies, so the two ship
together; an `LLM_EFFORT=low` A/B belongs in the same eval.

**Duration class per turn (eval, ships with the answer modes).** Each
mode implies how long a turn should take: silent execution and summary
finish in seconds; explain, recite, or a long write take as long as
their output. The scripted eval set labels each turn with its class and
fails a turn that lands in a slower one. This is the "too slow" check
that replaces a time limit for ordinary turns; the checks for long
tasks are in feature 10.

Two capabilities the 2026-10-11 live sessions showed the agent lacks, both
deferred to their own spec (they change the agent's core behavior, not one
tool):

- **Proactive reasoning partner.** Asked "how are we going to get the list
  of movies — stored or an API?", the agent answered "I don't have
  information on that; the document doesn't cover it." Unhelpful by
  design: today's prompt makes it a transcriber/reader. The user should be
  able to ask "is this viable?", "what do you think of this change?" and
  get a reasoned opinion that draws on the model's own knowledge, clearly
  framed as opinion, which the user can then have written down. Likely a
  system-prompt stance change plus an eval; possibly a mode or per-turn
  classification so brain-dump turns still get silence, questions get
  thinking.
- **Ambient capture while thinking out loud.** During brainstorming the
  user should not have to say "make a note of that" after every idea —
  ideas spoken mid-flow should land in the working document, coherently
  structured, as they're said (the product vision's "gives their words
  structure" line, still unimplemented). Needs care against the FR-60
  boundary (ambient WRITES to the discussed doc are wanted; ambient
  CREATES are not) and against barge-in (writes mid-speech). Candidate
  shape: a capture mode the user toggles by voice ("take notes while I
  talk"), scoped to one target document.

### 3.3.1 New-document check on Jev — the PR after the decision gates

Owner decision 2026-11-13 (decision-gates spec grilling): the FR-60
create gate stays on Haiku, untouched, while the decision gates ship, so
a problem in their testing has one possible cause. The PR after that
considers moving the gate's "did the user explicitly ask for a new
document?" judgement to Jev (Haiku measured 0.7–1.9s per call in our
sessions; Jev about 0.1s by one independent measurement, not yet
measured from our VM). Conditions: Jev has proven itself in production
on the two new checks; the judgement is replayed on the real gate rows
(`llm_traces` where purpose='gate') plus near-miss cases before the
switch; the gate keeps its fail-closed enforcement point. Spec-time: its
own call when a create arrives, as now, or a third question asked
alongside the two pre-call checks (the consolidation end-state in 3.3).

### 3.4 Playback-aware interruption context — shipped as FR-61 (§4.14)

A barge-in during TTS playback of an already-completed turn left the
context believing the whole reply was heard (observed 2026-10-11,
session c47e28af). Shipped same day: the output transport's
audio-synced text-frame release is the played/discarded signal; a
post-turn interruption truncates the last assistant entry to the
uttered prefix with FR-46's cut-off mark.

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
  - **Create gate (shipped as FR-60, §4.13, 2026-10-11):** the
    description-only create boundary failed live ("note that down"
    made a new doc); creation is now gated by a server-side yes/no
    judge over the user's recent turns, failing closed. Judge
    considered-and-rejected extras: having it also pick which existing
    document a change fits — too much for a cheap fast classifier, and
    the main agent already holds the docs in context.
  - **`delete_document` tool (planned):** expose deletion to the agent,
    gated by the SAME FR-60 judge (explicit request required, fail
    closed) — deletion is more destructive than creation, so it does
    not ship before one-step undo exists.
  - **Versioning / undo (decided 2026-10-10):** v1 is **one-step undo** —
    a `previous_content` 🔒 column written on every edit (agent or user),
    so "undo that" restores the pre-edit text; a full
    `document_versions` history table is the later shape if one step
    proves too shallow. Until it lands, "undo" is a compensating edit —
    there is no version control, a stated v1 acceptance of FR-53.
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
    5. Two BROWSER tabs (the app open twice — unpreventable) editing one
       document → same CAS + merge as case 4. Inside one app instance
       this case cannot arise: one editor tab per document — opening a
       document already open focuses its existing tab, never a
       duplicate.
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

- **Prompt caching for the conversation (added 2026-10-12, cost-driven):**
  every LLM call resends the FULL accumulated conversation (system prompt,
  attach block, every turn, every tool round), so per-session token cost
  grows quadratically with turn count — user LLM costs will skyrocket as
  sessions lengthen. Gemini supports context caching (implicit prefix
  caching plus explicit cached-content handles); the conversation's
  strictly-append-only prefix is the ideal cache shape, and the provider
  seam (FR-48's client) is where the cache handle lives. Design with this
  section's budgets in mind: section boundaries that rewrite the prefix
  (re-rendered attach blocks, compression) invalidate the cache, so cache
  granularity and section layout get decided together.
  - **On Claude (noted 2026-11-12, after the LLM swap): more urgent, and
    nothing to build.** Sonnet 5.5 input is $2 per 1M tokens against
    roughly $0.30 on Gemini 2.5 Flash (about 7x; output about 4x), and
    every call still pays full price for the whole context. Caching is a
    server-side API feature, not a framework: the request marks
    `cache_control` breakpoints and the API caches everything before
    them; cached reads cost $0.10 per 1M (a twentieth of the input
    price), cache writes cost slightly more than normal input. The work
    is ours only in two places: (1) `ClaudeLoopClient` sends `system` as
    blocks with breakpoints instead of one joined string, and (2) the
    context is ordered stable-first — system prompt, tool definitions,
    older turns ahead of the breakpoint; the attached-documents block,
    which is re-rendered on every edit, after it (today it sits second
    in the system prompt and would invalidate the cache on every write).
    Also to decide at spec time: usage accounting (cached-read and
    cache-write tokens are reported separately from input tokens and
    priced differently, and FR-34's single input rate assumes one
    price), and the cache lifetime to pay for (5 minutes by default;
    pauses in a voice session can exceed it).

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

### 10. Long tasks with a live conversation — new input while old input is still being processed

Owner direction (2026-11-12). Position in the order is not decided: it
builds on the loop (3) and probably wants the context engine (5) first.

**Vision.** The user hands the agent a long piece of work (research,
restructuring several documents, a long write) and keeps talking. While
the task runs they can ask a question, add information, or change the
task, and the agent answers and carries on, the way a coding agent takes
messages mid-task. Nothing that is working as intended is cut off for
taking long; the less hands-on the user wants to be, the longer the
agent runs on its own.

**Where it stands today.** One turn at a time, sized for a quick voice
exchange. Shipped with the LLM swap as a first step: a Claude response
has no time limit once it has started (only the 10s wait for it to
START remains, which is a liveness check and never fires on a working
call).

**What stands in the way (the two blockers, then the rest):**

- **A new user turn cancels the running one.** Barge-in ends the turn
  task: writes in flight finish, reads are abandoned, no further step
  runs. Taking input during a task needs the opposite: the task keeps
  running while a second, short turn answers, plus a way to hand the
  task what the user just said. The design questions this opens: which
  of the two speaks when both have something to say; whether "stop" and
  "also do X" are told apart by the model or by a decider; how a
  steering message enters the task's context (queued until its next
  step, the way coding agents do it, is the simplest); and what the
  user hears when the task finishes in the middle of their sentence.
- **Turn budgets sized for a quick exchange.** `MAX_STEPS = 5` ends a
  turn after five tool rounds (then one forced wrap-up call), and
  `TOOL_TIMEOUT_S = 10` fails any tool that runs longer. Both are FR-42
  bounds and both are right for a conversational turn. A task needs its
  own budget, not a raised global one: the quick turn keeps its bounds
  and the task gets a step and tool budget of its own, with FR-42
  amended to say so.
- The 4s judge timeout stays. It bounds a yes/no call, not work.
- No tool today takes long. Research needs tools that do (web search,
  reading long sources), which is where the 10s tool bound starts to
  matter.
- Silence during a long write. Claude sends nothing while it writes a
  tool call's input. Eager input streaming (off today) makes it send the
  input as it is written: the client could then report progress, and a
  short between-bytes timeout could come back as a true dead-connection
  check, since a working call would never be silent. The trade: the
  client must validate inputs itself.
- Nothing tells the user that work is in progress. The canned filler
  line ("One moment.") was removed on 2026-11-12: it armed when a tool
  call ARRIVED, which on Claude is after the whole call has been
  written, so it spoke right before the answer instead of during the
  wait, and its text had to be stripped back out of the context, the
  interruption capture and the judge's evidence. A long task needs a
  deliberate replacement, specced with this feature: when it starts
  (the tool call's start event, not its arrival), what it says, how
  often it repeats, and a rule that it never lands just before the
  answer. Quick turns stay silent.

**Thinking.** Not required for a long task: a task is a chain of tool
calls, and the loop runs those with thinking off today. It is likely
worth turning on FOR THE TASK'S CALLS, because planning and re-planning
across many steps is where it helps most, while the spoken turn stays
off (thinking delays the first audible word). So the spec should assume
two call profiles: conversational (thinking off, low latency) and task
(thinking on, no latency budget). Decide by eval at spec time, not by
assumption. The cost of turning it on is already written down in
CURRENT-ARCHITECTURE.md, "What turning thinking on would take"; the
hard part is that history must become append-only, which is also what
prompt caching (feature 5) wants.

**"Too slow" is an eval, not a timeout.** All from `llm_traces` as it
is today (`ttfb_ms`, `duration_ms`, `completion_tokens`):

- **Output rate per call.** Tokens per second, not characters, and per
  LLM call, not per task. `completion_tokens` counts everything the
  model generates in that call: spoken text, tool call inputs, and
  thinking tokens when thinking is on (they are billed as output, so
  they are already in the count). A call is healthy when its time is
  explained by its output: about 90 tokens/s measured on Sonnet. A
  32-second call that wrote a long document passes; a 12-second call
  that wrote one sentence fails. Time a task spends inside tools is
  outside every LLM call and is judged per tool, against that tool's
  own expected duration.
- **Slow start.** `ttfb_ms` well over the ~1.5s norm. With thinking on
  this must be measured to the first event of any kind, since the first
  spoken word arrives late by design.
- **Hang signature.** With no time limit, a call that stops producing
  never "hits" anything: it ends only when the user speaks over it or
  the session ends. So count calls that ended as interrupted, had run
  for several seconds, and had produced no output. It is a lead to
  investigate, not proof: a user who interrupts a long silent write
  looks the same until eager input streaming makes the write visible.
- Per task rather than per call: steps taken, total time, and whether
  the user had to ask "are you still working?" (the conversational
  version of the same signal).

### 11. Automated end-to-end use-case suite — simulated inputs, judged outputs

**Vision (owner, 2026-11-14).** A suite that plays real use cases
through the whole agent without a person talking, and judges what came
out: did it stay quiet when it should, edit the right thing, answer
briefly, read the section when asked. Today that judgement is the owner
running a scripted voice session by hand after every behavior change
(`docs/evals/`); the suite makes the same check repeatable, so a change
to the prompt, the checks, a model or a provider can be compared with
the run before it.

**Rough scope.**
- **Inputs:** the scripted rows of the eval files, starting with the
  standing regression set, plus turns drawn from the labelled local set
  built for the §4.15 bench (which today tests only the three check
  questions, not the agent). Text in first (the turn as the speech
  recogniser would deliver it, through the agent loop with no audio);
  recorded audio through the real pipeline later, which is the
  promotion gate's golden-audio idea in Process infrastructure.
- **What is judged, cheapest first:** facts the run records itself (the
  §4.15 outcome, which tools ran, what changed in the document, whether
  anything was spoken, the duration class); then an LLM judge for what
  only reading can tell (did the reply answer the question, is it a
  summary or an explanation, did it claim an edit it did not make).
- **Output:** a per-row pass/fail table against the row's expected
  outcome and answer mode, kept per run so two runs can be compared.

**Open questions (spec-time).**
- How multi-turn rows are driven: a fixed script, or a simulated user
  (an LLM playing the owner) that reacts to what the agent said.
- How far to trust the judge: it needs its own small labelled set, and
  the owner's manual run stays the reference until it agrees with it.
- Where it runs: on demand, nightly, or inside the promotion gate. It
  spends real provider money on every run, so not per-PR.
- Whether recorded turns may be used beyond the owner's machine (they
  are transcript text; today they never leave it).
- What it deliberately does not cover: how the voice sounds, barge-in
  timing, and anything else only a live session shows.

**Depends on:** §4.15 (the outcomes it asserts on), feature 7 (traces
it reads), and ideally feature 5 (a context it can set up per row).

## Process infrastructure

Per-PR CI (ruff + pytest, frontend build) and the tailored Claude review are
live. Still to add, roughly when its prerequisite feature lands:

- **Promotion gate (main → prod):** before promoting, run the heavy suite
  per-PR CI deliberately skips — golden-audio E2E through the real providers
  (recorded utterances with known content; assert loosely: transcript
  keywords, a response produced, tool fired, latency within budget) — plus a
  manual talk-through. (The deploy itself is done: the "Deploy to
  production" workflow ships any chosen main commit — and rolls back — at a
  click; this gate would run inside it before the VM step.) Last
  promotion: 2026-11-13, `f051bdb`, six PRs at once (#19–#25, the
  first deploy since 2026-09-15) with no gate but the health check and
  a model smoke test from the production container; the manual
  talk-through on the live site is still owed.
- **Evals** (retrieval recall, response quality) join the scheduled/nightly
  lane once features 6–7 provide the data they run on. The agent's
  use-case behavior gets its own suite: feature 11.

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

## Pending verification — the 2026-10-11/12 behavior-fix batch

Shipped on the create-gate branch but only partially verified live
(moving on to bigger features; come back and run the two eval docs:
docs/evals/2026-10-11-create-gate-judge.md and
docs/evals/2026-10-11-voice-behavior-fixes.md). The LLM swap's eval
(docs/evals/2026-11-12-anthropic-llm-swap.md) re-runs both on Claude, so
one live session can close all three, along with
docs/evals/2026-11-12-filler-removal.md (the filler line's removal,
whose script covers the same turns). The list, so nothing is
forgotten:

- **Sorted 2026-11-13 (decision-gates spec).** Of the carried list
  below, these are now specced and leave this list when §4.15 is
  built: (1) reply length (FR-72), (9) the first-sentence cut (FR-61
  amended; its "how much was heard" column is not), (10) the trailing
  thought (FR-62–FR-64), and from (11) the "Done." vocabulary and the
  after-a-tool-call ordering (FR-43 amended, FR-72). The rest wait for
  a cleanup PR of their own, none of them about what the user hears:
  (2) and (6) narration and when it plays, (3) judge latency (see
  3.3.1), (4) the `LLM_EFFORT=low` A/B, (5) the wrap-up instruction's
  position, (7) the invisible server-side fallback, (8) the refusal
  category, and from (11) the `tool_first` advisory flag and the
  admin page's missing vitest coverage.
- **Carried from the first Claude session (2026-11-12) into the
  decision-gates PR:** (1) replies run long — give the prompt a
  concrete bound (two or three sentences, then offer to go deeper)
  instead of "keep replies brief" beside "answer completely";
  (2) Sonnet narrates before tool calls and after refusals, and
  the loop speaks it ("I'll add that to the document", "the gate
  refused again") — decide between a prompt fix and not speaking a
  step's text when that step calls a tool (from PR #24's last
  review: a ban like "speak none of this" is the form that fails
  on this model; say WHEN speech is wanted instead, e.g. "say
  nothing until the write succeeds, then one short sentence"); (3) the judge ran over
  its ~1s bar twice (1.2s, 1.9s); (4) from PR #24's review: run an
  `LLM_EFFORT=low` A/B — the recommended level for chat-shaped
  turns, no code needed, and it may cut first-token time and reply
  length together; (5) from the same review: on Claude the forced
  wrap-up instruction is hoisted to the front of the system prompt
  instead of arriving last — keep it in place as a mid-conversation
  system message when the context is restructured for caching
  (feature 5) — or sooner: the last review says Sonnet 5.5 accepts
  a `system` message inside `messages` when it follows a user
  message and comes last, which the wrap-up call does, while the
  fallback model (Sonnet 5) does not; verify live before relying
  on it; (6) from review round 2, with measurements: a step
  that goes straight to a tool call is SILENT until Claude has
  written the whole call (3.0s for an 800-character document in the
  live session, 34s for an 11,700-character one in a probe). The
  filler line meant to cover it is REMOVED (owner decision,
  2026-11-12: it could only fire when the call arrived, so it
  landed right before the answer). The silence is accepted for
  quick turns; a wait indicator returns, in a deliberate form, with
  long tasks (feature 10). Kept from this item for that work:
  Claude announces a tool call at its START (`content_block_start`,
  ~1.6s in), which would be a new event in the FR-48 contract; and
  (from review round 3) `llm.thinking_discarded` is logged by the
  client with `session_id` only — hand the discarded count to the
  loop as a neutral event so it is logged with `turn_id` and step.
  Measured in the 2026-11-12 filler-removal session: a narration
  sentence before a tool call ("I'll add that to the document.")
  is not heard when it is generated. It sits in sentence
  aggregation until the step ends, so it plays AFTER the wait (the
  two slowest turns, 5.0s and 5.5s to first audio, were narrated
  ones). Flushing the pending sentence when the model's text block
  ends would put it at the start of the wait, which is the wait
  indicator feature 10 asks for, for free;
  (7) from PR #24's last review: a server-side fallback is
  invisible. When Sonnet 5.5 declines and the API re-runs the
  request on Sonnet 5, `message_start` names the model that
  answered, but the client reads only the token count: the trace
  row says `claude-sonnet-5-5`, and the declined attempt's tokens
  (in `usage.iterations`) are not counted. First step: read the
  model at `message_start`, log when it differs, pass it to the
  trace row. FIRST, test the tail risk live: after a fallback in
  the middle of a reply the client drops the `fallback` block and
  the loop replays `tool_use` blocks from before it; if the API
  enforces its echo rules, every later call in that session fails;
  (8) same review: a refusal's category is not logged. The API
  retries only `cyber` and `frontier_llm`; `general_harms` (which
  ordinary work can trip) gets no retry, and all of them are the
  one word `blocked` in `llm_traces`. Log the category from the
  client (never the text);
  (9) from the 2026-11-12 filler-removal eval, run 2: a reply cut
  INSIDE ITS FIRST SENTENCE is recorded as fully heard. FR-61 learns
  what played one released sentence at a time; with none released
  yet it cannot tell that cut from a broken signal and changes
  nothing, so a four-sentence answer the owner heard one second of
  stayed in the context whole, and the agent then insisted it had
  said it. Fix: once the session has seen released sentences (the
  signal works), a cut with none released for this reply keeps only
  the first sentence, marked as cut. Also worth a column or event:
  how much of a reply was heard is in the context and one log line
  only, not in the database;
  (10) same run: it answered a trailing thought ("Probably…") with
  advice. The keep-listening decision this PR's decider exists for;
  (11) from PR #25's last review, all in the "Done." rule and its
  marker (none blocking, none fixed): a "Done." written AFTER a
  tool call in the same step is spoken mid-work and the closing one
  dropped (not reproduced on Sonnet; the Gemini client can
  interleave that way); `step.N.tool_first` is recorded without
  the slow-stage advisory even when held text came first, which
  hides the one wait nothing else in the row measures (use
  `advisory=bool(step_text.strip())`); the confirmation vocabulary
  is only "done" / "all done" with `.` or `!` — a deliberately
  narrow bet from two live runs, so "All set." and "Got it." still
  run into the sentence before them; and `fmtStages` /
  `isToolFirst` on the admin session page have no vitest coverage.
  The answer modes below rework what a tool turn says, so settle
  these there rather than patching the rule again.

- **Questions and instructions never get "Hmm."** — lean talkative over
  unresponsive: a QUESTION always gets a real answer, a misheard one
  gets "say that again?", and never two "Hmm."s in a row. (The
  shipped rule until §4.15's checks are both on; FR-73 then removes "Hmm." from
  the prompt altogether and FR-64's silence takes its place.)
- **Silent tool work** — no narrated reasoning, steps, or workflow
  ("let me read the document", "okay I have the content").
- **Minimal confirmations** — never "it's on your screen", "I have
  written that to the document", "the document has been updated".
  One acknowledgment per piece of work (2026-11-12, enforced in the
  loop): no "Done." directly after a sentence the agent just spoke,
  and none in the middle of chained work; a turn that worked
  silently still ends with one. (Shipped rule until §4.15 is built:
  FR-43 as amended replaces it with a one-second pause and drops
  nothing.)
- **Create-gate redirection (FR-60)** — a blocked create silently picks
  the existing document from recent conversation (usually the one under
  discussion), writes without asking; only with no fitting document
  does it ask "do you want a new document for this?" (verbal yes passes
  the judge on retry); a spoken-answer request (summary/opinion) gets a
  spoken answer, never laundered into an edit.
- **Placement judgment** — "where in the document?" is never asked when
  there's a clear destination.
- **Rename mode** — a title change is ONE edit_document rename call, no
  document read, no narration.
- **Playback-cut context (FR-61)** — barge in on a finished reply, say
  "continue": the agent knows what was actually heard
  (grep logs for context.playback_cut to watch it fire).
