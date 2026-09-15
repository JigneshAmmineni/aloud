"""FR-42 spike: prove the stage swap before building the loop on it.

A custom processor sits exactly where the provider LLM service sat and
streams a canned response downstream. What this must demonstrate (the
spike's pass/fail criteria, FR-42):
  - the pipeline runs with a non-LLMService stage in the LLM slot;
  - text frames stream through sentence aggregation to TTS and are heard;
  - the greeting fires (LLMContextFrame arrives with no user turn);
  - barge-in interrupts the canned stream (via _start_interruption — the
    spike's key finding, see the override below);
  - the latency observer still logs turn.latency and the turn tracker
    still numbers turns (FR-47's survivals).

Enable with AGENT_LOOP_SPIKE=1 in the environment. This file grows into
the real AgentLoopProcessor during feature 3; the spike class is the
walking skeleton, not throwaway.
"""

import asyncio

from loguru import logger
from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

SPIKE_RESPONSE = (
    "This is the agent loop spike speaking. "
    "I am a custom stage sitting exactly where the language model used to be. "
    "If you can hear this streaming sentence by sentence, the swap works. "
    "Now try interrupting me mid-sentence to prove that barge-in still cuts "
    "me off the moment you start talking."
)


class SpikeLoopProcessor(FrameProcessor):
    """Streams a canned response wherever the LLM would have answered."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._response_task = None

    async def _start_interruption(self):
        # The framework's REAL interruption hook. Pipecat dispatches the
        # broadcast InterruptionFrame here (a SystemFrame handled out of
        # band, before process_frame), flushing this processor's own queue
        # — but it knows nothing of the detached streaming task we spawned.
        # Cancel it, or it keeps pushing words into TTS after the flush and
        # the bot talks over the user. Catching InterruptionFrame in
        # process_frame was the wrong seam (spike finding).
        if self._response_task:
            await self.cancel_task(self._response_task)
            self._response_task = None
        await super()._start_interruption()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            # A turn is ready (user turn or the greeting's LLMRunFrame,
            # both arrive here via the user aggregator).
            if self._response_task:
                await self.cancel_task(self._response_task)
            self._response_task = self.create_task(self._stream_response())
        else:
            await self.push_frame(frame, direction)

    async def _stream_response(self):
        logger.bind(component="agent.loop", event="spike.turn").info(
            "spike stage streaming canned response"
        )
        await self.push_frame(LLMFullResponseStartFrame())
        for word in SPIKE_RESPONSE.split(" "):
            await self.push_frame(LLMTextFrame(word + " "))
            await asyncio.sleep(0.04)  # ~word pace, long enough to barge into
        await self.push_frame(LLMFullResponseEndFrame())
        self._response_task = None
