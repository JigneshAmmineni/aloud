"""CompanionAgent: builds and runs one session's pipeline (SDD §2.3, §2.4)."""

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
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from agent.prompts import build_document_context_block, build_system_prompt
from agent.providers import make_llm, make_stt, make_tts
from agent.sanitizer import make_text_filters
from agent.tools import make_create_artifact_handler, tool_schemas
from app.config import Settings
from db.sessions_repo import create_session_row, end_session_row
from db.transcript_log import TranscriptWriter
from obs.latency import make_latency_observer
from obs.usage import UsageMetricsObserver, UsageRecorder

# FR-37's "live sessions now": an in-process count of running pipelines
# (single-instance truth; resets on deploy — accepted in the spec).
_live_sessions = 0


def live_session_count() -> int:
    return _live_sessions


def build_pipeline_parts(settings: Settings, documents=None):
    """Per-session services, context, and aggregators — separated from the
    transport so the assembly contracts are testable (test_pipeline_setup).

    `documents` (app.documents.Document list) are injected as a second system
    message after the base prompt; with none, the context is just the prompt."""
    stt = make_stt(settings)
    llm = make_llm(settings)
    tts = make_tts(
        settings,
        text_filters=make_text_filters(settings.tts_sanitize_enabled),
    )

    messages = [{"role": "system", "content": build_system_prompt()}]
    if documents:
        messages.append(
            {"role": "system", "content": build_document_context_block(documents)}
        )
    context = LLMContext(
        messages=messages,
        tools=tool_schemas(),
    )
    # Flux handles end-of-turn detection itself, so the user aggregator
    # defers to external turn events instead of running its own VAD logic.
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )
    return stt, llm, tts, context, user_agg, assistant_agg


class CompanionAgent:
    """One instance per session: builds the pipeline and runs it to completion.

    `user_id` arrives verified from the offer route (the auth seam) and rides
    session state from here — the session row, transcript writer, and artifact
    handler all scope by it."""

    def __init__(self, settings: Settings, documents=None, *, user_id: str):
        self._settings = settings
        self._documents = documents or []
        self._user_id = user_id

    async def run(self, webrtc_connection) -> None:
        session_id = webrtc_connection.pc_id
        log = logger.bind(session_id=session_id, component="agent.companion")
        transport = SmallWebRTCTransport(
            webrtc_connection=webrtc_connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
        )

        stt, llm, tts, context, user_agg, assistant_agg = build_pipeline_parts(
            self._settings, self._documents
        )
        llm.register_function(
            "create_artifact",
            make_create_artifact_handler(session_id, self._user_id),
        )

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_agg,
                llm,
                tts,
                transport.output(),
                assistant_agg,
            ]
        )

        writer = TranscriptWriter(session_id, self._user_id)
        recorder = UsageRecorder(session_id, self._user_id)

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

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
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
        global _live_sessions
        _live_sessions += 1
        session_started = time.monotonic()
        log.bind(event="session.started").info("Pipeline starting")
        end_reason = "user"  # tap and connection drop are indistinguishable (resume is descoped)
        try:
            runner = PipelineRunner(handle_sigint=False)
            await runner.run(task)
        except Exception:
            end_reason = "error"
            raise
        finally:
            _live_sessions -= 1
            # FR-32: STT usage = streamed-time proxy, recorded at session end
            # (crash-orphaned sessions are covered by the boot sweep).
            recorder.record_stt_seconds(time.monotonic() - session_started)
            await recorder.stop()
            await writer.stop()
            await end_session_row(session_id, self._user_id, end_reason)
            log.bind(event="session.ended", end_reason=end_reason).info(
                "Pipeline finished"
            )
