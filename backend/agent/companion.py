import asyncio
import json
import os

from fastapi import WebSocket
from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-live-2.5-flash-native-audio")

SYSTEM_PROMPT = """You are a thoughtful journaling companion called Aloud. Your role is to listen, \
reflect, and ask gentle guiding questions that help the user explore their thoughts more deeply.

You are not a therapist and you do not give clinical advice.
Listen carefully and respond with curiosity and warmth.
Ask one question at a time. Keep responses concise — this is a voice conversation."""


class CompanionAgent:
    def __init__(self):
        self.client = genai.Client(
            enterprise=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_PROMPT,
            # Disable server-side VAD so we can use activity_start/activity_end
            # for push-to-talk. With VAD on, batched audio confuses Gemini on turn 2+.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True
                )
            ),
            # Text transcripts let the model recall specific details (names, dates, etc.)
            # across turns far better than audio tokens alone, and drive the UI transcript view.
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    async def run_session(self, browser_ws: WebSocket):
        print(f"[session] connecting to Gemini Live (model={MODEL})", flush=True)
        try:
            async with self.client.aio.live.connect(model=MODEL, config=self.config) as session:
                print("[session] connected to Gemini Live", flush=True)
                recv_task = asyncio.ensure_future(self._gemini_to_browser(session, browser_ws))
                try:
                    await self._browser_to_gemini(browser_ws, session)
                finally:
                    recv_task.cancel()
                    try:
                        await recv_task
                    except asyncio.CancelledError:
                        pass
        except Exception as e:
            print(f"[session] FATAL: {type(e).__name__}: {e}", flush=True)
            raise

    async def _browser_to_gemini(self, browser_ws: WebSocket, session):
        """
        Push-to-talk protocol:
          - binary frames: buffered PCM audio (all at once when user presses Stop)
          - text frame {"type": "end_of_turn"}: signals Gemini the turn is complete

        Per-frame errors (malformed JSON, failed Gemini sends) are logged and the
        frame is dropped — they do NOT kill the session loop.
        """
        while True:
            try:
                msg = await browser_ws.receive()
            except Exception as e:
                print(f"[browser→gemini] receive error: {type(e).__name__}: {e}", flush=True)
                break

            if msg["type"] == "websocket.disconnect":
                print("[browser→gemini] browser disconnected", flush=True)
                break

            if msg.get("bytes"):
                # Streaming protocol: frontend sends 20ms chunks continuously during recording.
                # activity_start arrives as a separate text signal before the first chunk.
                audio_data = msg["bytes"]
                if audio_data:
                    print(f"[browser→gemini] audio chunk {len(audio_data)}B", flush=True)
                    try:
                        await session.send_realtime_input(
                            media=types.Blob(data=audio_data, mime_type="audio/pcm;rate=16000")
                        )
                    except Exception as e:
                        print(f"[browser→gemini] send error: {type(e).__name__}: {e}", flush=True)

            elif msg.get("text"):
                try:
                    payload = json.loads(msg["text"])
                    msg_type = payload.get("type")
                    if msg_type == "activity_start":
                        print("[browser→gemini] activity_start", flush=True)
                        await session.send_realtime_input(activity_start=types.ActivityStart())
                    elif msg_type == "end_of_turn":
                        print("[browser→gemini] end_of_turn → activity_end", flush=True)
                        await session.send_realtime_input(activity_end=types.ActivityEnd())
                except json.JSONDecodeError:
                    print(f"[browser→gemini] malformed JSON frame, ignoring", flush=True)
                except Exception as e:
                    print(f"[browser→gemini] send error: {type(e).__name__}: {e}", flush=True)

    async def _gemini_to_browser(self, session, browser_ws: WebSocket):
        # The SDK's receive() yields exactly one turn (breaks after turn_complete).
        # We loop so it re-enters for each subsequent turn in the same session.
        msg_count = 0
        try:
            while True:
                got_message = False
                async for msg in session.receive():
                    got_message = True
                    msg_count += 1
                    has_sc = msg.server_content is not None
                    print(f"[gemini→browser] msg#{msg_count}: data={bool(msg.data)} sc={has_sc}", flush=True)

                    if msg.data:
                        await browser_ws.send_bytes(msg.data)
                        print(f"[gemini→browser] sent {len(msg.data)}B via msg.data", flush=True)
                    elif has_sc and msg.server_content.model_turn:
                        for part in msg.server_content.model_turn.parts:
                            if part.inline_data:
                                await browser_ws.send_bytes(part.inline_data.data)
                                print(f"[gemini→browser] sent {len(part.inline_data.data)}B via inline_data", flush=True)

                    if has_sc:
                        sc = msg.server_content
                        t = getattr(sc, "input_transcription", None)
                        if t and getattr(t, "text", None):
                            await browser_ws.send_text(json.dumps({"type": "transcript", "role": "user", "text": t.text}))
                        t = getattr(sc, "output_transcription", None)
                        if t and getattr(t, "text", None):
                            await browser_ws.send_text(json.dumps({"type": "transcript", "role": "agent", "text": t.text}))
                        if getattr(sc, "turn_complete", False):
                            await browser_ws.send_text(json.dumps({"type": "turn_complete"}))
                            print("[gemini→browser] turn_complete forwarded", flush=True)

                    if msg.session_resumption_update:
                        upd = msg.session_resumption_update
                        if getattr(upd, "resumable", False) and getattr(upd, "new_handle", None):
                            print("[session] resumption handle updated", flush=True)

                if not got_message:
                    # receive() returned immediately with no messages — session closed cleanly
                    break

        except Exception as e:
            print(f"[gemini→browser] closed after {msg_count} msgs: {type(e).__name__}: {e}", flush=True)
            try:
                await browser_ws.send_text(json.dumps({"type": "error", "message": str(e)}))
            except Exception:
                pass
