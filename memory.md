# Aloud — Tiered Memory Layer (Architecture Sketch)

> **Status:** exploratory. This is a thinking document, not a spec. It sketches a
> MemGPT-style tiered memory layer for Aloud's cross-session memory (deferred in
> [REQUIREMENTS.md](REQUIREMENTS.md) §6, [SDD.md](SDD.md) §2.6) so the idea can be
> talked through and refined.
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

## 8. Open questions (to talk through)

1. **Retrieval trigger** — retrieve every turn, or only on topic shifts? Every turn
   is simpler but costs latency + tokens.
2. **Core memory budget** — how big before it stops being "core"? When does a core
   fact get demoted to archival?
3. **Who decides what's durable?** — the live agent flagging things in real time,
   the background agent inferring after the fact, or both?
4. **Summarization cadence** — per turn, per N turns, or only at session end?
5. **Conflict / correction** — when a new fact contradicts an old one, replace,
   version, or keep both with timestamps?
6. **Streaming retrieval** — can we kick off retrieval mid-utterance (partial STT)
   to hide its latency, and is the partial transcript a good enough query?
7. **Vector store choice** — pgvector (one less moving part) vs. a dedicated index.
