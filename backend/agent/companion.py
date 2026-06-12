"""CompanionAgent: builds and runs one session's pipeline (SDD §2.3, §2.4)."""

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

from agent.prompts import build_system_prompt
from agent.providers import make_llm, make_stt, make_tts
from agent.sanitizer import make_text_filters
from app.config import Settings
from obs.latency import make_latency_observer


class CompanionAgent:
    """One instance per session: builds the pipeline and runs it to completion."""

    def __init__(self, settings: Settings):
        self._settings = settings

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

        stt = make_stt(self._settings)
        llm = make_llm(self._settings)
        tts = make_tts(
            self._settings,
            text_filters=make_text_filters(self._settings.tts_sanitize_enabled),
        )

        context = LLMContext(
            messages=[{"role": "system", "content": build_system_prompt()}]
        )
        # Flux handles end-of-turn detection itself, so the user aggregator
        # defers to external turn events instead of running its own VAD logic.
        user_agg, assistant_agg = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies()
            ),
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

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
                observers=[make_latency_observer(session_id)],
            ),
            enable_turn_tracking=True,
            conversation_id=session_id,
        )

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

        log.bind(event="session.started").info("Pipeline starting")
        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)
        log.bind(event="session.ended").info("Pipeline finished")
