"""CompanionAgent: builds and runs one session's pipeline (SDD §2.3, §2.4).

§4.10: the pipeline's LLM slot holds our AgentLoopProcessor — Pipecat stays
the chassis (transport, STT/turn detection, TTS, barge-in propagation); the
loop is the brain. The user-side aggregator STAYS as turn assembly (it folds
transcription frames into one user message, deferring to Flux's external
turn events); its context is scratch only — the FR-44 ContextProvider owns
the conversation. The assistant-side aggregator and the provider LLM
service are gone.
"""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from agent.context import ContextProvider
from agent.loop import AgentLoopObserver, AgentLoopProcessor
from agent.prompts import build_document_context_block, build_system_prompt
from agent.providers import make_loop_llm, make_stt, make_tts
from agent.sanitizer import make_text_filters
from agent.tools import build_registry
from app.config import Settings
from db.sessions_repo import create_session_row, end_session_row
from db.transcript_log import TranscriptWriter
from obs.latency import make_latency_observer
from obs.trace import TraceRecorder
from obs.usage import UsageMetricsObserver, UsageRecorder

# FR-46: ONE teardown grace budget for in-flight write tools, spent once —
# per-session teardown awaits it, or the SIGTERM drain does for everyone;
# never both.
WRITE_GRACE_S = 5.0

# Live pipelines by session_id. Serves FR-37's "live sessions now" count
# (single-instance truth; resets on deploy — accepted in the spec) and the
# graceful-shutdown goodbye below.
_live_tasks: dict[str, PipelineTask] = {}
# FR-46: session-level in-flight-write registry — drain-reachable by
# construction (the drain must await every session's writes BEFORE its
# cancel triggers their teardowns).
_inflight_writes: dict[str, set[asyncio.Task]] = {}
_draining = False


def live_session_count() -> int:
    return len(_live_tasks)


async def _await_writes(writes: set[asyncio.Task], log, timeout: float) -> None:
    """Await in-flight write tools under (a slice of) the single
    WRITE_GRACE_S budget; a write the expired budget abandons is cancelled
    and logged at WARNING (FR-46: its only trace must never be asyncio's
    destroyed-task noise)."""
    pending = {t for t in writes if not t.done()}
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
    for task in still_pending:
        task.cancel()
        log.bind(event="tool.write_abandoned").warning(
            "in-flight write abandoned at teardown grace expiry"
        )


async def drain_live_sessions() -> int:
    """Graceful-shutdown goodbye (SIGTERM — deploys, restarts): tell every
    connected client the session is ending, await EVERY session's in-flight
    writes under the one grace budget, and only then cancel the pipelines —
    the cancel is what triggers per-session teardown, and waiting after it
    would enqueue completed writes into recorders already stopping (FR-46:
    the ORDER is the load-bearing part)."""
    global _draining
    _draining = True
    tasks = list(_live_tasks.items())
    for session_id, task in tasks:
        try:
            await task.queue_frames(
                [RTVIServerMessageFrame(data={"type": "session.ending"})]
            )
        except Exception:
            pass  # a torn connection can't hear the goodbye; cancel anyway
    if tasks:
        await asyncio.sleep(0.5)  # let the message flush over the data channel
        log = logger.bind(component="agent.companion")
        # ONE deadline for the whole budget (never composed): the sessions
        # are still live during this wait, so a write can START inside the
        # grace window — the post-cancel sweep below catches those on
        # whatever remains of the same budget (review finding: a snapshot
        # alone lets a late write die mid-commit at process exit,
        # unlogged).
        deadline = time.monotonic() + WRITE_GRACE_S
        all_writes = {t for s in _inflight_writes.values() for t in s}
        await _await_writes(all_writes, log, deadline - time.monotonic())
        for session_id, task in tasks:
            try:
                await task.cancel()
            except Exception:
                pass
        late_writes = {t for s in _inflight_writes.values() for t in s}
        await _await_writes(late_writes, log, deadline - time.monotonic())
        log.bind(event="session.drained").info(
            f"drained {len(tasks)} live session(s) for shutdown"
        )
    return len(tasks)


def build_pipeline_parts(settings: Settings, documents=None, *, session_id=""):
    """Per-session services, the context provider, and the turn-assembly
    aggregator — separated from the transport so the assembly contracts are
    testable (test_pipeline_setup).

    `documents` (app.documents.Document list) become the second system
    message via the FR-44 provider; the aggregator's LLMContext is EMPTY
    scratch — turn assembly only, reset by the loop after each consumed
    turn, never read as history."""
    stt = make_stt(settings)
    tts = make_tts(
        settings,
        text_filters=make_text_filters(settings.tts_sanitize_enabled),
    )
    context_provider = ContextProvider(
        build_system_prompt(),
        build_document_context_block(documents) if documents else None,
        session_id=session_id,
    )
    scratch = LLMContext()
    # Flux handles end-of-turn detection itself, so the user aggregator
    # defers to external turn events instead of running its own VAD logic.
    # The assistant side of the pair retires with the LLM service (FR-44).
    user_agg, _ = LLMContextAggregatorPair(
        scratch,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )
    return stt, tts, context_provider, scratch, user_agg


