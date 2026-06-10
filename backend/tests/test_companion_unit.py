"""
Unit tests for CompanionAgent._gemini_to_browser and ._browser_to_gemini.

All Gemini SDK calls are mocked — no network required.
"""
import json

import pytest

from tests.conftest import (
    MockBrowserWS,
    MockGeminiSession,
    audio_data_msg,
    audio_then_complete,
    binary_frame,
    disconnect_frame,
    end_of_turn_frame,
    inline_audio_msg,
    make_agent,
    malformed_text_frame,
    resumption_update_msg,
    turn_complete_msg,
    unknown_text_frame,
)


# ===========================================================================
# _gemini_to_browser
# ===========================================================================


class TestGeminiToBrowser:

    async def test_audio_via_msg_data_forwarded(self, pcm_1s):
        """Audio in msg.data is forwarded as binary frames."""
        session = MockGeminiSession([audio_data_msg(pcm_1s)])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == [pcm_1s]
        assert browser.sent_text == []

    async def test_audio_via_inline_data_forwarded(self, pcm_1s):
        """Audio in server_content.model_turn.parts[].inline_data is forwarded."""
        session = MockGeminiSession([inline_audio_msg(pcm_1s)])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == [pcm_1s]
        assert browser.sent_text == []

    async def test_turn_complete_forwarded_as_json(self):
        """A standalone turn_complete message triggers {"type": "turn_complete"} to browser."""
        session = MockGeminiSession([turn_complete_msg()])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == []
        assert len(browser.sent_text) == 1
        assert json.loads(browser.sent_text[0]) == {"type": "turn_complete"}

    async def test_audio_then_turn_complete(self, pcm_1s):
        """Full turn: audio chunks forwarded, then turn_complete JSON sent."""
        session = MockGeminiSession(audio_then_complete(pcm_1s))
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == [pcm_1s]
        assert len(browser.sent_text) == 1
        assert json.loads(browser.sent_text[0]) == {"type": "turn_complete"}

    async def test_multiple_audio_chunks_all_forwarded(self):
        """All audio chunks from a multi-chunk response are forwarded in order."""
        chunks = [b"a" * 9600, b"b" * 9600, b"c" * 3840]
        messages = [audio_data_msg(c) for c in chunks] + [turn_complete_msg()]
        session = MockGeminiSession(messages)
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == chunks
        assert len(browser.sent_text) == 1

    async def test_audio_with_turn_complete_in_same_message(self, pcm_1s):
        """turn_complete=True on a message that also contains audio: both are handled."""
        from tests.conftest import MockLiveMessage, MockServerContent

        msg = MockLiveMessage(
            data=pcm_1s,
            server_content=MockServerContent(turn_complete=True),
        )
        session = MockGeminiSession([msg])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == [pcm_1s]
        assert len(browser.sent_text) == 1
        assert json.loads(browser.sent_text[0]) == {"type": "turn_complete"}

    async def test_empty_message_sends_nothing(self):
        """A message with no data and no server_content is silently ignored."""
        from tests.conftest import MockLiveMessage

        session = MockGeminiSession([MockLiveMessage()])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == []
        assert browser.sent_text == []

    async def test_session_resumption_update_does_not_crash(self):
        """session_resumption_update messages are logged and not forwarded to browser."""
        session = MockGeminiSession([resumption_update_msg("handle-abc-123")])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_bytes == []
        assert browser.sent_text == []

    async def test_exception_in_receive_loop_sends_error_to_browser(self, pcm_1s):
        """If session.receive() raises, an error JSON is sent to the browser."""
        session = MockGeminiSession(
            [audio_data_msg(pcm_1s)],
            error=RuntimeError("Gemini dropped connection"),
        )
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        # Audio before error should still be forwarded
        assert browser.sent_bytes == [pcm_1s]

        # Error message should follow
        assert len(browser.sent_text) == 1
        payload = json.loads(browser.sent_text[0])
        assert payload["type"] == "error"
        assert "Gemini dropped connection" in payload["message"]

    async def test_error_json_not_sent_if_browser_ws_closed(self, pcm_1s):
        """If browser WS is already closed, the error-send failure is swallowed."""
        import asyncio

        session = MockGeminiSession(error=RuntimeError("boom"))
        browser = MockBrowserWS()

        # Patch send_text to raise
        async def _failing_send(text):
            raise RuntimeError("browser WS closed")

        browser.send_text = _failing_send

        # Should not propagate — the inner except swallows it
        await make_agent()._gemini_to_browser(session, browser)

    async def test_server_content_none_turn_complete_not_fired(self):
        """If server_content is None, turn_complete check is skipped safely."""
        from tests.conftest import MockLiveMessage

        session = MockGeminiSession([MockLiveMessage(data=b"x" * 9600, server_content=None)])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        # Audio forwarded, no turn_complete JSON (server_content was None)
        assert len(browser.sent_bytes) == 1
        assert browser.sent_text == []

    async def test_turn_complete_false_not_forwarded(self):
        """server_content present but turn_complete=False: no turn_complete JSON sent."""
        from tests.conftest import MockLiveMessage, MockServerContent

        session = MockGeminiSession([MockLiveMessage(server_content=MockServerContent(turn_complete=False))])
        browser = MockBrowserWS()

        await make_agent()._gemini_to_browser(session, browser)

        assert browser.sent_text == []


