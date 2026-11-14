# Turn-decision classifier (Jev) — research & viability, 2026-11-12

> **Superseded 2026-11-13 by REQUIREMENTS.md §4.15**, which is what
> implementation is held to. Corrections from a fact check that day,
> where this note is wrong or stale:
> - Current model is `jev-1.14.0`, not 1.13.0.
> - Pipecat's Jev classifier arrived in Pipecat 1.12.0 (the extra was
>   renamed `pipecat-ai[typesafe]` in 1.13.0). We are on 1.3.0, so it
>   is not available to us; §4.15 calls the vendor SDK directly.
> - DuplexJev is a community package, not part of Pipecat. Its weights
>   are CC BY-NC 4.0 and it needs a GPU with about 10 GB: ruled out.
> - The "0.83s end to end" figure below could not be sourced. One
>   independent evaluator measured about 0.1s median.
> - The vendor's 67.8% is agreement with larger models, not
>   correctness, and one independent test ranked Jev last of twelve on
>   a legal task. Our own bench decides (FR-71).
> - Data terms: no training on customer data without consent; kept "as
>   long as necessary" with no stated period; zero retention only for
>   enterprise customers on request; DPA automatic; US-hosted.
> - The design below changed in the spec: no canned replies, silence
>   instead of "Hmm.", three questions (done / write / speak), and the
>   create gate stays where it is. "FR-63's ingest classifier" refers
>   to the unmerged PII spec branch's numbering, not to §4.15's FR-63.

The proposal (owner, from live testing): at each end-of-user-turn, a
cheap decision model classifies the turn and drives the agent's response
mode: `incoherent | coherent→silent execution | coherent→respond |
mid-thought (give the turn back)`. Research question: is Jev viable for
this in our agent?

## The trace that motivates it

Turn 8, session of 2026-11-12: user said "…create two lists. **And give
me your ideas for** what could be client side and server side." The
agent silently edited the document and said "Done." — the ideas half was
dropped entirely. Decision quality, not mechanics: the filler fired
correctly (TTFB alone was 1372ms — real dead air), the edit was fine;
the agent chose write-only when the user wanted discussion. This class
of failure (execute vs. discuss) is currently decided implicitly by the
main model and is the biggest UX problem by owner judgment.

## First finding: the taxonomy splits into two different problems

1. **Turn-STATE classes** — `incoherent` (audio/STT garble) and
   `mid-thought` (premature end-of-turn). These are properties of the
   AUDIO/transcription, not of intent. They belong at the turn-detection
   layer, where Pipecat already has a seam (turn analyzers) and where
   Flux's own `eot_threshold` already lives.
2. **Intent classes** — `silent execution` vs `respond/discuss`. This is
   semantic classification over the transcript. It belongs in front of
   the main LLM call, exactly where FR-60's judge already sits for
   creates.

One model doing all four conflates layers; two small deciders (or one
per layer) fit the architecture better.

## Second finding: the 4-way single label would have FAILED today's case

"Create two lists AND give me your ideas" is *both* execute and
respond. A forced single choice between class 2 and class 3 reproduces
exactly the lossy decision that caused the failure. Redesign: two
independent yes/no questions (the Noul shape both Jev and our existing
judge handle well):

- Q1: "does the user expect spoken substance back (ideas, opinion,
  answer)?"
- Q2: "did the user ask for something to be written/changed?"

2x2 grid: (no,no)=ack only · (no,yes)=silent execute, say "Done." ·
(yes,no)=discuss, NO tools · (yes,yes)=do both: execute AND speak the
substance. Today's failure lands in (yes,yes) and gets both halves.

## What the decision must bind to (the stubbornness lesson applies)

Prompt hints alone will not hold. Mechanical bindings per outcome:

- **mid-thought** → suppress the agent turn entirely: no LLM call, the
  aggregation keeps accumulating, the user keeps the floor. (Also the
  cheapest outcome: saves a whole turn of tokens.)
- **incoherent** → canned "say that again?" / "Hmm." WITHOUT an LLM
  call — again saves a turn; today the main model burns a full call to
  output "Hmm."
- **discuss-only (yes,no)** → WRITE tools stripped from the declared
  tool set for that call (the registry already classifies every tool
  `is_write`; the loop passes a filtered schema list) — the agent
  structurally cannot write, but KEEPS its read tools: read_document,
  search, and future discussion aids (web search etc.). Owner decision
  2026-11-12: never `tool_choice="none"` here — discussion should be
  informed by reads.
- **execute-only (no,yes)** → tools allowed + the existing brevity
  rules.
