# Aloud — Tiered Memory Layer (Architecture Sketch)

> **Status:** working architecture document. This started as an exploratory sketch
> of a MemGPT-style tiered memory layer for Aloud's cross-session memory (deferred
> in [REQUIREMENTS.md](REQUIREMENTS.md) §6, [SDD.md](SDD.md) §2.6). It is now the
> canonical place where memory-layer architecture gets discussed, decided, and
> recorded — every decision lands here first (§14 decision log), and implementation
> follows the document, not the other way around.
>
> Reference: *MemGPT: Towards LLMs as Operating Systems* (Packer et al., 2023),
> https://arxiv.org/pdf/2310.08560

---

## 1. The problem

Today Aloud starts every session from a blank slate. The in-session `LLMContext`
holds the full transcript while you talk, and it is thrown away when the session
ends. That is fine for one conversation, but Aloud is supposed to be a *thinking
partner* — and a partner that forgets every idea you worked through last week is a
weak one.

We want memory that:

- **Persists across sessions** — it remembers the projects, decisions, and open
  threads you keep coming back to.
- **Stays small in the live window** — a voice turn cannot afford to stuff weeks
  of history into context. The model needs the *right* few facts, not all of them.
- **Updates itself** — the user should not have to curate memory by hand. The
  agent decides what is worth keeping.

MemGPT solves a structurally identical problem (a fixed context window vs. an
unbounded conversation) by treating the LLM like an operating system that pages
memory in and out of its own context. We borrow that model — with one major
adaptation for voice (§5).

---

## 2. Key concepts & definitions

| Term | Meaning in Aloud |
|---|---|
| **Main context** | What is actually in the LLM's context window right now. Fixed budget. Aloud's `LLMContext` is this today. |
| **External context** | Everything stored outside the window — past sessions, distilled facts, documents. Lives in Postgres + a vector index. Not visible to the model until retrieved. |
| **Core memory** | A small, always-present, agent-editable block of durable facts about the user (name, recurring projects, goals, working style). Sits inside main context. The MemGPT "working context." |
| **Recall storage** | Searchable episodic memory: per-session summaries and notable moments. "What did we decide about X last Tuesday?" |
| **Archival storage** | Long-term distilled knowledge — durable facts, idea threads, decisions, and persisted documents (FR-21). Retrieved by semantic search (RAG). |
| **Memory pressure** | The condition where main context grows past its budget and must be compressed/evicted. |
| **Recursive summarization** | Compressing the oldest turns into a summary, which is flushed to recall storage; the raw turns leave the window. |
| **Memory-management loop** | An agentic loop that *writes* memory: extract facts, summarize, evict, decide what to persist. In Aloud this runs **off** the voice path (§5). |
| **Retrieval / injection** | The *read* path: pull the few relevant memories for this topic and inject them into main context, like `build_document_context_block` already injects documents. |

---

## 3. Memory tiers

```
┌─────────────────────────────────────────────────────────────────────┐
│  MAIN CONTEXT  (the live LLMContext — fixed token budget)             │
│                                                                       │
│   ┌───────────────────────────────────────────────────────────┐     │
│   │ System / identity + style prompt   (build_system_prompt)    │     │
│   ├───────────────────────────────────────────────────────────┤     │
│   │ CORE MEMORY  — durable user facts, agent-editable           │     │
│   │   e.g. "Working on Aloud's memory layer; prefers blunt      │     │
│   │         pushback; thinks out loud, hates being interrupted" │     │
│   ├───────────────────────────────────────────────────────────┤     │
│   │ RETRIEVED MEMORY  — transient, injected for this topic      │     │
│   │   (top-k from recall / archival for what's being discussed) │     │
│   ├───────────────────────────────────────────────────────────┤     │
│   │ SESSION HISTORY (FIFO)  — this session's turns              │     │
│   │   oldest turns get summarized + evicted under pressure ──┐  │     │
│   └─────────────────────────────────────────────────────────┼──┘     │
└─────────────────────────────────────────────────────────────┼────────┘
        ▲ read (inject)                            write (flush)│
        │                                                       ▼
┌───────┴───────────────────────────────────────────────────────────────┐
│  EXTERNAL CONTEXT  (Postgres + vector index — outside the window)       │
│                                                                        │
│   RECALL STORAGE        session summaries, episodic memory  (searchable)│
│   ARCHIVAL STORAGE      distilled facts, idea threads, docs  (semantic) │
└────────────────────────────────────────────────────────────────────────┘
```

