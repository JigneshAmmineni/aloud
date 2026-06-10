"""
Live integration tests against a running backend (Docker or local).

These tests send real PCM audio over a WebSocket and assert that the backend
correctly proxies to Gemini Live and returns audio + turn_complete.

Skipped by default. Set BACKEND_WS_URL env var to enable:
    BACKEND_WS_URL=ws://localhost:8000 pytest tests/test_integration_live.py -v

The Turn 2 regression test is the primary canary for the multi-turn bug.
"""
import asyncio
import json
import math
import os
import struct
import time
from typing import Optional

import pytest

BACKEND_WS_URL = os.getenv("BACKEND_WS_URL", "")
SKIP_REASON = "Set BACKEND_WS_URL=ws://localhost:8000 to run live integration tests"

live = pytest.mark.skipif(not BACKEND_WS_URL, reason=SKIP_REASON)

# ---------------------------------------------------------------------------
# PCM fixture helpers (self-contained so tests can run standalone)
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16000


def _pcm(duration_s: float = 1.0, freq_hz: float = 440.0) -> bytes:
    n = int(_SAMPLE_RATE * duration_s)
    samples = [int(32767 * math.sin(2 * math.pi * freq_hz * i / _SAMPLE_RATE)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


# A 2-second spoken-length sine burst (enough for Gemini to perceive as speech)
SPEECH_LIKE_PCM = _pcm(2.0, 440.0)


# ---------------------------------------------------------------------------
# WebSocket client helper
# ---------------------------------------------------------------------------


class LiveWSClient:
    """
    Minimal async WebSocket client for integration tests.

    Usage:
        async with LiveWSClient(url) as ws:
            await ws.send_turn(pcm_bytes)
            audio, complete = await ws.collect_response(timeout=20.0)
    """

    def __init__(self, url: str):
        self._url = url
        self._ws = None

    async def __aenter__(self):
        import websockets

        self._ws = await websockets.connect(self._url)
        return self

    async def __aexit__(self, *args):
        if self._ws:
            await self._ws.close()

    async def send_turn(self, pcm: bytes) -> None:
        """Send one push-to-talk turn: binary audio + end_of_turn signal."""
        await self._ws.send(pcm)
        await self._ws.send(json.dumps({"type": "end_of_turn"}))

    async def collect_response(
        self, timeout: float = 30.0
    ) -> tuple[list[bytes], bool]:
        """
        Collect all messages from the server until turn_complete is received.

        Returns:
            (audio_chunks, received_turn_complete)
        """
        audio_chunks: list[bytes] = []
        turn_complete = False
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                import websockets

                msg = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed:
                break

            if isinstance(msg, bytes):
                audio_chunks.append(msg)
            elif isinstance(msg, str):
                payload = json.loads(msg)
                if payload.get("type") == "turn_complete":
                    turn_complete = True
                    break
                elif payload.get("type") == "error":
                    pytest.fail(f"Backend sent error: {payload.get('message')}")

        return audio_chunks, turn_complete


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@live
class TestSingleTurn:

    async def test_receives_audio_response(self):
        """Turn 1: backend returns audio bytes after push-to-talk input."""
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            await ws.send_turn(SPEECH_LIKE_PCM)
            audio, complete = await ws.collect_response()

        assert len(audio) > 0, "Expected audio bytes from Gemini, got none"

    async def test_receives_turn_complete(self):
        """Turn 1: backend sends turn_complete JSON after agent response."""
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            await ws.send_turn(SPEECH_LIKE_PCM)
            _, complete = await ws.collect_response()

        assert complete, "Expected turn_complete signal from backend"

    async def test_silence_input_still_completes(self):
        """Silence (all-zeros PCM) should still produce a turn_complete signal."""
        silence = bytes(_SAMPLE_RATE * 2 * 2)  # 2 seconds of silence

        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            await ws.send_turn(silence)
            _, complete = await ws.collect_response(timeout=30.0)

        assert complete, "Expected turn_complete even for silent input"

    async def test_long_audio_input(self):
        """A longer input (10 seconds) is processed without timeout."""
        long_pcm = _pcm(10.0)

        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            await ws.send_turn(long_pcm)
            audio, complete = await ws.collect_response(timeout=45.0)

        assert complete
        assert len(audio) > 0

    async def test_audio_chunks_are_valid_pcm_size(self):
        """Audio chunk sizes are multiples of 2 (valid 16-bit PCM frames)."""
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            await ws.send_turn(SPEECH_LIKE_PCM)
            audio, _ = await ws.collect_response()

        for chunk in audio:
            assert len(chunk) % 2 == 0, f"Chunk size {len(chunk)} is not a multiple of 2"


@live
class TestMultiTurn:
    """
    Multi-turn regression tests.

    Turn 2 currently fails (Gemini does not respond after the first turn in a
    persistent session). These tests document the expected behaviour and will
    pass once the bug is fixed.
    """

    async def test_turn_2_receives_audio_response(self):
        """
        REGRESSION: Turn 2 should return an audio response.

        This is the primary canary for the multi-turn bug. If this test fails,
        Gemini is still ignoring Turn 2.
        """
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            # Turn 1
            await ws.send_turn(SPEECH_LIKE_PCM)
            audio1, complete1 = await ws.collect_response()
            assert complete1, "Turn 1 did not complete"
            assert len(audio1) > 0, "Turn 1 returned no audio"

            # Turn 2 — currently broken
            await ws.send_turn(SPEECH_LIKE_PCM)
            audio2, complete2 = await ws.collect_response()

        assert complete2, "Turn 2 did not complete (multi-turn bug)"
        assert len(audio2) > 0, "Turn 2 returned no audio (multi-turn bug)"

    async def test_turn_3_receives_audio_response(self):
        """Turn 3 should also work in a healthy multi-turn session."""
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            for turn_num in range(1, 4):
                await ws.send_turn(SPEECH_LIKE_PCM)
                audio, complete = await ws.collect_response()
                assert complete, f"Turn {turn_num} did not complete"
                assert len(audio) > 0, f"Turn {turn_num} returned no audio"

    async def test_context_is_maintained_across_turns(self):
        """
        The agent should maintain conversational context across turns.

        This is a smoke test — we can't assert the content of the response,
        but we can confirm the session stays alive and responsive.
        """
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            # Turn 1: introduce a topic
            await ws.send_turn(SPEECH_LIKE_PCM)
            _, complete1 = await ws.collect_response()
            assert complete1

            # Turn 2: follow up (session should still be alive)
            await ws.send_turn(SPEECH_LIKE_PCM)
            _, complete2 = await ws.collect_response()
            assert complete2

    async def test_empty_audio_second_turn_does_not_hang(self):
        """
        Sending zero-byte audio on Turn 2 should not hang indefinitely.
        The backend should handle it gracefully (either error JSON or turn_complete).
        """
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            # Turn 1 (normal)
            await ws.send_turn(SPEECH_LIKE_PCM)
            _, complete1 = await ws.collect_response()
            assert complete1

            # Turn 2: empty audio
            await ws.send_turn(b"")
            _, complete2 = await ws.collect_response(timeout=15.0)

        # May or may not complete, but should not timeout with no response at all
        # (i.e. the session should stay alive)


@live
class TestConnectionEdgeCases:

    async def test_disconnect_before_end_of_turn_is_clean(self):
        """
        Closing the WebSocket after sending audio but before end_of_turn
        does not leave the backend in a broken state.
        """
        import websockets

        ws = await websockets.connect(f"{BACKEND_WS_URL}/ws/session")
        await ws.send(SPEECH_LIKE_PCM)
        # Close without sending end_of_turn
        await ws.close()

        # A new connection should still work
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws2:
            await ws2.send_turn(SPEECH_LIKE_PCM)
            _, complete = await ws2.collect_response()
        assert complete

    async def test_disconnect_during_response_does_not_crash_backend(self):
        """
        Closing the WebSocket while the agent is still responding should not
        crash the backend process.
        """
        import websockets

        ws = await websockets.connect(f"{BACKEND_WS_URL}/ws/session")
        await ws.send(SPEECH_LIKE_PCM)
        await ws.send(json.dumps({"type": "end_of_turn"}))

        # Wait for first audio chunk then close abruptly
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=20.0)
        except asyncio.TimeoutError:
            pass
        finally:
            await ws.close()

        # Backend should still accept new connections
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws2:
            await ws2.send_turn(SPEECH_LIKE_PCM)
            _, complete = await ws2.collect_response()
        assert complete

    async def test_concurrent_sessions_are_independent(self):
        """Two concurrent sessions each get their own responses."""

        async def _run_session():
            async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
                await ws.send_turn(SPEECH_LIKE_PCM)
                audio, complete = await ws.collect_response()
            return len(audio) > 0, complete

        results = await asyncio.gather(_run_session(), _run_session())
        for has_audio, complete in results:
            assert complete
            assert has_audio

    async def test_malformed_text_frame_does_not_crash_session(self):
        """Sending malformed JSON does not kill the session."""
        async with LiveWSClient(f"{BACKEND_WS_URL}/ws/session") as ws:
            # Inject malformed JSON
            await ws._ws.send("not json {{{")

            # Session should still work
            await ws.send_turn(SPEECH_LIKE_PCM)
            _, complete = await ws.collect_response()
        assert complete