class CompanionAgent:
    """One instance per session: builds the pipeline and runs it to completion.

    `user_id` arrives verified from the offer route (the auth seam) and rides
    session state from here — the session row, writers, and every tool
    handler scope by it."""

    def __init__(
        self, settings: Settings, documents=None, *, user_id: str, session_id: str
    ):
        self._settings = settings
        self._documents = documents or []
        self._user_id = user_id
        # The /start-minted session id — the ONE session identity everywhere
        # (DB rows, logs, admin URLs, the client's liveness poll). The
        # transport's pc_id is a connection detail, logged for correlation.
        self._session_id = session_id

    async def run(self, webrtc_connection) -> None:
        session_id = self._session_id
        log = logger.bind(
            session_id=session_id,
            pc_id=webrtc_connection.pc_id,
            component="agent.companion",
        )
        transport = SmallWebRTCTransport(
            webrtc_connection=webrtc_connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
        )

        stt, tts, context_provider, scratch, user_agg = build_pipeline_parts(
            self._settings, self._documents, session_id=session_id
        )

        writer = TranscriptWriter(session_id, self._user_id)
        recorder = UsageRecorder(session_id, self._user_id)
        traces = TraceRecorder(session_id, self._user_id)
        # registered in _inflight_writes just before the pipeline runs —
        # registering here would leak the entry if session setup raises
        # before the try/finally that removes it (review nit)
        write_tasks: set[asyncio.Task] = set()

        # FR-45/FR-46: the tools' server-message seam. Safe to call from a
        # detached write task after the pipeline closed: a silent no-op,
        # never an unretrieved exception (on teardown paths the client is
        # gone, so the announce is skipped by design).
        closed = False

        async def emit(data: dict) -> None:
            if closed:
                return
            try:
                await task.queue_frames([RTVIServerMessageFrame(data=data)])
            except Exception:
                pass

        loop_stage = AgentLoopProcessor(
            context=context_provider,
            llm=make_loop_llm(self._settings),
            registry=build_registry(),
            session_id=session_id,
            user_id=self._user_id,
            model_name=self._settings.llm_model,
            recorder=recorder,
            traces=traces,
            emit=emit,
            write_registry=write_tasks,
        )

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_agg,
                loop_stage,
                tts,
                transport.output(),
            ]
        )

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            # observers go on the task, NOT PipelineParams — the params model
            # silently ignores unknown fields
            observers=[
                make_latency_observer(session_id, recorder),
                writer.observer(),
                UsageMetricsObserver(recorder),
                AgentLoopObserver(loop_stage),
            ],
            enable_turn_tracking=True,
            conversation_id=session_id,
        )

        # FR-33: turn identity comes from the pipeline's own turn tracker.
        turn_tracker = task.turn_tracking_observer
        if turn_tracker is not None:

            @turn_tracker.event_handler("on_turn_started")
            async def on_turn_started(_obs, turn_number: int):
                recorder.current_turn = turn_number
        else:
            # Loud, because the failure mode is silent otherwise: no turn
            # numbers = no turn_metrics rows and NULL turn_ids, and the
            # admin overview would render as HEALTHY latency, not missing
            # data (FR-33/36/37).
            log.bind(event="turn_tracking.unavailable").error(
                "pipeline exposes no turn tracker — per-turn metrics and "
                "turn-attributed usage will be missing for this session"
            )

        # FR-32 defines the STT proxy as connect → disconnect: the clock
        # starts when the CLIENT connects, not at pipeline start — a session
        # whose ICE never completes streamed zero audio and must record 0.
        # Bound BEFORE the handler that closes over it: the handler could in
        # principle fire the moment the transport is live.
        connected_at: float | None = None

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            nonlocal connected_at
            if connected_at is None:
                connected_at = time.monotonic()
            log.bind(event="transport.connected").info(
                "Client connected; kicking off greeting"
            )
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            log.bind(event="transport.disconnected").info(
                "Client disconnected; ending session"
            )
            await task.cancel()

        await create_session_row(session_id, self._user_id)
        writer.start()
        recorder.start()
        traces.start()
        _inflight_writes[session_id] = write_tasks
        _live_tasks[session_id] = task
        log.bind(event="session.started").info("Pipeline starting")
        end_reason = "user"  # tap and connection drop are indistinguishable (resume is descoped)
        try:
            runner = PipelineRunner(handle_sigint=False)
            await runner.run(task)
        except Exception:
            end_reason = "error"
            raise
        finally:
            _live_tasks.pop(session_id, None)
            closed = True  # the emit callback goes silent from here
            if _draining and end_reason == "user":
                # ended by the shutdown drain, not the user — same label the
                # boot sweep uses, so deploys never pollute the error signal
                end_reason = "interrupted"
            # FR-46 teardown order: (1) await in-flight writes under the ONE
            # grace budget — unless the drain already spent it for everyone;
            # (2) flush + stop the writers; (3) close the row. A completed
            # write's events must land in live queues, so the wait comes
            # first; the drain path must NOT wait twice.
            if not _draining:
                await _await_writes(write_tasks, log, WRITE_GRACE_S)
            _inflight_writes.pop(session_id, None)
            # FR-32: STT usage = streamed-time proxy, recorded at session end
            # (crash-orphaned sessions are covered by the boot sweep). A
            # session the client never reached streamed nothing: 0 seconds.
            recorder.record_stt_seconds(
                time.monotonic() - connected_at if connected_at is not None else 0.0
            )
            # Writers stop CONCURRENTLY (FR-46: bounded at the max of one
            # flush ceiling, not the sum); each flush is already
            # deadline-bounded by FLUSH_TIMEOUT_S. recorder.stop() also
            # flushes a measured mid-turn row first (FR-47: teardown
            # mid-turn is a turn end).
            await asyncio.gather(recorder.stop(), writer.stop(), traces.stop())
            try:
                # deadline-bounded row close (FR-46's teardown arithmetic)
                await asyncio.wait_for(
                    end_session_row(session_id, self._user_id, end_reason),
                    timeout=2.0,
                )
            except Exception as e:
                log.bind(event="session.row_close_failed").error(
                    f"end_session_row failed or timed out: {e!r}"
                )
            log.bind(event="session.ended", end_reason=end_reason).info(
                "Pipeline finished"
            )