**Inside the window** (main context) is small and fast to read. **Outside the
window** (external context) is effectively unbounded and only enters the window
when retrieved.

---

## 4. Two paths: read fast, write slow

The single most important design decision is to **separate reading memory from
writing memory**, because they have opposite latency requirements.

### Read path — synchronous, must be fast

When a turn starts (ideally *while the user is still speaking* — the streaming
memory goal in REQUIREMENTS §6), Aloud retrieves the handful of relevant memories
and injects them into main context. This is a **single-hop** RAG lookup, not a
multi-step agentic search — it has to fit inside the ~3s voice budget (NFR-1).

```
user speaks ──► STT ──► [retrieve top-k memory] ──► inject ──► LLM ──► TTS
                              (fast, 1 hop)
```

### Write path — asynchronous, agentic, off the critical path

This is where the MemGPT-style **agentic loop** lives. After a turn or at end of
session, a background "memory agent" decides what to do with what just happened:

```
turn / session ends
        │
        ▼
  ┌──────────────────────────────────────────────┐
  │  MEMORY-MANAGEMENT LOOP  (background, no SLA)  │
  │                                               │
  │  • extract new durable facts → core memory    │
  │  • summarize old turns → recall storage       │
  │  • persist idea threads → archival storage    │
  │  • evict to relieve memory pressure           │
  │                                               │
  │  LLM chains memory tool calls until done      │
  │  (MemGPT "heartbeat" — keep control until the │
  │   work is finished, then yield)               │
  └──────────────────────────────────────────────┘
```

Because this loop runs out-of-band, it can take its time, make several tool calls,
and even use a stronger/slower model than the live conversation does.

---

## 5. Why this differs from vanilla MemGPT

MemGPT runs everything in **one** agentic loop: the same model that answers you
also pauses mid-conversation to search archival memory, edit core memory, and page
things in and out. That is fine for a text chat where a few seconds of latency is
invisible.

Aloud is **voice-first and real-time.** A multi-hop self-directed memory loop on
the critical path would blow the latency budget and create dead air. So we split
MemGPT in two:

- **Live turn:** fast, single-hop retrieval + injection. *No* self-directed loop.
- **Background:** the full MemGPT-style agentic loop, off the clock.

So — to the original question — **yes, the agentic loop is real and necessary, but
it belongs in the background, not in the voice turn.**

---

## 6. Memory-management tools (the agent's verbs)

These mirror MemGPT's function interface and slot into Aloud's existing tool
pattern (`agent/tools.py` → `tool_schemas()` + `llm.register_function`, exactly how
`create_artifact` works today):

| Tool | Purpose |
|---|---|
| `core_memory_append` | Add a durable fact to core memory. |
| `core_memory_replace` | Correct/replace an existing core-memory fact. |
| `archival_insert` | Persist a distilled fact / idea thread to archival storage. |
| `archival_search` | Semantic search over archival storage (RAG). |
| `recall_search` | Search past session summaries / episodic memory. |

The **read path** uses `archival_search` / `recall_search` (often called *for* the
agent by the retrieval step, not *by* it). The **write path** uses the append /
replace / insert verbs inside the background loop.

---

## 7. Where it touches the existing system

- **Tools:** extend `tool_schemas()` and register handlers, mirroring
  `create_artifact`.
- **Injection:** add a memory system-block, mirroring `build_document_context_block`.
- **Storage:** new `session_summaries` + memory tables alongside `sessions_repo.py`;
  a vector index for archival search. Sensitive columns stay separable per NFR-6.
- **Documents (FR-21):** the ephemeral `DocumentStore` (`app/documents.py`) is the
  swap point — uploaded docs become archival storage that persists across sessions.
