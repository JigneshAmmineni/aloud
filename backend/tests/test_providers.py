"""Provider factory construction tests (SDD §8) — fake keys, no network."""

import pytest

from agent.providers import (
    ClaudeLoopClient,
    GeminiLoopClient,
    make_judge_llm,
    make_loop_llm,
    make_stt,
    make_tts,
    validate_llm_config,
)
from agent.sanitizer import make_text_filters
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        deepgram_api_key="dg_fake",
        anthropic_api_key="a_fake",
        google_api_key="g_fake",
        cartesia_api_key="c_fake",
        cartesia_voice_id="voice",
        llm_model="claude-sonnet-5-5",
        judge_model="claude-haiku-5-5",
        llm_effort="medium",
        stt_provider="deepgram_flux",
        llm_provider="anthropic",
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
    assert isinstance(make_loop_llm(s), ClaudeLoopClient)
    assert make_tts(s, text_filters=make_text_filters(True)) is not None


def test_flux_eot_threshold_comes_from_settings():
    stt = make_stt(_settings(flux_eot_threshold=0.95))
    assert stt._settings.eot_threshold == 0.95


def test_cartesia_speed_comes_from_settings():
    tts = make_tts(_settings(cartesia_speed=0.7), text_filters=[])
    assert tts._settings.generation_config.speed == 0.7


def test_loop_and_judge_clients_use_their_own_models():
    s = _settings()
    loop_llm, judge_llm = make_loop_llm(s), make_judge_llm(s)
    assert (loop_llm._model, loop_llm._effort) == ("claude-sonnet-5-5", "medium")
    # the judge is the smaller model at the lowest effort, whatever LLM_EFFORT says
    assert (judge_llm._model, judge_llm._effort) == ("claude-haiku-5-5", "low")


def test_google_provider_still_constructs_for_loop_and_judge():
    s = _settings(
        llm_provider="google",
        llm_model="gemini-2.5-flash",
        judge_model="gemini-2.5-flash-lite",
    )
    assert isinstance(make_loop_llm(s), GeminiLoopClient)
    judge = make_judge_llm(s)
    assert isinstance(judge, GeminiLoopClient)
    assert judge._model == "gemini-2.5-flash-lite"


def test_anthropic_provider_rejects_a_model_it_has_no_request_shape_for():
    # the likeliest misconfiguration after the swap: a leftover Gemini
    # LLM_MODEL line in .env under LLM_PROVIDER=anthropic
    with pytest.raises(ValueError, match="gemini-2.5-flash"):
        make_loop_llm(_settings(llm_model="gemini-2.5-flash"))


def test_boot_validation_rejects_every_pair_no_client_can_run():
    """PR #24 round 1: these mistakes used to boot clean and fail (or,
    for the judge, silently refuse) per session."""
    validate_llm_config(_settings())  # the shipped defaults pass
    validate_llm_config(
        _settings(llm_provider="google", llm_model="gemini-2.5-flash", judge_model="gemini-2.5-flash")
    )
    # swap to Claude with the Gemini model line left behind
    with pytest.raises(ValueError, match="gemini-2.5-flash"):
        validate_llm_config(_settings(llm_model="gemini-2.5-flash"))
    # a judge model the provider cannot run
    with pytest.raises(ValueError, match="claude-opus-5-5"):
        validate_llm_config(_settings(judge_model="claude-opus-5-5"))
    # swap back to Gemini with the Claude judge line left behind
    with pytest.raises(ValueError, match="claude-haiku-5-5"):
        validate_llm_config(_settings(llm_provider="google", llm_model="gemini-2.5-flash"))


def test_app_refuses_to_boot_on_a_model_the_provider_cannot_run():
    """The check has to run at import of app.main (where the deploy
    health check can see it fail), not at the first session."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, LLM_PROVIDER="anthropic", LLM_MODEL="gemini-2.5-flash")
    env.pop("JUDGE_MODEL", None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "Unsupported Anthropic model" in result.stderr


def test_unknown_providers_raise():
    with pytest.raises(ValueError):
        make_stt(_settings(stt_provider="nope"))
    with pytest.raises(ValueError):
        make_loop_llm(_settings(llm_provider="nope"))
    with pytest.raises(ValueError):
        make_judge_llm(_settings(llm_provider="nope"))
    with pytest.raises(ValueError):
        make_tts(_settings(tts_provider="nope"), text_filters=[])