- **(yes,yes)** → tools allowed + one ephemeral system line ("the user
  also asked for your ideas — speak them after the edit"), never
  appended to context (the wrap-up-instruction pattern).
- **Fail direction: OPEN to today's behavior** — classifier error or
  timeout means `tool_choice="auto"`, no hint: a broken classifier can
  only cost us the improvement, never a regression.

## Jev, current facts (2026-11; re-verify before building)

- Decision-only model (TypeSafe): typed outputs (Choice ≤255 / Score /
  calibrated yes-no), closed weights. API `POST
  api.typesafe.ai/v1/systemone`, model `jev-1.13.0`; Python SDK
  `typesafe-sdk`; also via OpenRouter and Cloudflare Workers AI.
- **Latency:** vendor claims 70–500ms end-to-end; one independent test
  measured ~140ms server-side but **0.83s e2e including network** from
  their vantage. That spread decides everything for a voice hot path —
  must be benched from OUR VM (p50/p95), not taken from either claim.
- **Pricing:** ~$0.042 per 1M input tokens, output free (some resellers
  slightly higher). At our turn sizes this is effectively free —
  cheaper than the Gemini call it would sometimes SAVE (the suppressed
  and canned turns).
- **Accuracy:** vendor-reported 67.8% on their own 4-workflow bench — a
  few points behind frontier models; it wins on price/latency, not
  intelligence. Must be benched on OUR corpus (we have months of real
  transcript_events: genuine garbles like "No alcohol.", real
  mid-thought fragments, real mixed turns) before trusting it over the
  Gemini-through-our-seam baseline.
- **Maturity risk:** API is early access; field names may still change.
- **Pipecat already integrates both layers** (big):
  - `pipecat-ai[jev]` install extra — a Jev classifier extension by
    TypeSafe inside our own framework.
  - **DuplexJev** (`DuplexJevTurnAnalyzer`) — a SEPARATE, open-weights
    speech model (Adventists.ai) that answers multiple-choice questions
    about RAW AUDIO in one forward pass: finished | still talking |
    backchanneling | asking-to-wait, with a tunable finished-probability
    threshold (default 0.5). That is almost exactly the turn-STATE half
    of the proposal, at the audio layer where it belongs — and open
    weights means self-hostable: no fourth data processor for that
    half. Open question: compute (our VM is 2 vCPU, no GPU — likely
    needs tuning down, a bigger box, or stays hosted-by-us elsewhere).

## Costs and constraints (ours)

- Hosted Jev = a FOURTH content-processing provider receiving user
  speech → NFR-5 privacy-policy amendment + a `providers.py` factory
  (hard constraint). Same conclusion as the 2026-10-11 evaluation —
  but the amortization argument has now flipped: this is no longer one
  rare gate; it is every turn, plus the FR-60 judge, plus FR-63's
  ingest classifier could all ride the same decider.
- Serial-latency budget: the intent decision must complete BEFORE the
  main call to set `tool_choice` — a real serial add of (bench result)
  ms on every turn. Acceptable if ~≤300ms p95 against our 3s E2E
  budget and ~1.3s typical TTFB; unacceptable at the 0.83s number.
  The turn-state half adds nothing serial if it lives in the turn
  analyzer (it gates whether a turn STARTS at all).
- Provider-neutral by construction: define the decider as a seam
  (factory returning a `classify(questions, evidence) -> verdicts`
  client). Candidates behind it: Jev (fast/cheap, new vendor),
  Gemini flash-lite via the EXISTING client (zero new vendors, ~300to
  700ms, strictly a latency question), DuplexJev (audio half,
  self-hosted). The seam makes the vendor choice reversible — the
  whole point of the house style.

## Verdict

**Viable and worth a spec — as two layers, not one model, with
benching as the go/no-go.**

1. Turn-state layer: try Flux `eot_threshold` tuning first (free),
   then evaluate `DuplexJevTurnAnalyzer` behind Pipecat's existing
   analyzer seam (open weights, no privacy cost; compute TBD).
2. Intent layer: the 2x2 Noul pair in front of the main call, bound
   mechanically to `tool_choice` + turn suppression + canned replies;
   decider behind a new provider-seam factory; Jev vs flash-lite
   decided by a one-day bench from our VM (latency p95 + accuracy on
   a labeled corpus pulled from our own transcript_events).
3. Go/no-go gates (loosened 2026-11-12, owner decision: quality first,
   budget later — keep ~3s E2E as a SOFT target so later optimization
   stays minor): the latency gate is advisory, not disqualifying —
   bench and record p95, prefer the faster option at equal accuracy;
   accuracy on our corpus is the real gate; privacy amendment written
   before any hosted-Jev traffic.

Natural home: roadmap §3.3 (thinking-partner mode) — this IS the
response-mode decider that section needs; spec it as its own § when
picked up (candidate FRs: the taxonomy, the bindings, the fail-open
rule, the bench acceptance bar, the eval corpus).

Sources: sanity.io/glossary/jev-typesafe-ai-model ·
firecrawl.dev/blog/what-is-jev · aimlapi.com/blog/what-is-jev ·
developersdigest.tech (pricing/benchmarks) ·
docs.pipecat.ai/api-reference/server/extensions/pipecat-duplexjev ·
livekit.com/blog/turn-detection-voice-agents (text-based turn-detection
tradeoffs) · deepgram.com/blog/evaluating-end-of-turn-detection-models
(eval methodology).
