"""The agent loop (§4.10): the pipeline's LLM stage, replaced.

AgentLoopProcessor sits exactly where the provider LLM service sat —
user-turn text in (LLMContextFrame from the user aggregator, the frame the
FR-42 spike named), streamed text frames out to the sanitizer → TTS chain.
Within one turn it can reason, call tools, observe results, and chain
further calls before and while speaking (FR-42), streaming each step's
text as it arrives (FR-43), and surviving barge-in with its in-flight work correctly
disposed (FR-46). It is plain Python behind our seams: context from the
FR-44 provider, tools from the FR-45 registry, LLM through the FR-48
client — if Pipecat is ever retired, this loop walks away intact.

Interruption contract (spike finding): Pipecat delivers barge-in via
_start_interruption(), the framework's out-of-band hook — never as a frame
through process_frame. The hook cancels the turn task, then finalizes from
state the loop already holds (FR-49: the interrupted trace is enqueued in
the handler, never behind the cancelled await).

Instrumentation is the loop's own job (FR-47): per-step agent.step /
tool.invoked logs, step-indexed stage entries and explicit-turn LLM usage
into the recorder, one llm_traces row per call (FR-49).
"""

import asyncio
import itertools
import json
import time
from collections import deque
from contextlib import aclosing

from loguru import logger
from pipecat.frames.frames import (
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_output import BaseOutputTransport

from agent.checks import (
    QUESTION_IDS,
    CheckSettings,
    TurnPlan,
    build_evidence,
    decide,
    questions_for,
)
from agent.context import ContextProvider
from agent.prompts import (
    FALLBACK_GREETING_LINES,
    FALLBACK_LINES,
    WRAP_UP_INSTRUCTION,
)
from agent.providers import (
    CHECK_ANSWERED,
    CHECK_ERROR,
    FINISH_BLOCKED,
    FINISH_INTERRUPTED,
    FINISH_TRUNCATED,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
    LLMUsage,
)
from agent.tools import Tool, ToolContext, registry_schemas

# Hard bounds (FR-42). True LLM-call ceiling is MAX_STEPS + 1: hitting the
# cap triggers one forced final call with tool selection forbidden.
MAX_STEPS = 5
TOOL_TIMEOUT_S = 10.0

# FR-43: when two steps of one turn both speak, the later one waits for
# the earlier speech to finish playing and then for this much silence, so
# a closing "Done." never runs into "I'll add that to the document."
# Nothing the model says is dropped. The loop knows playback only through
# the output transport's bot-started / bot-stopped signals (the text
# frames it releases are per WORD on this pipeline, so there is nothing
# to count per sentence); a stop between two sentences is followed by a
# start, which re-arms the wait.
STEP_SPEECH_GAP_S = 1.0
# The wait must not strand a reply if a signal never comes. Its ceiling
# scales with the speech being waited for — that speech's length at a
# rate well under the voice's real one (about 12 chars/s at
# CARTESIA_SPEED 0.85; revisit this if that speed changes) plus slack —
# so a long recitation keeps its pause and only a missing signal gets
# here.
STEP_SPEECH_SLOWEST_CPS = 6.0
STEP_SPEECH_HOLD_SLACK_S = 2.0
# ...and never beyond this, however long the earlier speech was: when the
# stop signal is the thing that is missing, this is silence the user sits
# through mid-reply, and the model's stream is parked for all of it. The
# price is that speech longer than this, followed by another speaking
# step, loses its pause.
STEP_SPEECH_HOLD_MAX_S = 30.0
# An earlier step's text that never becomes audio (the filters reduced it
# to nothing, synthesis failed) has no playback to wait for: if no sign of
# it arrives within this long, the wait is over. Its own event, not the
# missing-signal warning.
STEP_SPEECH_START_MAX_S = 2.0

class _TurnState:
    """Live state for one turn — what the interruption handler finalizes
    from. Everything here is owned by the turn task while it runs and read
    only after that task is cancelled."""

    def __init__(self, turn_id: int | None, purpose: str):
        self.turn_id = turn_id
        self.purpose = purpose  # turn | greeting; wrap_up per forced call
        self.step = 1
        self.messages: list[dict] = []  # built list for the current call
        self.step_text = ""  # model text streamed this step
        self.calls: list[LLMToolCall] = []  # current step's tool calls
        self.results: list[dict | None] = []
        self.write_tasks: dict[str, asyncio.Task] = {}  # call.id -> task
        self.read_tasks: dict[str, asyncio.Task] = {}
        self.t0: float | None = None  # current call's start
        self.ttfb_ms: int | None = None
        self.usage = None  # LLMUsage when the provider reported it
        # True once _instrument_call closed the current step's LLM call —
        # an interruption after that (mid-tool-round) must not re-record
        # usage or write a second trace row (review: FR-47 iv held on
        # interrupted turns too).
        self.call_closed = False
        self.spoken: list[str] = []  # AggregatedTextFrame texts THIS STEP
        # (reset each step: earlier steps' sentences are already appended
        # to the context — carrying them would duplicate them into the
        # interrupted step's entry)
        # Text earlier steps already appended this turn: sentence
        # aggregation LAGS the loop (a fast tool round starts step N+1
        # before step N's sentence is captured), so the reset alone can't
        # keep a late-arriving step-N sentence out of step N+1's prefix —
        # _spoken_prefix filters against this too.
        self.appended_texts: list[str] = []
        # Sentences actually captured for earlier steps (exact strings) —
        # the precise half of the filter above.
        self.prior_sentences: set[str] = set()
        # §4.15: what the turn checks decided (plain until they answer)
        self.plan = TurnPlan()
        # FR-43's pause: how much the most recent step that spoke said,
        # and when it began pushing text
        self.earlier_speech_chars = 0
        self.earlier_speech_at: float | None = None


class AgentLoopProcessor(FrameProcessor):
    """One per session, in the pipeline's LLM slot."""

    def __init__(
        self,
        *,
        context: ContextProvider,
        llm,
        registry: list[Tool],
        session_id: str,
        user_id: str,
        model_name: str,
        recorder,
        traces,
        emit,
        write_registry: set,
        greeting_trigger: str,  # required: dropping it must not pass a green suite
        # required too (§4.15): a dropped kwarg would run every turn
        # unchecked under a green suite. `checker` may be None (no key);
        # the switches live in `checks`.
        checker,
        checks: CheckSettings,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
        self._llm = llm
        self._tools = {t.name: t for t in registry}
        self._schemas = registry_schemas(registry)
        self._checker = checker
        self._checks = checks
        self._session_id = session_id
        self._user_id = user_id
        self._model_name = model_name
        self._recorder = recorder
        self._traces = traces
        self._emit = emit
        # FR-46: session-level in-flight-write set, owned by companion so
        # teardown and the SIGTERM drain can await it; the loop only adds.
        self._write_registry = write_registry
        self._greeting_trigger = greeting_trigger
        self._log = logger.bind(session_id=session_id, component="agent.loop")

        self._turn_task: asyncio.Task | None = None
        self._state: _TurnState | None = None
        # FR-44 straggler discriminator: a context frame arriving before
        # Flux's next user-speech-start is the consumed turn's leftover.
        self._await_user_speech = False
        # The greeting is signalled by position, not inferred repeatedly: a
        # LATER no-text frame (whitespace-only aggregation, a stray
        # LLMRunFrame) must never re-greet mid-session with tool_choice
        # none — it is dropped and logged (review finding).
        self._greeted = False
        # FR-46 late-write bookkeeping (all event-loop-serialized):
        self._placeholder_calls: set[str] = set()  # appended as placeholder
        self._late_results: dict[str, dict] = {}  # completed, not yet landed
        # Abandoned reads still running (session-level): cleanup() cancels
        # them so a wedged read never becomes destroyed-task noise.
        self._live_reads: set[asyncio.Task] = set()
        # FR-61: sentences that actually PLAYED (TTSTextFrames released by
        # the output transport after their preceding audio was sent).
        # Reset at turn START, never at turn end - the buffer must span
        # the previous turn's playback tail, where the cut can land.
        self._uttered: list[str] = []
        # round 3: sentences released AFTER the last context append are
        # the ones that vouch for the last entry — without the mark, an
        # earlier step's audio satisfied the mismatch valve and the
        # unheard closing reply was counted as heard
        self._uttered_mark = 0
        # FR-61's safety valve (round-2 review): if audio demonstrably
        # played this turn but no uttered sentence ever arrived, the
        # transport signal is broken (or the cut landed inside the first
        # sentence — heard, by §4.14's over-approximation): never amend.
        self._bot_spoke_this_turn = False
        self._fr61_signal_warned = False
        # FR-61: has the transport EVER released played text this session?
        # If so the signal works, and a reply cut before any of it was
        # released was cut inside its first sentence.
        self._release_seen = False
        # FR-43's pause: the transport's speaking state, as observed
        self._bot_speaking = False
        self._bot_started_at: float | None = None
        self._bot_stopped_at: float | None = None
        self._speech_changed = asyncio.Event()
        self._fallback_cycle = itertools.cycle(FALLBACK_LINES)
        self._greeting_fallback_cycle = itertools.cycle(FALLBACK_GREETING_LINES)

    # ---- frame handling --------------------------------------------------

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            await self._on_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _on_context_frame(self, frame) -> None:
        user_text = self._consume_scratch(frame)
        if user_text is not None and self._await_user_speech:
            # FR-44: a final that straggled in after the turn was consumed
            # and before new user speech — the consumed turn's leftover,
            # never a second fragment-only turn. Dropped and logged.
            # WARNING, not INFO (round-5 review): this branch deliberately
            # discards user speech — if the speech-start discriminator ever
            # stopped firing, every turn after the greeting would land here
            # and the agent would go mute; that must be loud in prod logs.
            self._log.bind(
                event="turn.straggler_dropped", chars=len(user_text)
            ).warning("late final after consumed turn dropped")
            return
        if user_text is None:
            if self._greeted:
                self._log.bind(event="turn.empty_dropped").info(
                    "no-text context frame after the greeting dropped"
                )
                return
            self._greeted = True
        self._await_user_speech = True
        if self._turn_task is not None:
            # A new turn landing while the previous one still runs (a
            # silent tool round draws no interruption broadcast): cancel
            # AND dispose exactly like a barge-in — running writes need
            # their placeholder, the trace its interrupted row, the
            # metrics buffer its turn end (review finding). Then broadcast
            # the interruption barge-in would have: this branch exists
            # BECAUSE none fired, and the abandoned step's queued speech
            # must not play over the new turn's answer (round 3).
            task = self._turn_task
            self._turn_task = None
            await self.cancel_task(task)
            self._finalize_interrupted_turn()
            await self.broadcast_interruption()
        self._turn_task = self.create_task(self._run_turn(user_text))

    def _consume_scratch(self, frame) -> str | None:
        """Read the assembled user message off the aggregator's scratch
        context and RESET it (FR-44: the scratch is turn assembly only —
        without the reset the session silently carries a second unbounded
        copy of the conversation). None = no user message (the greeting)."""
        user_text = None
        for msg in reversed(frame.context.get_messages()):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if content and str(content).strip():
                    user_text = str(content).strip()
                break
        frame.context.set_messages([])
        return user_text

    # ---- observer callbacks (AgentLoopObserver) --------------------------

    def note_user_speech_start(self) -> None:
        self._await_user_speech = False

    def note_bot_spoke(self) -> None:
        self._bot_spoke_this_turn = True  # FR-61's valve: audio began
        self._bot_speaking = True
        self._bot_started_at = time.monotonic()
        self._speech_changed.set()

    def note_bot_stopped(self) -> None:
        self._bot_speaking = False
        self._bot_stopped_at = time.monotonic()
        self._speech_changed.set()

    def note_spoken_sentence(self, text: str) -> None:
        """FR-46: the sentence-level synthesis stream (the same one FR-20
        logs) — the spoken-prefix over-approximation source."""
        if self._state is not None:
            self._state.spoken.append(text)

    def note_uttered_sentence(self, text: str) -> None:
        """FR-61: a sentence whose audio actually reached the speaker —
        the transport releases its text frame only after the preceding
        audio was sent, and an interruption discards unplayed ones."""
        self._release_seen = True
        self._uttered.append(text)

    # ---- the loop --------------------------------------------------------

    async def _run_turn(self, user_text: str | None):
        self._uttered = []  # FR-61: the previous turn's tail is settled
        self._uttered_mark = 0
        self._bot_spoke_this_turn = False
        purpose = "greeting" if user_text is None else "turn"
        state = self._state = _TurnState(self._recorder.current_turn, purpose)
        log = self._log.bind(turn_id=state.turn_id)
        try:
            if user_text is not None:
                # appended BEFORE the checks, whatever they decide (FR-62):
                # a turn the agent stays silent on is still in the context
                self._context.append_user(user_text)
                state.plan = await self._check_turn(state)
            if state.plan.silent is not None:
                # FR-64: a silent outcome runs no step at all
                await self._stay_silent(state)
                ended = True
            else:
                ended = False
                for step in range(1, MAX_STEPS + 1):
                    state.step = step
                    # Greeting: tool selection forbidden, tools still
                    # declared (FR-42/FR-7 — a session must not open with
                    # list_documents volunteering last week's titles
                    # unprompted).
                    tool_choice = "none" if purpose == "greeting" else "auto"
                    ended = await self._run_step(state, tool_choice, purpose)
                    if ended:
                        break
            if not ended:
                # Cap hit: ONE forced final call, tools declared but
                # selection forbidden, wrap-up instruction injected into
                # this call only (never appended to the context).
                state.step += 1
                await self._run_step(state, "none", "wrap_up", wrap_up=True)
        except asyncio.CancelledError:
            raise  # FR-46: _start_interruption owns the aftermath
        except Exception as e:
            # exception TYPE only (NFR-9): a propagated handler/DB error
            # can embed SQL parameters, i.e. artifact content
            log.bind(event="agent.turn_failed", step=state.step).error(
                f"loop error, degrading to spoken fallback: {type(e).__name__}"
            )
            if state.calls:
                # Round 4: a raising tool must not discard the round —
                # gather doesn't cancel siblings, so a concurrent write's
                # result may already be committed AND announced; a context
                # that records nothing makes the next turn re-create it.
                self._dispose_tool_round(state, state.step_text, interrupted=False)
                # the step's narration is playing or about to: the fallback
                # line must wait for it (FR-43), and it would not know to
                # once the text is cleared below
                self._note_step_spoke(state)
                state.step_text = ""  # recorded; the fallback won't re-append
            await self._speak_fallback(state, reason="error")
        # Any non-cancelled exit is a turn end (FR-47: flush-or-clear).
        self._recorder.turn_ended(state.turn_id)
        self._state = None
        self._turn_task = None

    async def _run_step(
        self, state: _TurnState, tool_choice: str, purpose: str, wrap_up: bool = False
    ) -> bool:
        """One step: build → one streaming LLM call → forward deltas →
        maybe a tool round. Returns True when the turn is over."""
        # FR-61 entry scope, stamped at STEP start (round 4): playback of
        # this step's own reply begins while the stream is still open, so
        # an append-time mark raced it — the entry's first sentences fell
        # below the mark and the mismatch valve ate the amendment. At
        # step start nothing of this step has been pushed yet, and an
        # earlier step's late sentences merely prefix `heard`, which the
        # endswith match ignores.
        self._uttered_mark = len(self._uttered)
        messages = self._context.build(turn_id=state.turn_id, step=state.step)
        # §4.15: the turn's one instruction rides every call of the turn
        # and is never appended to the context (FR-66)
        if state.plan.instruction:
            messages = messages + [
                {"role": "system", "content": state.plan.instruction}
            ]
        if wrap_up:
            messages = messages + [{"role": "system", "content": WRAP_UP_INSTRUCTION}]
        elif purpose == "greeting":
            # Ephemeral trigger, this call only (never appended): the built
            # context is system-prompt-only here, and Gemini rejects a call
            # with an empty contents list.
            messages = messages + [
                {"role": "user", "content": self._greeting_trigger}
            ]
        # FR-43's pause looks back at what EARLIER steps of this turn said:
        # read before the reset below
        self._note_step_spoke(state)
        held_for_gap = False
        state.purpose = purpose
        state.messages = messages
        state.step_text = ""
        state.calls = []
        state.results = []
        state.usage = None
        state.ttfb_ms = None
        state.call_closed = False
        # Per-STEP spoken capture: the previous step's sentences are already
        # in the context via its own append — carrying them across steps
        # would duplicate them into an interrupted step's entry. Remember
        # them exactly, for _spoken_prefix's precise filter.
        state.prior_sentences.update(s.strip() for s in state.spoken if s.strip())
        state.spoken = []
        state.t0 = time.monotonic()
        finish = None
        log = self._log.bind(turn_id=state.turn_id, step=state.step)
        # Dev-only live view (FR-49): full step input at DEBUG, never
        # shipped (FR-39); lazy so the serialization is free otherwise.
        log.bind(event="agent.step_input").opt(lazy=True).debug(
            "input: {msgs}",
            msgs=lambda: json.dumps(messages, ensure_ascii=False, default=str),
        )

        await self.push_frame(LLMFullResponseStartFrame())
        try:
            # aclosing: barge-in cancellation closes the provider stream —
            # and its HTTP connection — deterministically, not at GC time.
            async with aclosing(
                self._llm.stream(messages, tools=self._schemas, tool_choice=tool_choice)
            ) as stream:
                async for event in stream:
                    elapsed_ms = round((time.monotonic() - state.t0) * 1000)
                    if state.ttfb_ms is None and not isinstance(
                        event, (LLMDone, LLMUsage)
                    ):
                        state.ttfb_ms = elapsed_ms
                    if isinstance(event, LLMTextDelta):
                        state.step_text += event.text
                        if not held_for_gap and state.step_text.strip():
                            held_for_gap = True
                            if state.earlier_speech_chars:
                                await self._hold_for_speech_gap(state)
                            state.earlier_speech_at = time.monotonic()
                            # FR-61 entry scope, re-stamped as this step
                            # begins to SPEAK: the transport releases
                            # played text word by word, so an earlier
                            # step's words land after this step STARTED
                            # (during its LLM call and the pause above).
                            # Left at the step-start mark they would be
                            # counted as this entry's own, and a reply
                            # the user never heard would stay whole
                            # behind the mismatch valve.
                            self._uttered_mark = len(self._uttered)
                        await self.push_frame(LLMTextFrame(event.text))
                    elif isinstance(event, LLMToolCall):
                        if (
                            not state.calls
                            and tool_choice != "none"
                            and not state.step_text.strip()
                        ):
                            # NFR-1's known exception, made findable: this
                            # step spoke nothing before its first tool
                            # call. Not for a call that will be refused
                            # (selection forbidden): that turn speaks at
                            # once. The value is how long the silence had
                            # lasted when the call arrived. No advisory:
                            # the same wait is already this step's TTFB.
                            self._recorder.record_step_stage(
                                state.turn_id,
                                f"step.{state.step}.tool_first",
                                elapsed_ms,
                                advisory=False,
                            )
                        state.calls.append(event)
                    elif isinstance(event, LLMUsage):
                        # reported ahead of the output on providers that
                        # can: a barge-in or a dropped stream after this
                        # point still records the input it was billed for
                        state.usage = event
                    elif isinstance(event, LLMDone):
                        finish = event.finish_reason
                        # None from a stream that never reported: keep
                        # whatever arrived early
                        state.usage = event.usage or state.usage
        except Exception:
            # FR-49: EVERY call writes a row — a provider drop after 40
            # deltas is precisely the call a trace is for (review round 3).
            # No finish reason ever arrived, so "error" is the loop-level
            # spelling for a call that raised (FR-48's vocabulary describes
            # streams that returned). Usage is whatever arrived before
            # the drop: Claude's input count (reported up front), or
            # nothing on Gemini, which reports only on its final chunk.
            self._instrument_call(
                state,
                "error",
                round((time.monotonic() - state.t0) * 1000),
                purpose,
            )
            # close the frame pair before degrading — a dangling
            # LLMFullResponseStartFrame stalls downstream aggregation.
            # (CancelledError skips this: every cancel path broadcasts an
            # interruption — barge-in's own, or the one _on_context_frame
            # fires for silent cancels — which resets downstream itself.)
            await self.push_frame(LLMFullResponseEndFrame())
            raise
        await self.push_frame(LLMFullResponseEndFrame())

        duration_ms = round((time.monotonic() - state.t0) * 1000)
        self._instrument_call(state, finish, duration_ms, purpose)
        log.bind(
            event="agent.step",
            ttfb_ms=state.ttfb_ms,
            duration_ms=duration_ms,
            tool_calls=len(state.calls),
            finish_reason=finish,
        ).info(f"step {state.step}: {len(state.calls)} tool call(s), finish={finish}")
        log.bind(event="agent.step_output").opt(lazy=True).debug(
            "output: {out}",
            out=lambda: f"{state.step_text!r} calls="
            f"{[(c.name, c.arguments) for c in state.calls]}",
        )

        if not state.step_text.strip() and not state.calls:
            # FR-42: an empty step is a failure, not an ending — blocked or
            # truncated generations return, they don't raise. .strip():
            # a whitespace-only generation is the same silent turn, and an
            # effectively-empty assistant message 400s the Claude swap
            # (round-5 review).
            log.bind(event="agent.empty_step", finish_reason=finish).warning(
                "model returned neither text nor tool calls"
            )
            await self._speak_fallback(state, reason=finish or "empty")
            return True

        if state.calls and finish in (FINISH_TRUNCATED, FINISH_BLOCKED):
            # A cut or declined generation is not a complete one: its
            # tool input may be half-written (a length cut mid-call) or
            # belong to output the provider says to discard (a refusal
            # after partial output). Run nothing — a truncated document
            # must never be written — and say so via the fallback.
            log.bind(
                event="agent.incomplete_step_tools_skipped",
                calls=len(state.calls),
                finish_reason=finish,
            ).warning("tool calls from an incomplete generation were not run")
            # never issued: a barge-in during the fallback line must
            # not dispose them as a tool round that was cut
            state.calls = []
            await self._speak_fallback(state, reason=f"{finish}_tool_calls")
            return True

        if state.calls and tool_choice == "none":
            # tool_choice "none" is a REQUEST — FR-48 names a silent
            # mistranslation here as what would unguard the loop. BOTH
            # calls that forbid selection refuse to execute: the wrap-up
            # (never a round past the cap) AND the greeting (round-5
            # review: FR-7's guard — a session must not open by running
            # list_documents and speaking last week's titles). Speak
            # whatever text came, or the fallback — never silence.
            log.bind(
                event="agent.forbidden_tools_refused",
                calls=len(state.calls),
                purpose=purpose,
            ).warning(
                "call returned tool calls despite tool_choice none"
            )
            state.calls = []  # never issued — as in the branch above
            # .strip(), matching the empty-step check above (review): a
            # whitespace-only generation alongside the forbidden call must
            # take the fallback, not open mute with an empty append
            if state.step_text.strip():
                self._context.append_assistant(state.step_text.strip())
            else:
                await self._speak_fallback(state, reason="forbidden_tool_calls")
            return True

        if state.calls:
            await self._run_tools(state)
            self._context.append_step(state.step_text, state.calls, state.results)
            state.appended_texts.append(state.step_text)
            self._land_late_results()
            return False  # next step sees the results

        self._context.append_assistant(state.step_text)
        return True

    # ---- §4.15: the turn checks ------------------------------------------

    async def _check_turn(self, state: _TurnState) -> TurnPlan:
        """FR-62: ask the decision model about the turn BEFORE the main
        model is called, in one request, and turn the answers into the
        turn's plan (FR-63). A request that fails or outlasts the ceiling
        never holds the turn: whatever is unanswered is skipped and the
        turn runs as it always did (FR-67). Every checked turn leaves a
        log line, a usage event and a trace row (FR-70) on EVERY exit —
        the barge-in cancellation included, from the finally."""
        cfg = self._checks
        questions = questions_for(cfg)
        if self._checker is None or not questions:
            return TurnPlan()
        log = self._log.bind(turn_id=state.turn_id)
        evidence = build_evidence(self._context)
        answers: dict = {}
        usage = None
        reason = None  # why nothing came back: timeout | the error's type
        status = "cancelled"  # overwritten unless a barge-in unwinds
        plan: TurnPlan | None = None
        t0 = time.monotonic()
        try:
            answers, usage = await asyncio.wait_for(
                self._checker.ask(evidence, questions), cfg.timeout_s
            )
            status = CHECK_ANSWERED
            # inside the try on purpose: anything this raises is one more
            # way of having no verdict, and must end as a plain turn with
            # its records (FR-67) — not as an exception out of the finally
            plan = decide(answers, cfg)
        except asyncio.TimeoutError:
            status = reason = "timeout"
        except asyncio.CancelledError:
            raise  # the finally still writes the interrupted records
        except Exception as e:
            status, reason = "error", type(e).__name__
        finally:
            ms = round((time.monotonic() - t0) * 1000)
            interrupted = status == "cancelled"
            if plan is None:
                # no verdict: today's behaviour, marked as a bypass
                plan = TurnPlan(bypass=not interrupted)
            # a request that came back without one of its answers: that
            # question is an error, not "answered" (FR-67)
            missing = CHECK_ERROR if status == CHECK_ANSWERED else status
            asked = {q.id for q in questions}
            per_question = {
                qid: {
                    "status": (
                        "off"
                        if qid not in asked
                        else answers[qid].status if qid in answers else missing
                    ),
                    "p_yes": answers[qid].p_yes if qid in answers else None,
                    "probabilities": (
                        answers[qid].probabilities if qid in answers else None
                    ),
                }
                for qid in QUESTION_IDS
            }
            detail = "interrupted" if interrupted else plan.detail
            # C-1's advisory rides the stage record, whatever the ceiling
            self._recorder.record_step_stage(state.turn_id, "check", ms)
            self._recorder.record_check(
                len(questions), detail, turn_id=state.turn_id
            )
            self._traces.record_trace(
                turn_id=state.turn_id,
                step=0,
                model=cfg.model,
                purpose="check",
                finish_reason=FINISH_INTERRUPTED if interrupted else "stop",
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                ttfb_ms=None,
                duration_ms=ms,
                input_messages=[{"role": "user", "content": evidence}],
                output=json.dumps({"questions": per_question, "outcome": detail}),
            )
            log.bind(
                event="turn.check",
                duration_ms=ms,
                outcome=detail,
                questions=per_question,
                bypass_reason=reason,
            ).info(f"turn check: {detail} in {ms}ms")
            if plan.bypass and not interrupted:
                unanswered = [
                    qid
                    for qid, a in per_question.items()
                    if a["status"] != CHECK_ANSWERED
                ]
                log.bind(
                    event="turn.check_bypassed",
                    questions=unanswered,
                    reason=reason or "unreadable_answer",
                ).warning(
                    f"turn check unanswered ({reason or 'unreadable answer'}): "
                    f"{', '.join(unanswered)} — running the turn without it"
                )
        return plan

    async def _stay_silent(self, state: _TurnState) -> None:
        """FR-64: the main model is not called and nothing is spoken — no
        step, no assistant entry, no fallback line. The user's text is
        already in the context; the next turn's text lands after it as a
        further user message and the model reads them as one. The client
        is told, so its indicator leaves Thinking (FR-6)."""
        self._log.bind(
            event="turn.silent", turn_id=state.turn_id, outcome=state.plan.silent
        ).info(f"staying silent: {state.plan.silent}")
        await self._emit({"type": "turn.silent", "reason": state.plan.silent})

    @staticmethod
    def _note_step_spoke(state: _TurnState) -> None:
        """FR-43: remember how much the step that just ended (or failed)
        said. The pause waits for THAT speech — the most recent step that
        spoke — so its ceiling is sized from it, not from everything the
        turn has said. A silent step leaves the earlier figure in place:
        that speech is still the last thing to have played."""
        if state.step_text.strip():
            state.earlier_speech_chars = len(state.step_text)

    async def _hold_for_speech_gap(self, state: _TurnState) -> None:
        """FR-43: an earlier step of this turn spoke, and this one is
        about to. Wait until that speech has started playing, then for
        STEP_SPEECH_GAP_S of silence after it — re-armed whenever speech
        resumes, so a stop between two sentences does not end the wait.
        Bounded by a ceiling that scales with the speech waited for."""
        ceiling = min(
            state.earlier_speech_chars / STEP_SPEECH_SLOWEST_CPS
            + STEP_SPEECH_HOLD_SLACK_S,
            STEP_SPEECH_HOLD_MAX_S,
        )
        pushed_at = state.earlier_speech_at or 0.0
        start_by = time.monotonic() + STEP_SPEECH_START_MAX_S

        def earlier_speech_seen() -> bool:
            # it is playing now, or a start or a stop came after it was
            # pushed (a stop alone: it continued a run already playing)
            return self._bot_speaking or any(
                at is not None and at >= pushed_at
                for at in (self._bot_started_at, self._bot_stopped_at)
            )

        async def wait() -> bool:
            """False: the earlier speech never started playing."""
            while True:
                self._speech_changed.clear()
                if not earlier_speech_seen():
                    left = start_by - time.monotonic()
                    if left <= 0:
                        return False
                    try:
                        await asyncio.wait_for(self._speech_changed.wait(), left)
                    except asyncio.TimeoutError:
                        return False
                    continue
                if self._bot_speaking:
                    await self._speech_changed.wait()
                    continue
                remaining = STEP_SPEECH_GAP_S - (
                    time.monotonic() - (self._bot_stopped_at or 0.0)
                )
                if remaining <= 0:
                    return True
                try:
                    await asyncio.wait_for(self._speech_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return True  # the silence held for the whole gap

        try:
            if not await asyncio.wait_for(wait(), ceiling):
                self._log.bind(
                    event="speech_gap.no_playback",
                    turn_id=state.turn_id,
                    step=state.step,
                ).info("the earlier step's text produced no audio; not waiting")
        except asyncio.TimeoutError:
            # means a broken signal and nothing else: the ceiling is far
            # above how long that speech can take to play
            self._log.bind(
                event="speech_gap.signal_missing",
                turn_id=state.turn_id,
                step=state.step,
                waited_s=round(ceiling, 1),
            ).warning("no playback signal for the earlier step's speech")

    # ---- tools -----------------------------------------------------------

    async def _run_tools(self, state: _TurnState) -> None:
        """Execute the step's calls concurrently. Reads run detached and
        are abandoned on interruption; writes run in tasks OUTSIDE the
        pipeline's cancellation scope (FR-46) and always finish."""
        ctx = ToolContext(
            session_id=self._session_id,
            user_id=self._user_id,
            turn_id=state.turn_id,
            emit=self._emit,
        )
        state.results = [None] * len(state.calls)
        await asyncio.gather(
            *(
                self._run_one_tool(state, i, call, ctx)
                for i, call in enumerate(state.calls)
            )
        )

    async def _run_one_tool(
        self, state: _TurnState, index: int, call: LLMToolCall, ctx: ToolContext
    ) -> None:
        tool = self._tools.get(call.name)
        log = self._log.bind(turn_id=state.turn_id, step=state.step, tool=call.name)
        outcome = "ok"
        # FR-47's anti-overwrite rule: two same-named calls in one step
        # would silently overwrite each other in the stage dict
        stage_suffix = (
            f".{index + 1}"
            if sum(1 for c in state.calls if c.name == call.name) > 1
            else ""
        )
        t0 = time.monotonic()
        if tool is None:
            result = {"status": "error", "error": f"unknown tool: {call.name}"}
            outcome = "error"
        else:
            # asyncio.create_task, NOT self.create_task: a processor-owned
            # task dies with the pipeline, and a write cancelled mid-commit
            # is the failure FR-46 exists to prevent. Reads get the same
            # detachment so an abandoned read is discarded, not
            # force-cancelled inside a DB query.
            task = asyncio.create_task(tool.handler(call.arguments, ctx))
            if tool.is_write:
                self._write_registry.add(task)
                state.write_tasks[call.id] = task
                task.add_done_callback(
                    lambda t, c=call: self._write_done(c, t)
                )
            else:
                state.read_tasks[call.id] = task
                self._live_reads.add(task)
                task.add_done_callback(lambda t, c=call: self._read_done(c, t))
            try:
                result = await asyncio.wait_for(asyncio.shield(task), TOOL_TIMEOUT_S)
            except asyncio.TimeoutError:
                # A timeout is a tool RESULT saying so, fed back to the
                # model — never an exception. The task keeps running; a
                # write that completes later lands via update-in-place.
                outcome = "timeout"
                result = {
                    "status": "timeout",
                    "error": f"tool did not finish within {TOOL_TIMEOUT_S:.0f}s",
                }
                if tool.is_write:
                    self._placeholder_calls.add(call.id)
            # a raising handler propagates: FR-42's primary error path
            # (spoken fallback, clean end of turn) — never a dead session.
        duration_ms = round((time.monotonic() - t0) * 1000)
        key = f"step.{state.step}.tool.{call.name}{stage_suffix}"
        self._recorder.record_step_stage(state.turn_id, key, duration_ms)
        if outcome == "ok" and isinstance(result, dict):
            status = result.get("status", "ok")
            outcome = (
                "error" if status in ("error", "not_found", "refused") else "ok"
            )
        log.bind(
            event="tool.invoked", duration_ms=duration_ms, outcome=outcome
        ).info(f"{call.name}: {outcome} in {duration_ms}ms")
        state.results[index] = result

    def _write_done(self, call: LLMToolCall, task: asyncio.Task) -> None:
        """Done-callback for every write task — the FR-46 landing path for
        a write that outlived its await (barge-in or timeout). Never
        raises; runs after the pipeline may be gone. A write that finished
        by RAISING lands a terminal error result the same way — a
        placeholder must never stay in_progress forever (review finding)."""
        self._write_registry.discard(task)
        if task.cancelled():
            # only the expired teardown grace cancels writes; the canceller
            # logs the WARNING with session_id
            return
        exc = task.exception()
        if exc is not None:
            # exception TYPE only: provider/DB exceptions can embed SQL
            # parameters, i.e. artifact content (NFR-9)
            self._log.bind(event="tool.write_failed").error(
                f"detached write failed: {type(exc).__name__}"
            )
            result = {"status": "error", "error": "the write failed"}
        else:
            result = task.result()
        if call.id in self._placeholder_calls:
            if self._context.update_tool_result(call.id, result):
                self._placeholder_calls.discard(call.id)
            else:
                # the step's append hasn't happened yet — land it there
                self._late_results[call.id] = result
        # if the loop's own await consumed the result, nothing to do

    def _read_done(self, call: LLMToolCall, task: asyncio.Task) -> None:
        """Abandoned-read disposal (FR-46): a discarded read that raises is
        swallowed and logged — never an unretrieved-exception traceback."""
        self._live_reads.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.bind(event="tool.abandoned_read_failed").info(
                f"abandoned read {call.name} raised: {type(exc).__name__}"
            )

    def _land_late_results(self) -> None:
        """Writes that completed between their placeholder's creation and
        the step's append land here, deterministically."""
        for call_id in list(self._late_results):
            if self._context.update_tool_result(call_id, self._late_results[call_id]):
                del self._late_results[call_id]
                self._placeholder_calls.discard(call_id)

    def _dispose_tool_round(
        self, state: _TurnState, text: str, *, interrupted: bool
    ) -> None:
        """EVERY issued call gets a terminal-or-placeholder result and the
        step is appended NOW — the FR-46 atomic-step rule, shared by
        barge-in, silent-cancel, teardown, AND the tool-crash error path
        (round 4: gather doesn't cancel siblings, so a raising read must
        not discard a concurrent write's already-committed result).
        `is not None`, never truthiness: a finished read whose result is
        an empty dict must not be relabeled."""
        results: list[dict] = []
        for i, call in enumerate(state.calls):
            write_task = state.write_tasks.get(call.id)
            if (
                state.results
                and i < len(state.results)
                and state.results[i] is not None
            ):
                results.append(state.results[i])  # finished before the cut
            elif write_task is not None:
                if write_task.done() and not write_task.cancelled():
                    if write_task.exception() is None:
                        results.append(write_task.result())
                    else:
                        # already failed — terminal, never a placeholder
                        # nothing will ever update
                        results.append(
                            {"status": "error", "error": "the write failed"}
                        )
                else:
                    # the write runs to completion; its result updates
                    # this placeholder in place — never a tail append
                    results.append({"status": "in_progress"})
                    self._placeholder_calls.add(call.id)
            else:
                read_task = state.read_tasks.get(call.id)
                if (
                    read_task is not None
                    and read_task.done()
                    and not read_task.cancelled()
                    and read_task.exception() is not None
                ):
                    # the read that raised — a terminal error, not abandoned
                    results.append({"status": "error", "error": "the tool failed"})
                else:
                    # pending/in-flight reads: abandoned, results discarded
                    results.append({"status": "cancelled"})
        self._context.append_step(text, state.calls, results, interrupted=interrupted)
        self._land_late_results()

    # ---- failure + instrumentation ---------------------------------------

    async def _speak_fallback(self, state: _TurnState, reason: str) -> None:
        """FR-42's degrade path: a brief spoken canned line and a clean end
        of turn. Nothing from a FAILED/EMPTY generation is appended, but
        words the user already heard — model text that streamed before the
        failure (a handler crash after step text) — never vanish from the
        context (review finding).
        A failed GREETING falls back to a canned greeting — "where were we"
        at session open would read as a resumed-session assumption."""
        if state.purpose == "greeting":
            line = next(self._greeting_fallback_cycle)
        else:
            line = next(self._fallback_cycle)
        self._log.bind(
            event="agent.fallback", turn_id=state.turn_id, reason=reason
        ).warning(f"spoken fallback (reason: {reason})")
        # FR-43: the one line the loop speaks itself must not run into
        # what the turn already said — an earlier step's narration, or
        # this step's own text before it failed.
        self._note_step_spoke(state)
        if state.earlier_speech_chars:
            await self._hold_for_speech_gap(state)
        try:
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame(line))
            await self.push_frame(LLMFullResponseEndFrame())
        except Exception:
            pass  # a torn pipeline can't speak; the turn still ends cleanly
        if state.step_text.strip():
            self._context.append_assistant(state.step_text.strip())

    def _instrument_call(
        self, state: _TurnState, finish: str | None, duration_ms: int, purpose: str
    ) -> None:
        """FR-47 + FR-49 for one completed (non-interrupted) LLM call."""
        state.call_closed = True  # a later barge-in must not re-record it
        if state.ttfb_ms is not None:
            self._recorder.record_step_stage(
                state.turn_id, f"step.{state.step}.ttfb.llm", state.ttfb_ms
            )
        if state.usage is not None:
            self._recorder.record_llm_usage(
                state.usage.prompt_tokens,
                state.usage.completion_tokens,
                turn_id=state.turn_id,
            )
        self._traces.record_trace(
            turn_id=state.turn_id,
            step=state.step,
            model=self._model_name,
            purpose=purpose,
            finish_reason=finish or "stop",
            prompt_tokens=state.usage.prompt_tokens if state.usage else None,
            completion_tokens=state.usage.completion_tokens if state.usage else None,
            ttfb_ms=state.ttfb_ms,
            duration_ms=duration_ms,
            input_messages=state.messages,
            output=self._trace_output(state),
        )

    def _trace_output(self, state: _TurnState) -> str:
        output = state.step_text
        if state.calls:
            calls = [{"name": c.name, "arguments": c.arguments} for c in state.calls]
            output += ("\n" if output else "") + "[tool_calls] " + json.dumps(
                calls, ensure_ascii=False, default=str
            )
        return output

    # ---- interruption (FR-46) --------------------------------------------

    async def _start_interruption(self):
        # The framework's real interruption hook (spike finding): the
        # broadcast InterruptionFrame is handled out of band, never in
        # process_frame. Cancel the turn task FIRST, then finalize from the
        # state the loop holds — the enqueue points live here, never behind
        # the cancelled await (FR-49).
        task = self._turn_task
        if task is not None:
            self._turn_task = None
            await self.cancel_task(task)
            self._finalize_interrupted_turn()
        else:
            # FR-61: no live turn means this barge-in (or ordinary speech
            # start) landed during the LAST turn's playback tail — if the
            # cut left context text the user never heard, mark it now,
            # before the new user turn appends.
            tail = self._uttered[self._uttered_mark :]
            if not tail and self._bot_spoke_this_turn:
                # audio played this turn but nothing of the LAST entry was
                # released. If the transport has ever released played text
                # this session the signal works, so the cut landed before
                # the entry's first words finished: keep its first
                # sentence, marked as cut (2026-11-12: a four-sentence
                # answer heard for a second stayed in the context whole).
                # If it never has, the signal itself is unproven — a
                # session's first spoken reply — so nothing is amended,
                # and a broken signal warns once per session instead of
                # corrupting every turn (round-2 review).
                if self._release_seen:
                    self._context.cut_to_first_sentence()
                elif not self._fr61_signal_warned:
                    self._fr61_signal_warned = True
                    self._log.bind(event="fr61.signal_suspect").warning(
                        "bot audio played this turn but the transport has "
                        "released no text this session — playback-cut "
                        "amendment skipped"
                    )
            else:
                # only THIS entry's own releases vouch for it: an earlier
                # entry's audio (an earlier step's text) is excluded by the
                # mark, so a reply that never played annotates instead of
                # hiding behind the mismatch valve (round-3 blocker)
                self._context.amend_unspoken_tail(" ".join(tail))
        await super()._start_interruption()

    async def cleanup(self):
        # End-tap/disconnect teardown is the THIRD interruption path
        # (review round 2), alongside barge-in and next-turn: the pipeline
        # cancels the turn task with no interruption broadcast, and the cut
        # step still owes its interrupted trace and partial usage — FR-49's
        # "the cut-off turns are precisely the ones a trace is for". This
        # hook runs inside the runner, before companion stops the
        # recorders, so the rows land in live queues.
        task = self._turn_task
        if task is not None:
            self._turn_task = None
            await self.cancel_task(task)
            self._finalize_interrupted_turn()
        # A wedged abandoned read must not outlive the session as
        # destroyed-task noise (round 4); _read_done swallows the cancel.
        for read_task in list(self._live_reads):
            read_task.cancel()
        await super().cleanup()

    def _finalize_interrupted_turn(self) -> None:
        state = self._state
        self._state = None
        if state is None:
            return
        log = self._log.bind(turn_id=state.turn_id)
        # 1. The interrupted trace + partial usage (FR-49/FR-46: spent is
        # recorded, from state already held) — ONLY for a call cut mid-
        # stream. A barge-in during the tool ROUND arrives after
        # _instrument_call already closed this step's call; re-recording
        # would double the usage and duplicate the trace row (FR-47 iv).
        if not state.call_closed and state.t0 is not None and state.messages:
            duration_ms = round((time.monotonic() - state.t0) * 1000)
            if state.usage is not None:
                self._recorder.record_llm_usage(
                    state.usage.prompt_tokens,
                    state.usage.completion_tokens,
                    turn_id=state.turn_id,
                )
            self._traces.record_trace(
                turn_id=state.turn_id,
                step=state.step,
                model=self._model_name,
                purpose=state.purpose,
                finish_reason=FINISH_INTERRUPTED,
                prompt_tokens=state.usage.prompt_tokens if state.usage else None,
                completion_tokens=(
                    state.usage.completion_tokens if state.usage else None
                ),
                ttfb_ms=state.ttfb_ms,
                duration_ms=duration_ms,
                input_messages=state.messages,
                output=self._trace_output(state),
            )
        # 2. What the context holds is DEFINED (FR-46): the spoken prefix
        # (sentence-level over-approximation), marked with the
        # provider-owned sentinel.
        prefix = self._spoken_prefix(state)
        if state.calls:
            self._dispose_tool_round(state, prefix, interrupted=True)
            log.bind(event="agent.interrupted", step=state.step).info(
                f"interrupted mid-tool-round: {len(state.calls)} call(s) disposed"
            )
        elif prefix:
            self._context.append_assistant(prefix, interrupted=True)
            log.bind(event="agent.interrupted", step=state.step).info(
                "interrupted mid-stream; spoken prefix recorded"
            )
        else:
            log.bind(event="agent.interrupted", step=state.step).info(
                "interrupted before any speech; nothing to record"
            )
        # 3. Turn end for the metrics buffer (FR-47): a measurement that
        # already exists flushes the row; a pre-audio interruption clears.
        self._recorder.turn_ended(state.turn_id)

    def _spoken_prefix(self, state: _TurnState) -> str:
        """What was SENT FOR SYNTHESIS for the interrupted STEP (the
        accepted sentence-level over-approximation), with one exclusion
        the loop owns because it knows the exact texts: sentences
        belonging to EARLIER steps' already-appended text — sentence
        aggregation lags the loop, so a fast tool round starts step N+1
        before step N's sentence is captured, and without this filter it
        would be duplicated into the interrupted step's entry."""
        kept = []
        for sentence in state.spoken:
            stripped = sentence.strip()
            if not stripped:
                continue
            if stripped in state.prior_sentences:
                continue  # captured for an earlier step — exact match
            # Late-arriving earlier-step sentences that were never captured
            # before the reset: match against the appended text, but only
            # exactly or for long strings — bare containment would eat a
            # genuinely-spoken short "Okay." that happens to appear inside
            # an earlier step's text (round-3 nit). Over-approximation is
            # the accepted direction.
            if any(
                stripped == appended.strip()
                or (len(stripped) >= 15 and stripped in appended)
                for appended in state.appended_texts
            ):
                continue
            kept.append(stripped)
        return " ".join(kept)


class AgentLoopObserver(BaseObserver):
    """The loop's eyes downstream of itself. The loop sits upstream of TTS
    and cannot see playback or synthesis frames for free; this observer
    feeds it the three signals the spec names: bot-started speaking
    (FR-61's audio-began valve), the sentence-level synthesis stream
    (FR-46's spoken-prefix capture — the same AggregatedTextFrame stream
    FR-20 logs), and user-speech-start (FR-44's straggler boundary)."""

    # Dedup window: frames are re-observed once per pipeline hop (a handful
    # of times), never later — a small bound suffices and keeps the set from
    # growing for the whole session (this observer taps the highest-volume
    # frame types: one id per synthesized sentence).
    _SEEN_MAX = 2048

    def __init__(self, loop: AgentLoopProcessor, **kwargs):
        super().__init__(**kwargs)
        self._loop = loop
        self._seen: set = set()
        self._seen_order: deque = deque()

    def _already_seen(self, frame_id) -> bool:
        if frame_id in self._seen:
            return True
        self._seen.add(frame_id)
        self._seen_order.append(frame_id)
        if len(self._seen_order) > self._SEEN_MAX:
            self._seen.discard(self._seen_order.popleft())
        return False

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, UserStartedSpeakingFrame):
            if data.direction != FrameDirection.DOWNSTREAM:
                return
            if self._already_seen(frame.id):
                return
            self._loop.note_user_speech_start()
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._already_seen(frame.id):
                return
            self._loop.note_bot_spoke()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._already_seen(frame.id):
                return
            self._loop.note_bot_stopped()
        elif isinstance(frame, TTSTextFrame):
            # FR-61: only the OUTPUT TRANSPORT's release counts — it holds
            # text frames behind their preceding audio and discards them
            # on interruption, so this push means "actually played" (the
            # same frame's earlier TTS->transport hops have other sources
            # and are ignored, unmarked, so the release is never deduped
            # away).
            if data.direction != FrameDirection.DOWNSTREAM:
                return
            if not isinstance(data.source, BaseOutputTransport):
                return
            if self._already_seen(frame.id):
                return
            self._loop.note_uttered_sentence(frame.text)
        elif isinstance(frame, AggregatedTextFrame) and not isinstance(
            frame, TTSTextFrame
        ):
            if data.direction != FrameDirection.DOWNSTREAM:
                return
            if self._already_seen(frame.id):
                return
            self._loop.note_spoken_sentence(frame.text)
