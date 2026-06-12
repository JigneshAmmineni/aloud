"""Shared test setup.

Harmless fake credentials so modules with import-time config (app.main) can
load in tests; individual tests override/delete via monkeypatch as needed.
"""

import os

import pytest

os.environ.setdefault("DEEPGRAM_API_KEY", "test-dg")
os.environ.setdefault("GOOGLE_API_KEY", "test-g")
os.environ.setdefault("CARTESIA_API_KEY", "test-c")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")


@pytest.fixture
def make_settings():
    """Settings factory with fake keys; override any field per test."""
    from app.config import Settings

    def _make(**overrides) -> Settings:
        base = dict(
            deepgram_api_key="dg_fake",
            google_api_key="g_fake",
            cartesia_api_key="c_fake",
            cartesia_voice_id="voice",
            llm_model="gemini-2.5-flash",
            stt_provider="deepgram_flux",
            llm_provider="google",
            tts_provider="cartesia",
            tts_sanitize_enabled=True,
            database_url="sqlite+aiosqlite://",
        )
        base.update(overrides)
        return Settings(**base)

    return _make
