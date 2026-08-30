"""Provider factory construction tests (SDD §8) — fake keys, no network."""

import pytest

from agent.providers import make_llm, make_stt, make_tts
from agent.sanitizer import make_text_filters
from app.config import Settings


def _settings(**overrides) -> Settings:
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
        cartesia_speed=0.85,
        flux_eot_threshold=0.8,
        database_url="sqlite+aiosqlite://",
        firebase_service_account_path="test-not-a-real-key.json",
        rate_stt_per_minute=0.0,
        rate_llm_per_1m_tokens_in=0.0,
        rate_llm_per_1m_tokens_out=0.0,
        rate_tts_per_1m_chars=0.0,
    )
    base.update(overrides)
    return Settings(**base)


def test_factories_construct():
    s = _settings()
    assert make_stt(s) is not None
    assert make_llm(s) is not None
    assert make_tts(s, text_filters=make_text_filters(True)) is not None


def test_flux_eot_threshold_comes_from_settings():
    stt = make_stt(_settings(flux_eot_threshold=0.95))
    assert stt._settings.eot_threshold == 0.95


def test_cartesia_speed_comes_from_settings():
    tts = make_tts(_settings(cartesia_speed=0.7), text_filters=[])
    assert tts._settings.generation_config.speed == 0.7


def test_unknown_providers_raise():
    with pytest.raises(ValueError):
        make_stt(_settings(stt_provider="nope"))
    with pytest.raises(ValueError):
        make_llm(_settings(llm_provider="nope"))
    with pytest.raises(ValueError):
        make_tts(_settings(tts_provider="nope"), text_filters=[])