- **Background agent:** a new component, separate from the `CompanionAgent`
  pipeline, that runs the management loop on turn/session/idle events.

---

## 8. Baseline: the harness as-is (no loop, no window management)

Before designing the memory layer we walked the current harness end to end. The
short version, so this document is self-contained:

- **The "loop" today is a dataflow pipeline, not an agentic loop.** One forward
  pass per user turn through a fixed Pipecat chain
  (`backend/agent/companion.py`):

  ```
  transport.input → stt → user_agg → llm → tts → transport.output → assistant_agg
  ```

- **Prompt assembly happens once, at session start.** `build_system_prompt()`
  (identity + spoken style) plus, if documents are attached, a second system
  message from `build_document_context_block()` (`backend/agent/prompts.py`).
  Nothing mutates the prompt per turn; there is no retrieval, no injection.
- **There is no context-window management.** The `LLMContext` grows unbounded for
  the whole session ("text is cheap; no truncation within a session" — SDD §2.6)
  and is discarded at session end. The only bound anywhere is the static document
  cap (400k chars/session, `backend/app/documents.py`), applied once at start.
- **Transcripts go to Postgres as an ops log only** (FR-20, `db/transcript_log.py`)
  — batched off the audio path, never read back into context.
- **One tool round-trip already exists.** `create_artifact`
  (`backend/agent/tools.py`) proves the in-turn `LLM → tool → LLM` mechanism:
  Pipecat runs the registered handler, feeds the result back, and the LLM speaks
  its confirmation.

So the memory layer introduces exactly the two things the harness lacks: **context-
window management** (summarize / evict / inject) and an **agentic management loop**
(§4) — plus the storage to back them (§11).

---

## 9. Turn phases and speculative retrieval

### What "Flux owns turns" means

Pipecat's user aggregator normally decides "the user's turn is over" from VAD
frames. Deepgram Flux does its own turn detection server-side, so the aggregator
is configured with `ExternalUserTurnStrategies` — it defers to Flux's turn events
instead of running VAD logic. Verified against the Pipecat source, the event
sequence during one utterance is:

| Flux event | What Pipecat does |
|---|---|
| `Update` (incremental transcript while speaking) | fires the `on_update` event handler — **no frame** enters the pipeline |
| `EagerEndOfTurn` (medium confidence the turn is ending; only if `eager_eot_threshold` is set) | emits an `InterimTranscriptionFrame` |
| `TurnResumed` (user kept talking after an eager EOT) | event handler only, no frame |
| `EndOfTurn` (confident) | emits the final `TranscriptionFrame` → user_agg → LLM runs |

**Consequence:** partial transcripts *are* available before end of turn — but not
through `user_agg`. A speculative retriever is a **new component hooked on the STT
service's events**, not the aggregator.

### Speculative retrieval (prefetch during listening)

```
user speaking ──► Flux Update events ──► prefetcher ──► vector search (async)
                                                            │
user stops ──► EndOfTurn ──► final transcript ──► join ◄────┘
                                                  │
                                    inject result (if ready) ──► LLM
```

The prefetcher fires retrieval on partial transcripts while the user is still
talking, hiding retrieval latency entirely behind the user's own speech. Restarted
retrievals on topic shift are cheap; the final query can re-validate against the
full transcript at EOT.

**Timeout fallback (decided, amended):** the original idea — wait up to ~5s after
EOT — doesn't survive contact with the latency budget: NFR-1 allows **3s total**
from end-of-speech to first audio, and the LLM+TTS need most of it. So the grace
window is **~300–800ms after EOT**; if retrieval hasn't returned, generate
**without** the extra context. Never have the agent say "I don't remember that" on
a timeout — a slow retrieval is not an absent memory, and claiming to forget would
be false. If the answer genuinely required the memory, the agent can buy time with
the two-output filler pattern (§10) instead.

**Mid-utterance topic change (decided):** the injected-context slot is a **bounded
FIFO** — when new retrievals would overflow the slot's token budget, the oldest
retrieved block is evicted first. FIFO is the MVP policy; relevance-scored eviction
(evict the *least relevant*, not the oldest) is the natural upgrade if FIFO evicts
something still in play.

