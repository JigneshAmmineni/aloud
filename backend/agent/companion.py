import asyncio
import json
import os

from fastapi import WebSocket
from google import genai
from google.genai import types

MODEL = "gemini-live-2.5-flash-preview"

SYSTEM_PROMPT = """You are a thoughtful journaling companion called Aloud. Your role is to listen, \
reflect, and ask gentle guiding questions that help the user explore their thoughts more deeply.

You are not a therapist and you do not give clinical advice.
Listen carefully and respond with curiosity and warmth.
Ask one question at a time. Keep responses concise — this is a voice conversation."""


class CompanionAgent:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO", "TEXT"],
            system_instruction=SYSTEM_PROMPT,
        )

    async def run_session(self, browser_ws: WebSocket):
        async with self.client.aio.live.connect(model=MODEL, config=self.config) as session:
            await asyncio.gather(
                self._browser_to_gemini(browser_ws, session),
                self._gemini_to_browser(session, browser_ws),
            )

    async def _browser_to_gemini(self, browser_ws: WebSocket, session):
        try:
            while True:
                data = await browser_ws.receive_bytes()
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )
        except Exception:
            pass

    async def _gemini_to_browser(self, session, browser_ws: WebSocket):
        try:
            async for msg in session.receive():
                if msg.data:
                    await browser_ws.send_bytes(msg.data)
                if msg.text:
                    await browser_ws.send_text(
                        json.dumps({"type": "transcript", "role": "agent", "text": msg.text})
                    )
        except Exception:
            pass
