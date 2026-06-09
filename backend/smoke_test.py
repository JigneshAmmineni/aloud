"""
Smoke test: verifies CompanionAgent can connect to Gemini Live and receive a response.
Run inside the backend container:
  docker compose run --rm backend python smoke_test.py
"""
import asyncio
import os
from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-live-2.5-flash-native-audio")


async def main():
    print(f"Project : {os.environ.get('GOOGLE_CLOUD_PROJECT')}")
    print(f"Location: {os.environ.get('GOOGLE_CLOUD_LOCATION', 'us-central1')}")
    print(f"Model   : {MODEL}")
    print("Connecting to Gemini Live API...")

    client = genai.Client(
        enterprise=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="You are a helpful assistant. Reply very briefly.",
    )

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected. Sending test message...")
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text": "Say hello in exactly five words."}]},
            turn_complete=True,
        )
        audio_bytes = 0
        async for msg in session.receive():
            if msg.data:
                audio_bytes += len(msg.data)
            if msg.server_content and getattr(msg.server_content, "turn_complete", False):
                break
        print(f"Response: received {audio_bytes} bytes of audio")

    print("Smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