---

## 10. Tool taxonomy and full inventory

### The two axes that classify every tool

Turn phase (listening / thinking / talking) turned out to be downstream of two more
fundamental questions:

1. **On-path or off-path** — does the agent's next utterance depend on the tool's
   result? On-path tools sit inside the 3s window; off-path tools are dispatched
   and forgotten.
2. **Worker or subagent** — is the work a deterministic side-effect (plain async
   task), or does it need its own LLM reasoning/generation (a subagent making its
   own API calls — the "agent as tool call" pattern)?

### The inventory

| Tool | On/off path | Worker or subagent | Notes |
|---|---|---|---|
| `recall_search` | **on-path** read | worker (1-hop vector/keyword search) | speculative during listening (§9); timeout fallback |
| `archival_search` | **on-path** read | worker | same |
| `core_memory_append` | off-path write | worker | fire-and-forget durable fact |
| `core_memory_replace` | off-path write | worker | correction of an existing fact |
| `archival_insert` (distill) | off-path write | **subagent** | deciding *what's worth keeping* needs judgment — lives in the background loop |
| `compact_context` | off-path | **subagent** | summarizes all but the last ~N turns (~10 to start); summary replaces them in-window and flushes to recall storage. **Trigger is a deterministic token watermark, not the model's choice** — LLM-powered work, deterministic trigger |
| `create_artifact` v2 | off-path | **subagent** | main agent dispatches a lightweight *intent*; subagent generates the body concurrently while the agent keeps talking. Today's v1 generates the full body on the critical path — the flaw v2 fixes |
| `document_save` | off-path write | worker | persists an uploaded/attached doc (replaces the ephemeral `DocumentStore.add`) |
| `document_search` | **on-path** read | worker | semantic search over persisted docs (chunks), same path as `archival_search` |

### The two-output pattern (filler → real response)

Verified against Pipecat's `llm_service.py`: when a tool handler returns via
`result_callback`, the `run_llm` flag on `FunctionCallResultProperties` controls
whether the LLM is immediately re-invoked with the result appended. That gives
exactly the "two consecutive outputs" the design needs:

```
output #1:  "Hmm, let me think about what we said about that…"  + tool call
                │ (TTS speaks the filler while the tool runs)
tool result arrives, run_llm=True
                ▼
output #2:  the real response, grounded in the retrieved memory
```

This chaining — LLM → tool → LLM → possibly another tool — **is** the in-pipeline
heartbeat, and it already exists in the harness (it's how `create_artifact`'s
spoken confirmation works today). It is distinct from the **background management
loop** (§4), which is a plain LLM-API while-loop *outside* the Pipecat pipeline:
same chaining pattern, but no pipeline, no latency budget, and it can use a
stronger model.

---

## 11. Storage, users, and isolation

The memory layer forces the multi-user question that the single-user MVP deferred:
memories and documents are durable, personal data, so user accounts, auth, and
**hard per-user isolation** (no user can ever read another's memories or
documents) arrive with it.

### Database choice (leaning, not final)

**Single PostgreSQL instance with pgvector.** The reasoning:

- Memory rows are *mostly relational*: `user_id`, session FK, timestamps, type
  (core/recall/archival), provenance. That data wants foreign keys, transactions,
  and indexes — SQL's home turf. NoSQL buys nothing here.
- The vector part (embeddings for semantic search) is a **column, not a
  database**. pgvector keeps embeddings in the same transaction as the row they
  describe — one system, no consistency gap, no second service to deploy.
- A dedicated vector DB (Pinecone, Qdrant, …) earns its keep at a scale (millions
  of vectors, heavy QPS) that a personal thinking partner won't see for a long
  time. The repo pattern (`db/*_repo.py`) keeps the swap localized if that day
  comes.
- Postgres is already in the stack — the ops transcript log lives there.

### Isolation model

- Every row in every memory/document table carries `user_id`. All repo-layer
  queries are scoped by it — **no unscoped query exists** in the repo API (the
  `DocumentStore.get(user_id, ids)` signature already follows this discipline).
