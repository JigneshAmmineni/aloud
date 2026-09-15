"""The agent loop (§4.10): the pipeline's LLM stage, replaced.

AgentLoopProcessor sits exactly where the provider LLM service sat —
user-turn text in (LLMContextFrame from the user aggregator, the frame the
FR-42 spike named), streamed text frames out to the sanitizer → TTS chain.
Within one turn it can reason, call tools, observe results, and chain
further calls before and while speaking (FR-42), never delaying first
audio (FR-43), and surviving barge-in with its in-flight work correctly
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

from agent.context import ContextProvider
from agent.prompts import FALLBACK_LINES, FILLER_LINES, WRAP_UP_INSTRUCTION
from agent.providers import (
    FINISH_INTERRUPTED,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
)
from agent.tools import Tool, ToolContext, registry_schemas

# Hard bounds (FR-42). True LLM-call ceiling is MAX_STEPS + 1: hitting the
# cap triggers one forced final call with tool selection forbidden.
MAX_STEPS = 5
TOOL_TIMEOUT_S = 10.0
# FR-43's named lever for the atomic-delivery branch, deliberately
# DISENGAGED: the FR-48 spike measured ~1.0s of pre-call dead air for a
# typical create_artifact — inside NFR-1's 3s — so no blind deadline runs
# and a slow PLAIN turn never gets filler (the no-tool-turn mandate). If
# artifact sizes ever push the gap past budget, this is the knob the spec
# names, with its false positive accepted knowingly.
FILLER_DEADLINE_MS = 700


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
        self.step_filler = ""  # filler spoken this step, not yet appended
        self.filler_spoken: list[str] = []  # all filler texts this turn
        self.calls: list[LLMToolCall] = []  # current step's tool calls
        self.results: list[dict | None] = []
        self.write_tasks: dict[str, asyncio.Task] = {}  # call.id -> task
        self.read_tasks: dict[str, asyncio.Task] = {}
        self.t0: float | None = None  # current call's start
        self.ttfb_ms: int | None = None
        self.usage = None  # LLMUsage when the provider reported it
        self.spoken: list[str] = []  # AggregatedTextFrame texts this turn


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
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
        self._llm = llm
        self._tools = {t.name: t for t in registry}
        self._schemas = registry_schemas(registry)
        self._session_id = session_id
        self._user_id = user_id
        self._model_name = model_name
        self._recorder = recorder
        self._traces = traces
        self._emit = emit
        # FR-46: session-level in-flight-write set, owned by companion so
        # teardown and the SIGTERM drain can await it; the loop only adds.
        self._write_registry = write_registry
        self._log = logger.bind(session_id=session_id, component="agent.loop")

        self._turn_task: asyncio.Task | None = None
        self._state: _TurnState | None = None
        # FR-43 playback state, fed by AgentLoopObserver: the loop sits
        # upstream of TTS, so "playing or queued" is derived from the text
        # it forwarded and the bot-started/stopped frames flowing back.
        self._bot_speaking = False
        self._audio_pending = False
        # FR-44 straggler discriminator: a context frame arriving before
        # Flux's next user-speech-start is the consumed turn's leftover.
        self._await_user_speech = False
        # FR-46 late-write bookkeeping (all event-loop-serialized):
        self._placeholder_calls: set[str] = set()  # appended as placeholder
        self._late_results: dict[str, dict] = {}  # completed, not yet landed
        self._filler_cycle = itertools.cycle(FILLER_LINES)
        self._fallback_cycle = itertools.cycle(FALLBACK_LINES)

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
            self._log.bind(
                event="turn.straggler_dropped", chars=len(user_text)
            ).info("late final after consumed turn dropped")
            return
        self._await_user_speech = True
        if self._turn_task is not None:
            await self.cancel_task(self._turn_task)
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

    def note_bot_speaking(self, speaking: bool) -> None:
        self._bot_speaking = speaking
        if not speaking:
            self._audio_pending = False  # everything queued has played out

    def note_spoken_sentence(self, text: str) -> None:
        """FR-46: the sentence-level synthesis stream (the same one FR-20
        logs) — the spoken-prefix over-approximation source."""
        if self._state is not None:
            self._state.spoken.append(text)

    # ---- the loop --------------------------------------------------------

    async def _run_turn(self, user_text: str | None):
        purpose = "greeting" if user_text is None else "turn"
        state = self._state = _TurnState(self._recorder.current_turn, purpose)
        log = self._log.bind(turn_id=state.turn_id)
        try:
            if user_text is not None:
                self._context.append_user(user_text)
            ended = False
            for step in range(1, MAX_STEPS + 1):
                state.step = step
                # Greeting: tool selection forbidden, tools still declared
                # (FR-42/FR-7 — a session must not open with list_artifacts
                # volunteering last week's titles unprompted).
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
            log.bind(event="agent.turn_failed", step=state.step).error(
                f"loop error, degrading to spoken fallback: {e!r}"
            )
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
        messages = self._context.build(turn_id=state.turn_id, step=state.step)
        if wrap_up:
            messages = messages + [{"role": "system", "content": WRAP_UP_INSTRUCTION}]
        state.purpose = purpose
        state.messages = messages
        state.step_text = ""
        state.step_filler = ""
        state.calls = []
        state.results = []
        state.usage = None
        state.ttfb_ms = None
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
        async for event in self._llm.stream(
            messages, tools=self._schemas, tool_choice=tool_choice
        ):
            elapsed_ms = round((time.monotonic() - state.t0) * 1000)
            if state.ttfb_ms is None and not isinstance(event, LLMDone):
                state.ttfb_ms = elapsed_ms
            if isinstance(event, LLMTextDelta):
                state.step_text += event.text
                self._audio_pending = True
                await self.push_frame(LLMTextFrame(event.text))
            elif isinstance(event, LLMToolCall):
                if not state.calls:
                    await self._maybe_filler(state, elapsed_ms)
                state.calls.append(event)
            elif isinstance(event, LLMDone):
                finish = event.finish_reason
                state.usage = event.usage
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

        if not state.step_text and not state.calls:
            # FR-42: an empty step is a failure, not an ending — blocked or
            # truncated generations return, they don't raise.
            log.bind(event="agent.empty_step", finish_reason=finish).warning(
                "model returned neither text nor tool calls"
            )
            await self._speak_fallback(state, reason=finish or "empty")
            return True

        if state.calls:
            await self._run_tools(state)
            self._context.append_step(
                state.step_filler + state.step_text, state.calls, state.results
            )
            state.step_filler = ""
            self._land_late_results()
            return False  # next step sees the results

        self._context.append_assistant(state.step_filler + state.step_text)
        return True

    async def _maybe_filler(self, state: _TurnState, elapsed_ms: int) -> None:
        """FR-43, atomic branch (spike-selected): the backstop fires at the
        completed call's ARRIVAL, before any handler runs — and the
        condition is state-based: filler is owed whenever a tool round is
        beginning and no speech is playing or queued (a tool-only step 3
        after step 1's sentence finished playing is the same dead air as a
        silent step 1)."""
        if self._bot_speaking or self._audio_pending or state.step_text:
            return
        filler = next(self._filler_cycle)
        state.step_filler = filler + " "
        state.filler_spoken.append(filler)
        # Through the NORMAL downstream text path — FR-20's transcript log
        # records it; it counts as first audio but must not launder the
        # turn as healthy: the step.N.filler stage entry marks it (FR-47).
        self._audio_pending = True
        await self.push_frame(LLMTextFrame(filler + " "))
        self._recorder.record_step_stage(
            state.turn_id, f"step.{state.step}.filler", elapsed_ms
        )
        self._log.bind(
            event="agent.filler", turn_id=state.turn_id, step=state.step
        ).info(f"filler spoken at +{elapsed_ms}ms (silent tool round)")

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
        t0 = time.monotonic()
        outcome = "ok"
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
        self._recorder.record_step_stage(
            state.turn_id, f"step.{state.step}.tool.{call.name}", duration_ms
        )
        if outcome == "ok" and isinstance(result, dict):
            status = result.get("status", "ok")
            outcome = "error" if status in ("error", "not_found") else "ok"
        log.bind(
            event="tool.invoked", duration_ms=duration_ms, outcome=outcome
        ).info(f"{call.name}: {outcome} in {duration_ms}ms")
        state.results[index] = result

    def _write_done(self, call: LLMToolCall, task: asyncio.Task) -> None:
        """Done-callback for every write task — the FR-46 landing path for
        a write that outlived its await (barge-in or timeout). Never
        raises; runs after the pipeline may be gone."""
        self._write_registry.discard(task)
        if task.cancelled():
            # only the expired teardown grace cancels writes; the canceller
            # logs the WARNING with session_id
            return
        exc = task.exception()
        if exc is not None:
            self._log.bind(event="tool.write_failed").error(
                f"detached write failed: {exc!r}"
            )
            return
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
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.bind(event="tool.abandoned_read_failed").info(
                f"abandoned read {call.name} raised: {exc!r}"
            )

    def _land_late_results(self) -> None:
        """Writes that completed between their placeholder's creation and
        the step's append land here, deterministically."""
        for call_id in list(self._late_results):
            if self._context.update_tool_result(call_id, self._late_results[call_id]):
                del self._late_results[call_id]
                self._placeholder_calls.discard(call_id)

    # ---- failure + instrumentation ---------------------------------------

    async def _speak_fallback(self, state: _TurnState, reason: str) -> None:
        """FR-42's degrade path: a brief spoken canned line and a clean end
        of turn. Nothing FROM THE MODEL is appended; a filler the user
        already heard is appended alone (FR-43: heard words never vanish)."""
        line = next(self._fallback_cycle)
        self._log.bind(
            event="agent.fallback", turn_id=state.turn_id, reason=reason
        ).warning(f"spoken fallback (reason: {reason})")
        try:
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame(line))
            await self.push_frame(LLMFullResponseEndFrame())
        except Exception:
            pass  # a torn pipeline can't speak; the turn still ends cleanly
        if state.step_filler:
            self._context.append_assistant(state.step_filler.strip())
            state.step_filler = ""

    def _instrument_call(
        self, state: _TurnState, finish: str | None, duration_ms: int, purpose: str
    ) -> None:
        """FR-47 + FR-49 for one completed (non-interrupted) LLM call."""
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
        await super()._start_interruption()

    def _finalize_interrupted_turn(self) -> None:
        state = self._state
        self._state = None
        if state is None:
            return
        log = self._log.bind(turn_id=state.turn_id)
        # 1. The interrupted trace + partial usage (FR-49/FR-46: spent is
        # recorded, from state already held).
        if state.t0 is not None and state.messages:
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
        # (sentence-level over-approximation, filler excluded by the loop's
        # own dedupe), marked with the provider-owned sentinel.
        prefix = self._spoken_prefix(state)
        if state.calls:
            results: list[dict] = []
            for i, call in enumerate(state.calls):
                write_task = state.write_tasks.get(call.id)
                if state.results and i < len(state.results) and state.results[i]:
                    results.append(state.results[i])  # finished before barge-in
                elif write_task is not None:
                    if (
                        write_task.done()
                        and not write_task.cancelled()
                        and write_task.exception() is None
                    ):
                        results.append(write_task.result())
                    else:
                        # the write runs to completion; its result updates
                        # this placeholder in place — never a tail append
                        results.append({"status": "in_progress"})
                        self._placeholder_calls.add(call.id)
                else:
                    # pending/in-flight reads: abandoned, results discarded
                    results.append({"status": "cancelled"})
            self._context.append_step(
                state.step_filler + prefix, state.calls, results, interrupted=True
            )
            state.step_filler = ""
            self._land_late_results()
            log.bind(event="agent.interrupted", step=state.step).info(
                f"interrupted mid-tool-round: {len(state.calls)} call(s) disposed"
            )
        elif prefix or state.step_filler:
            self._context.append_assistant(
                state.step_filler + prefix, interrupted=bool(prefix)
            )
            state.step_filler = ""
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
        """What was SENT FOR SYNTHESIS this turn (the accepted sentence-level
        over-approximation), with the filler subtracted — the loop emitted
        it and knows its exact text, so its context entry stays owned by
        FR-43's prefix rule alone (recorded once, never twice)."""
        sentences = list(state.spoken)
        for filler in state.filler_spoken:
            for i, sentence in enumerate(sentences):
                stripped = sentence.strip()
                if stripped == filler or stripped == filler.strip():
                    del sentences[i]
                    break
        return " ".join(s.strip() for s in sentences if s.strip())