# ===========================================================================
# _browser_to_gemini
# ===========================================================================


class TestBrowserToGemini:

    async def test_binary_frame_sends_media_only(self, pcm_1s):
        """Binary (audio) frame → media blob only. activity_start arrives as a separate text signal."""
        browser = MockBrowserWS([binary_frame(pcm_1s)])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        assert len(session.realtime_inputs) == 1
        assert "media" in session.realtime_inputs[0]

    async def test_activity_start_text_frame_sends_activity_start(self):
        """{"type": "activity_start"} JSON → ActivityStart to Gemini."""
        from tests.conftest import text_frame

        browser = MockBrowserWS([text_frame({"type": "activity_start"})])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        assert len(session.realtime_inputs) == 1
        assert "activity_start" in session.realtime_inputs[0]

    async def test_binary_frame_media_blob_contains_correct_data(self, pcm_1s):
        """The media blob carries the exact PCM bytes with correct MIME type."""
        browser = MockBrowserWS([binary_frame(pcm_1s)])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        media_blob = session.realtime_inputs[0]["media"]
        assert media_blob.data == pcm_1s
        assert media_blob.mime_type == "audio/pcm;rate=16000"

    async def test_end_of_turn_sends_activity_end(self, pcm_1s):
        """{"type": "end_of_turn"} JSON → activity_end signal to Gemini."""
        from tests.conftest import text_frame

        browser = MockBrowserWS([text_frame({"type": "activity_start"}), binary_frame(pcm_1s), end_of_turn_frame()])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        # Calls: activity_start, media, activity_end
        assert len(session.realtime_inputs) == 3
        assert "activity_end" in session.realtime_inputs[2]

    async def test_full_push_to_talk_turn(self, pcm_3s):
        """Full push-to-talk: activity_start text → streaming binary chunks → end_of_turn."""
        from tests.conftest import text_frame

        browser = MockBrowserWS([text_frame({"type": "activity_start"}), binary_frame(pcm_3s), end_of_turn_frame()])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        keys = [list(call.keys())[0] for call in session.realtime_inputs]
        assert keys == ["activity_start", "media", "activity_end"]

    async def test_multiple_turns_correct_signal_sequence(self, pcm_1s):
        """Two complete push-to-talk turns in the same session."""
        from tests.conftest import text_frame

        browser = MockBrowserWS([
            text_frame({"type": "activity_start"}), binary_frame(pcm_1s), end_of_turn_frame(),
            text_frame({"type": "activity_start"}), binary_frame(pcm_1s), end_of_turn_frame(),
        ])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        keys = [list(call.keys())[0] for call in session.realtime_inputs]
        assert keys == ["activity_start", "media", "activity_end",
                        "activity_start", "media", "activity_end"]

    async def test_unknown_text_type_is_ignored(self):
        """Unknown JSON message types are silently dropped."""
        browser = MockBrowserWS([unknown_text_frame()])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        assert session.realtime_inputs == []

    async def test_malformed_json_does_not_crash(self):
        """Malformed JSON text frames are logged and dropped, not raised."""
        browser = MockBrowserWS([malformed_text_frame()])
        session = MockGeminiSession()

        # Should not raise
        await make_agent()._browser_to_gemini(browser, session)

        assert session.realtime_inputs == []

    async def test_disconnect_frame_exits_cleanly(self):
        """websocket.disconnect causes the loop to exit without error."""
        browser = MockBrowserWS([disconnect_frame()])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        assert session.realtime_inputs == []

    async def test_empty_binary_frame_skips_media_send(self):
        """Zero-byte audio is falsy — media is NOT sent.
        Only activity_end fires from the end_of_turn that follows.
        """
        browser = MockBrowserWS([binary_frame(b""), end_of_turn_frame()])
        session = MockGeminiSession()

        await make_agent()._browser_to_gemini(browser, session)

        keys = [list(c.keys())[0] for c in session.realtime_inputs]
        assert keys == ["activity_end"]

    async def test_gemini_send_error_is_logged_not_raised(self, pcm_1s, caplog):
        """If send_realtime_input raises, the error is caught and logged."""
        import logging

        browser = MockBrowserWS([binary_frame(pcm_1s)])
        session = MockGeminiSession()

        async def _failing_send(**kwargs):
            raise RuntimeError("Gemini WS closed")

        session.send_realtime_input = _failing_send

        with caplog.at_level(logging.DEBUG):
            await make_agent()._browser_to_gemini(browser, session)

        # Should not propagate