- Auth: Supabase Auth or Auth.js per the project docs — still TBD; nothing in the
  schema depends on the choice (it just supplies the `user_id`).
- Postgres **row-level security** as defense-in-depth later: even a buggy query
  can't cross users. Not MVP-blocking.
- Sensitive content columns (memory text, summaries, doc content) stay separable
  from metadata per NFR-6, so encryption at rest drops in post-MVP.

### Memory vs documents: same database, separate tables

```
users ─┬─ sessions ─┬─ transcript_events   (ops log, exists today)
       │            └─ session_summaries   (recall storage)
       ├─ memories                          (core + archival rows; embedding column)
       └─ documents ── document_chunks      (source: uploaded | agent; chunked
                                             + embedded on save)
```

- Conversation memory and documents are **different tables, one database** —
  different lifecycles (memories are agent-written, documents user-uploaded), same
  isolation and retrieval machinery.
- Indexing: vector index (HNSW/IVFFlat) on embedding columns; btree on
  `(user_id, created_at)` and session FKs. Documents are chunked and embedded at
  upload time so `document_search` is a pure read at conversation time.

**Open:** embedding model choice (and where it runs); chunking strategy
(size/overlap/structure-aware); whether a session's *attached* documents stay
fully pinned in context (today's behavior) or degrade to retrieval-only chunks
under memory pressure.

### Artifacts become documents (decided)

Artifacts stop being ephemeral boxes on screen and become **documents in their own
right** — the same first-class thing as an uploaded file:

- Every `create_artifact` output is a **markdown document**: downloadable as a
  `.md` file, persisted in the `documents` table with a `source` column
  (`uploaded` | `agent`) rather than a separate artifacts table. One storage
  model, one retrieval path — agent-created documents get chunked + embedded like
  uploads, so a summary written last week is searchable next week.
- **UI:** two scrollable lists at the bottom of the screen — uploaded documents
  and created documents — each item with a download and a **preview** button.
- **Preview pane:** one document at a time, whichever is selected, opening on the
  side of the screen. Split-screen (multiple previews) is a possible later
  addition, not MVP.

This supersedes today's `artifacts` table + `artifact.created` box rendering
(`backend/agent/tools.py`, ArtifactsPanel). The spoken contract stays the same:
non-committal "writing that up now…" (§12), then the document appears in the
created list when the subagent finishes.

---

## 12. Concurrency and consistency

Decisions from working through the three voice-specific hazards (concurrent
workers meet a live conversation):

- **Barge-in × background workers: deferred.** We don't yet know it's a real
  problem — in most imagined cases the worker can simply finish its job through
  the interruption (a summary of turns 1–30 is still valid if the user barges in
  at turn 31). No cancellation machinery until usage proves it's needed.
- **Artifact confirmation: non-committal speech.** The agent says "I'm writing
  that up now…" and returns to listening; the artifact streams onto the screen
  when the subagent finishes. Speech is **never gated** on subagent completion, so
  the agent can't be caught claiming something is on screen before it is.
- **Write races: deterministic locking, per resource.** An asyncio lock per shared
  resource (a memory row, an artifact, a document) so a read and a write never
  collide on the same memory space. The simplification that makes most locks moot
  in practice: **single-writer discipline** — all memory *mutations* serialize
  through the background loop's queue; ad-hoc writes from elsewhere are not
  allowed. Readers never block; the per-resource locks remain as the safety net.

---

## 13. Anatomy of one LLM call

Every LLM API call the live agent makes carries one context object with four
blocks (the "three-part" mental model, corrected: core memory is its own block,
and tool definitions are not prompt text):

```
┌────────────────────────────────────────────────────────────────┐
│ 1. FIXED PREAMBLE                                                │
│    system prompt: identity, spoken style, tool-usage guidance    │
│    + attached-session documents (today: pinned in full)          │
│    (tool DEFINITIONS ride in the API's structured `tools`        │
│     field, not in the prompt text)                               │
├────────────────────────────────────────────────────────────────┤
│ 2. CORE MEMORY   — agent-editable durable facts (small, always   │
│                    present, written via core_memory_* tools)     │
├────────────────────────────────────────────────────────────────┤
│ 3. INJECTED CONTEXT — bounded FIFO slot of retrieved memories /  │
│                    document chunks for the current topic (§9)    │
├────────────────────────────────────────────────────────────────┤
│ 4. RUNNING CONVERSATION — user/assistant turns + tool-call and   │
│                    tool-result messages (the "subagent action    │
│                    summaries"); compacted at a token watermark:  │
│                    all but the last ~N turns summarized, the     │
│                    summary replacing them in-window and flushing │
│                    to recall storage (§10 compact_context)       │
└────────────────────────────────────────────────────────────────┘
```

Block 1 is static per session; 2 changes rarely (background loop); 3 churns with
the topic (FIFO); 4 grows every turn and shrinks at each compaction. This is §3's
tier diagram seen from the API call's point of view.

---

## 14. Decision log & open questions

The running record. Items move upward as they harden: OPEN → LEANING → DECIDED.

### Decided

| Decision | Rationale |
|---|---|
| Split read path (fast, on-path) from write path (agentic, background) | opposite latency requirements; the voice budget can't host a self-directed loop (§4, §5) |
| The MemGPT-style agentic loop runs in the **background**, not the voice turn | §5; the in-pipeline `run_llm` chaining covers the rare on-path tool round-trip |
| Retrieval timeout fallback: short grace (~300–800ms) after EOT, then answer **without** the context; never claim "I don't remember" on a timeout | 3s NFR-1 budget; slow retrieval ≠ absent memory (§9) |
| Injected-context slot is a bounded FIFO (oldest evicted first) | simple, deterministic MVP policy for mid-utterance topic change (§9) |
| Barge-in × background workers: deferred | may be a non-problem; workers finish through interruptions (§12) |
| Artifact confirmation is non-committal ("writing that up now…"); speech never gated on a subagent | agent can't lie about what's on screen (§12) |
| Write races handled deterministically: per-resource locks + single-writer discipline through the background loop | no LLM-mediated conflict resolution for a systems problem (§12) |
| Compaction is LLM-powered but **deterministically triggered** (token watermark, not model choice) | the model shouldn't gamble the context window (§10) |
| Artifacts become markdown **documents** — downloadable, persisted in `documents` with `source: uploaded \| agent`, retrievable like uploads | one storage/retrieval model; artifacts stop being ephemeral boxes (§11) |
| Document UI: two scrollable lists (uploaded / created) with per-item preview; single preview pane, selected doc only | user-friendly minimum; split-screen preview deferred (§11) |

### Leaning (recommended, awaiting final call)

| Direction | Why / what would change it |
|---|---|
| Postgres + pgvector, single database, memories and documents in separate tables | one system, transactional embeddings, already in stack; revisit at real scale (§11) |
| Two-output filler pattern for slow on-path retrievals | natively supported by `run_llm` chaining; needs prompt-tuning so filler doesn't get annoying (§10) |
| `create_artifact` v2: intent-only dispatch + subagent generation | fixes the v1 flaw of generating the body on the critical path (§10) |
| Compaction keeps the last ~10 turns verbatim | starting point; tune by feel |

### Open

1. **Retrieval trigger cadence** — prefetch on every Flux update, throttled, or
   only on detected topic shifts? (cost vs freshness)
2. **Core memory budget** — size cap, and the demotion path core → archival.
3. **Who flags durability** — live agent in real time, background agent after the
   fact, or both?
4. **Summarization cadence** — watermark-only, or also periodic / at session end?
5. **Conflict / correction semantics** — new fact contradicts old: replace,
   version, or keep both timestamped?
6. **Embedding model + chunking strategy** — which model, where it runs, chunk
   size/overlap (§11).
7. **Auth provider** — Supabase Auth vs Auth.js (§11); schema is agnostic.
8. **Attached-doc pinning** — session documents fully pinned (today) vs demoted to
   retrieval-only chunks under pressure (§11).
9. **Relevance-scored eviction** — upgrade the injected-context FIFO when it
   starts evicting things still in play (§9).
