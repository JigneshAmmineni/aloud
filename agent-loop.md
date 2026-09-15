# Agent Loop — Personal Notes

Knowledge dump from the design conversations after the observability feature
merged (PR #14/#15). These notes do NOT map to the project — they're
everything worth remembering, whether or not it got used.

---

## 1. Pipecat — what it actually is

**The intuition:** Pipecat is a conveyor belt for typed messages called
**frames**. Everything in a voice session — a 20ms chunk of mic audio, a
transcribed word, an LLM token, "the user started speaking" — is a frame.
The pipeline is a fixed chain of **processors**, each an async worker
connected to its neighbors by queues. Every processor obeys one contract:
frames arrive, it consumes the types it cares about, emits new ones, and
passes everything else through untouched. That's the whole framework.

Its real value: it solves the four genuinely hard problems of streaming
voice **once, at the belt level** — ordering, backpressure, provider
adapters, and interruption — so no individual stage has to think about them.

**Observers** are taps clipped onto the belt (latency, transcript, usage
recorders): they watch every frame pass but never touch one. Capture rides
observers; the hot path never does I/O.

### Our belt (companion.py)

```
transport.input → STT (Flux) → user aggregator → LLM (Gemini) → TTS (Cartesia) → transport.output → assistant aggregator
```

### Full call trace: user says "summarize that artifact"

1. **Browser → VM.** Mic audio leaves as compressed packets over WebRTC
   (UDP, bypasses Caddy). `SmallWebRTCTransport.input()` drops raw audio
   frames onto the belt, ~50/second.
2. **STT stage** (`DeepgramFluxSTTService`) forwards audio over a websocket
   to Deepgram. Flux returns two things → two frame types:
   `TranscriptionFrame`s (the text) and — because Flux does its **own**
   turn detection, no local VAD — an end-of-turn signal that becomes
   `UserStoppedSpeakingFrame`. That frame is load-bearing: it starts the
   NFR-1 latency stopwatch.
3. **User aggregator** answers "when is the user's sentence *done*?" It
   collects transcription fragments (including finals that straggle in
   after the turn signal), folds them into one user message on the
   end-of-turn frame, appends it to the shared `LLMContext`, and pushes a
   run-the-LLM trigger downstream. Configured with
   `ExternalUserTurnStrategies` precisely because Flux owns turn-ending —
   the aggregator just obeys. **This is turn assembly, not context
   bookkeeping** — the distinction mattered in the spec review.
4. **LLM stage** (`GoogleLLMService`) takes context + registered tool
   schemas, opens a *streaming* Gemini call. Forks:
   - **Text path**: tokens emitted downstream as frames *as Gemini
     produces them* — nothing waits for the full response.
   - **Tool path**: the service spots a function call in the stream,
     invokes the registered handler, appends the result to context, and
     **silently calls Gemini again** so the model can speak its
     confirmation. That hidden second call is Pipecat's built-in one-lap
     agent loop — the thing feature 3 takes ownership of.
5. **TTS stage** (`CartesiaTTSService`): sanitizer filters strip markdown
   from the token stream; the service aggregates tokens into complete
   *sentences* (a voice model can't take half a sentence), ships each to
   Cartesia's websocket, emits returned audio frames + a per-sentence text
   frame (which the transcript writer logs as `agent_text`).
6. **`transport.output()`** paces audio out over WebRTC at playback speed
   and emits `BotStartedSpeakingFrame` — stops the latency stopwatch
   (`turn.latency` log line), drives the frontend "speaking" waveform.
   First audio ≈ 1.0–1.3s after end of speech, while Gemini is still
   generating the rest.
7. **Assistant aggregator** — last on the belt, deliberately — records into
   context *what was actually spoken*, not what Gemini generated. Only
   matters in one case:
8. **Barge-in.** User speaks over the agent → `UserStartedSpeaking` →
   Pipecat injects an **interruption frame** that races down the belt
   flushing every queue: Gemini stream dropped, Cartesia stops, queued
   audio discarded — silence within ~100ms. The assistant aggregator then
   finalizes context with only the sentences that made it out, so the
   agent knows what the user actually heard.

**Where the agentic loop cuts:** replace stage 4 only. Same frames in
(assembled user turn), same frames out (streamed text toward TTS). Steps
1–3 and 5–8 untouched — that's the whole argument for keeping Pipecat as
the chassis. The loop is plain Python inside a stage; if Pipecat is ever
retired, the loop walks away and only the audio chassis gets rebuilt. The
opposite of lock-in.

---

## 2. Agent architecture fundamentals

### An "agent loop" is just a while-loop around an LLM call

```
messages = context.build()              # deterministic, microseconds
loop:
    response = llm(messages, tools)     # streams text and/or tool calls
    stream text straight to TTS
    if tool calls:
        results = execute (parallel)
        append calls + results to messages
        continue                        # model sees what happened
    break                               # plain text = turn over
```

~200 lines of Python. The value isn't the code, it's the **control**: how
many laps, what gets appended, whether speech and tools interleave, which
model runs which step, what happens on barge-in.

### Loop / context engine / memory — the coupling is weaker than it looks

Key correction to the MemGPT mental model: **most context management should
NOT be done by the agent through tools.** The MemGPT paper's framing (agent
edits its own context via tool calls) is the part practice walked away
from. In real systems (Letta, Claude Code, ChatGPT memory) the heavy
lifting — token counting, eviction triggers, summarization — is
**deterministic machinery outside the agent**. Code, not cognition. Only a
thin layer surfaces as agent-visible tools ("remember this," "search my
memory"), and those belong to the *memory* feature, not the context engine.

Consequence: the loop needs *a* context, not a *structured* context. The
interface is one function: `build_messages() → list of messages`. So the
sequencing works cleanly:

1. **Loop first**, against that seam, trivial implementation (linear
   history).
2. **Context engine second**: swap in sections/budgets/compression behind
   the same seam; loop control flow untouched.
3. **Memory third**: fills a section the engine reserved + adds the 2–3
   agent-visible tools.

Building context sections before watching the loop run on real transcripts
= designing budgets blind. Loop → observe → then design the window.

### Latency doctrine for voice agents

- **The hot path (end-of-speech → first audio) contains exactly ONE LLM
  call in the common case** — the one that produces speech. Everything
  else is deterministic code or overlaps speech already playing.
- **Deterministic scripts are the norm, not a hack**: context assembly,
  token budgeting, eviction, retrieval lookup (embeddings + vector search
  is tens of ms, no LLM) — all plain code.
- **The small-model-router pattern is an anti-pattern here.** "Fast LLM
  decides tools → smart LLM verifies and speaks" is two *serial* calls
  where one would do — it adds latency. Modern tool-calling models emit
  the tool call AND the speech in the same response, and Gemini Flash
  (~400–500ms TTFB) already *is* the fast model. If tool-decision quality
  disappoints, swap the main model (one factory), don't stack a second.
- **Where cheap models DO belong: background jobs** — summarization,
  memory extraction, long artifact drafting — where latency doesn't matter
  and cost does. The role assignment is the mirror of the naive version:
  the *speaking* call must be fast; the slow careful work happens where
  nobody waits (including during the user's own speaking time, which is
  free compute).
- **Speak-while-working**: the model's first streamed sentence goes to TTS
  immediately ("let me write that up—"), tools run while it talks, results
  feed the next utterance. First-word latency is then independent of tool
  chain length.
- Retrieval later: an embedding lookup, not an LLM call — triggerable
  mid-user-speech on partial transcript, results injected before the
  turn's main call. The embedding API call (~50–100ms) dominates, not the
  vector search.

### A "turn" can contain multiple distinct utterances

The loop's step structure produces chained outputs naturally: step 1's
text ("hmm, let me look that up") streams → tools run → step 2's text
("got it, ...") streams → until a text-only step ends the turn, the step
cap forces a wrap-up, or barge-in cancels the chain. Boundary to remember:
utterances within a turn are separated by **tool work**, not arbitrary
pauses — a step ends in tool calls (another utterance follows) or text
(turn over). Every silence maps to real, instrumented work.

### Proactive vs reactive (FR-7)

FR-7: "By default, the agent is reactive: it responds when the user
speaks." Proactive tool use = the agent invoking tools it wasn't asked
for. The loop makes proactivity *technically* trivial (one prompt
sentence), which is exactly why the spec excludes it explicitly —
capability now, initiative later as its own deliberate spec change
(proactive flagging, ex-FR-8, is parked with a user-facing toggle idea).

---

## 3. State lessons from the spec review (round 1: "precise about control flow, vague about state")

These generalize to any tool-calling agent implementation:

- **Every tool round must append the ASSISTANT side, not just results.**
  The assistant message carrying the step's text and its tool calls enters
  the context *before* the results — one atomic append per step. Orphan
  tool results are literally unsendable: Claude rejects a `tool_result`
  with no preceding `tool_use` of the same id; Gemini defines
  `functionResponse` relative to the `functionCall` turn. Forgetting this
  also silently erases the agent's own words from its history mid-turn.
- **Post-barge-in context must record the SPOKEN PREFIX** — what the user
  actually heard — never the full generated text (agent believes it said
  things nobody heard) and never nothing (agent repeats the half-heard
  answer). The sanitized sentence frames that reached TTS are the source
  of truth. And **every issued tool call gets a terminal result**
  (cancelled ones get `{"status": "cancelled"}`) — a context must never
  hold a half-round.
- **A prompt is guidance, not a guarantee.** "Say something before slow
  tools" fails: Flash routinely emits a tool call as its entire first
  chunk → user sits in silence for a full tool timeout. The fix is a
  **deterministic backstop in the loop**: if nothing has been spoken when
  a step-1 tool round starts, the loop itself speaks a canned filler line.
  General principle: latency guarantees live in code, not prompts.
- **Forcing a text-only step requires omitting tools from the request**
  (or `tool_choice: none`) — a plain "wrap up now" instruction can still
  come back as a tool call, and then your step cap has no teeth. Also:
  the wrap-up instruction is injected into that ONE call and never
  appended to history, or every later turn is built atop "wrap up now."
  And a cap of N steps really means N+1 LLM calls (the forced final one).
- **Instrumentation doesn't survive stage swaps for free.** Latency
  breakdowns built from a service's own metrics vanish when you remove
  that service — and *missing data reads as healthy*. State positively
  what the new component owes each metrics row.
- **Two aggregator jobs, easy to conflate**: context bookkeeping
  (retire it with the LLM stage) vs. **turn assembly** (folding streamed
  transcription + external end-of-turn events + late finals into one user
  message — that stays with the audio machinery). Also: the greeting is a
  turn with NO user message — it needs a named entry point or the session
  opens in silence.
- **Session teardown hits the same window as barge-in**: End-tap,
  disconnect, and SIGTERM drain can all cancel a task mid-write. Waiting
  a bounded grace for in-flight *writes* before stopping the recorders is
  what keeps a completed write's usage event from being enqueued into a
  stopped (= silently dropping) writer.
- **`count_tokens` APIs are network calls.** A per-step token log must be
  a local character heuristic; a provider count on the hot path is
  50–200ms per step spent on a log line.

---

## 4. HNSW / vector search

- **It IS an index** — the leading index *structure* for approximate
  nearest-neighbor (ANN) vector search, the way a B-tree is the standard
  for ordered lookups. Vector similarity has no ordering a B-tree can
  exploit; without an index the only exact answer is comparing every row.
- **Structure**: multi-layer graph over the vectors (Malkov & Yashunin
  2016). Every vector is a node linked to near neighbors; a fraction get
  promoted to sparse upper layers exactly like a skip list. Search enters
  at the sparse top, greedily hops "warmer," drops a layer, refines —
  ~log(N) hops instead of N comparisons. "Small world" = the
  six-degrees property: a few long-range shortcuts make everything
  reachable in few hops.
- **The trade**: approximate (typically 95–99% recall), tunable — `m`
  (connections/node), `ef_construction` (build thoroughness), `ef_search`
  (query thoroughness); each up = better recall, more cost. In exchange:
  ms queries over millions of vectors, incremental inserts, no training
  step. Costs: slower builds, graph wants RAM.
- **vs IVFFlat** (the Postgres alternative): k-means clusters, search
  nearest clusters only — faster build, needs training data, degrades as
  data drifts. HNSW has broadly won (default in pgvector, Qdrant,
  Weaviate, Milvus).
- **pgvector specifics**: `CREATE INDEX ... USING hnsw (embedding
  vector_cosine_ops) WITH (m=16, ef_construction=64)`; query knob
  `SET hnsw.ef_search`. Iterative index scans (pgvector 0.8) continue the
  graph walk until enough *filtered* matches surface.
- **Fit for Aloud**: near-tautological — it's the index type inside the
  already-chosen door (single Postgres + pgvector-later, decision #7). No
  new service; memory rows stay normal Postgres rows, so RLS and user_id
  scoping apply to vector queries like everything else.
- **Scale honesty**: HNSW earns its keep ~100K+ vectors. Thousands of
  per-user memory rows → an exact scan (`ORDER BY embedding <=> $q LIMIT
  10`) is ~1ms with PERFECT recall. Right move: exact scan first, written
  revisit trigger (~50–100K rows) to add the index.
- **The future gotcha — filtered ANN**: our queries are `WHERE user_id =
  X` but the HNSW graph is GLOBAL across users. The walk finds globally
  nearest vectors, THEN the filter discards other users' rows → too few
  results / wasted walk. Iterative scans mitigate; exact scan sidesteps it
  entirely at small scale. Verify this specifically when the index turns
  on.
- RAM: thousands of 768-dim vectors = a few MB — trivial even on a 2GB VM.

---

## 5. Deploys, rollback history, and ops bits

- **A force-moved pointer branch has no recoverable history**: GitHub does
  not expose remote reflogs, and Actions run logs expire (~90 days). The
  durable mechanism is **deploy tags** — an immutable
  `deploy-YYYYMMDD-HHMMSS` tag pushed at the deployed SHA *after* the
  health check, in the same step that moves the pointer. Tags accumulate
  forever in git, visible in any clone (`git tag -l 'deploy-*'`), each
  timestamped in its name → the permanent rollback menu. Actions history
  stays the *rich* record (logs, who clicked) while retention lasts; tags
  are the *index*. A failed deploy leaves neither pointer move nor tag —
  the history only ever contains verified deploys.
- **Rollback here rebuilds from git**, it doesn't swap images: reset the
  checkout to the old SHA, rebuild — fast because of Docker layer/build
  cache (which is why prunes keep one release back + a build-cache
  reserve). True instant-swap rollback = per-release image tags + a
  registry; overkill at this scale.
- **Reviewer authentication has TWO separate credentials**: the Claude
  GitHub App *installation* (posts comments; doesn't expire) and the
  `CLAUDE_CODE_OAUTH_TOKEN` repo secret (authenticates the actual model
  calls against the Claude subscription; minted by `claude setup-token`,
  dies on logout/rotation). Failure signature of a dead token: the action
  boots, then `Claude execution failed: result is_error:true` within
  seconds. GitHub secrets are write-only — you can't inspect one, only
  regenerate: `claude setup-token` → `gh secret set
  CLAUDE_CODE_OAUTH_TOKEN`. (Diagnostic order learned the hard way: rule
  out usage limits by checking the plan, THEN suspect the token —
  identical symptoms.)

---

## 6. Feature-3 spec decisions worth remembering (the why, not the FRs)

- Agentic loop pulled ahead of the artifacts rework so the document
  workspace can be *defined as tools of the agent* instead of bolted onto
  Pipecat's function calling. This also quietly resolved the context
  engine's parked "custom stage vs. own loop" decision.
- v1 tool inventory: `create_artifact` (migrated), `list_artifacts`,
  `read_artifact` — the reads exist so multi-step has something real to
  chain on day one. `list_artifacts` deliberately spans sessions: the
  first cross-session capability, free, a preview of memory's value
  (with an explicit §6 carve-out so it doesn't contradict "memory is
  deferred").
- Tool security invariant (same shape as the auth work): `user_id` comes
  from session state, NEVER from model-supplied arguments — the model
  picks *which* artifact, the handler proves ownership via the user-scoped
  query with RLS as backstop, negative test mandated.
- Barge-in policy: cancel pending/in-flight READS, let in-flight WRITES
  finish (atomic transaction either way; half-done writes are worse than
  moot ones); a post-interruption write completion spawns no new LLM step
  but its result enters context so the next turn knows.
- Two spikes before any loop code: (1) custom processor in the LLM slot
  with TTS streaming + barge-in intact; (2) Gemini streaming + native
  function calling + usage metadata through our own provider-seam client.