class AgentLoopObserver(BaseObserver):
    """The loop's eyes downstream of itself. The loop sits upstream of TTS
    and cannot see playback or synthesis frames for free; this observer
    feeds it the three signals the spec names: bot-started/stopped speaking
    (FR-43's playing-or-queued state), the sentence-level synthesis stream
    (FR-46's spoken-prefix capture — the same AggregatedTextFrame stream
    FR-20 logs), and user-speech-start (FR-44's straggler boundary)."""

    def __init__(self, loop: AgentLoopProcessor, **kwargs):
        super().__init__(**kwargs)
        self._loop = loop
        self._seen: set = set()

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, UserStartedSpeakingFrame):
            if data.direction != FrameDirection.DOWNSTREAM:
                return
            if frame.id in self._seen:
                return
            self._seen.add(frame.id)
            self._loop.note_user_speech_start()
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            if frame.id in self._seen:
                return
            self._seen.add(frame.id)
            self._loop.note_bot_speaking(isinstance(frame, BotStartedSpeakingFrame))
        elif isinstance(frame, AggregatedTextFrame) and not isinstance(
            frame, TTSTextFrame
        ):
            if data.direction != FrameDirection.DOWNSTREAM:
                return
            if frame.id in self._seen:
                return
            self._seen.add(frame.id)
            self._loop.note_spoken_sentence(frame.text)
