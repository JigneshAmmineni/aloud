"""
Shared fixtures and mock factories for all backend tests.
"""
import asyncio
import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# PCM audio fixtures
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16000


def _make_pcm(duration_s: float = 1.0, freq_hz: float = 440.0) -> bytes:
    """Mono 16-bit little-endian PCM, sine wave at freq_hz for duration_s seconds."""
    n = int(_SAMPLE_RATE * duration_s)
    samples = [int(32767 * math.sin(2 * math.pi * freq_hz * i / _SAMPLE_RATE)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _make_silence(duration_s: float = 1.0) -> bytes:
    n = int(_SAMPLE_RATE * duration_s)
    return bytes(n * 2)


@pytest.fixture
def pcm_1s() -> bytes:
    return _make_pcm(1.0)


@pytest.fixture
def pcm_3s() -> bytes:
    return _make_pcm(3.0)


@pytest.fixture
def pcm_silence() -> bytes:
    return _make_silence(1.0)


# ---------------------------------------------------------------------------
# Mock Gemini message objects
# ---------------------------------------------------------------------------


@dataclass
class MockInlineData:
    data: bytes


@dataclass
class MockPart:
    inline_data: Optional[MockInlineData] = None


@dataclass
class MockModelTurn:
    parts: list = field(default_factory=list)


@dataclass
class MockServerContent:
    model_turn: Optional[MockModelTurn] = None
    turn_complete: bool = False


@dataclass
class MockResumptionUpdate:
    resumable: bool = False
    new_handle: Optional[str] = None


@dataclass
class MockLiveMessage:
    data: Optional[bytes] = None
    server_content: Optional[MockServerContent] = None
    session_resumption_update: Optional[MockResumptionUpdate] = None


# Convenience constructors for common message shapes


def audio_data_msg(data: bytes) -> MockLiveMessage:
    """Audio delivered via msg.data (the common path in practice)."""
    return MockLiveMessage(data=data, server_content=MockServerContent())


def inline_audio_msg(data: bytes) -> MockLiveMessage:
    """Audio delivered via server_content.model_turn.parts[].inline_data."""
    part = MockPart(inline_data=MockInlineData(data=data))
    return MockLiveMessage(server_content=MockServerContent(model_turn=MockModelTurn(parts=[part])))


def turn_complete_msg() -> MockLiveMessage:
    """Standalone turn_complete (no audio)."""
    return MockLiveMessage(server_content=MockServerContent(turn_complete=True))


def audio_then_complete(audio: bytes) -> list[MockLiveMessage]:
    """Typical response: one audio chunk followed by turn_complete."""
    return [audio_data_msg(audio), turn_complete_msg()]


def resumption_update_msg(handle: str, resumable: bool = True) -> MockLiveMessage:
    return MockLiveMessage(session_resumption_update=MockResumptionUpdate(resumable=resumable, new_handle=handle))


# ---------------------------------------------------------------------------
# Mock Gemini session
# ---------------------------------------------------------------------------


class MockGeminiSession:
    """
    Simulates google.genai.live.AsyncSession for unit testing.

    messages: list of MockLiveMessage objects to yield from receive().
    error: if set, raised after all messages are exhausted.

    The real SDK's receive() yields exactly one turn (breaks after turn_complete)
    and must be called again for subsequent turns. This mock is stateful: the
    first call to receive() yields all preset messages; subsequent calls yield
    nothing (simulating a cleanly closed session), which causes _gemini_to_browser's
    outer while loop to exit via the got_message=False check.
    """

    def __init__(self, messages: list = None, error: Exception = None):
        self._messages = list(messages or [])
        self._error = error
        self.realtime_inputs: list[dict] = []
        self._receive_call_count = 0

    async def send_realtime_input(self, **kwargs):
        self.realtime_inputs.append(kwargs)

    def receive(self) -> AsyncIterator:
        self._receive_call_count += 1
        is_first_call = self._receive_call_count == 1
        messages = self._messages
        error = self._error

        async def _gen():
            if is_first_call:
                for msg in messages:
                    yield msg
                if error:
                    raise error
            # Second+ calls: yield nothing → got_message=False → outer loop exits

        return _gen()


# ---------------------------------------------------------------------------
# Mock browser WebSocket (FastAPI/ASGI level)
# ---------------------------------------------------------------------------


class MockBrowserWS:
    """
    Simulates a FastAPI WebSocket for unit testing.

    incoming: list of ASGI dicts consumed by receive().
    A disconnect frame is appended automatically.
    """

    def __init__(self, incoming: list[dict] = None):
        self._queue: list[dict] = list(incoming or [])
        self._queue.append({"type": "websocket.disconnect"})
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []

    async def receive(self) -> dict:
        if self._queue:
            return self._queue.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)


# Helper frame constructors

def binary_frame(pcm: bytes) -> dict:
    return {"type": "websocket.receive", "bytes": pcm, "text": None}


def text_frame(payload: dict) -> dict:
    return {"type": "websocket.receive", "bytes": None, "text": json.dumps(payload)}


def end_of_turn_frame() -> dict:
    return text_frame({"type": "end_of_turn"})


def unknown_text_frame() -> dict:
    return text_frame({"type": "something_unknown"})


def malformed_text_frame() -> dict:
    return {"type": "websocket.receive", "bytes": None, "text": "not json {{{"}


def disconnect_frame() -> dict:
    return {"type": "websocket.disconnect"}


# ---------------------------------------------------------------------------
# CompanionAgent factory (bypasses __init__ to avoid Gemini SDK calls)
# ---------------------------------------------------------------------------


def make_agent():
    """Return a CompanionAgent with no live client (for unit-testing methods directly)."""
    from agent.companion import CompanionAgent

    agent = object.__new__(CompanionAgent)
    agent.client = MagicMock()
    agent.config = MagicMock()
    return agent
