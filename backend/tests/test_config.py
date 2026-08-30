"""Config contracts (SDD §7): fail fast naming missing vars; defaults hold."""

import pytest

from app.config import _env_bool, _env_float, load_settings

_REQUIRED = (
    "DEEPGRAM_API_KEY",
    "GOOGLE_API_KEY",
    "CARTESIA_API_KEY",
    "FIREBASE_SERVICE_ACCOUNT_PATH",
)
_OPTIONAL = (
    "CARTESIA_VOICE_ID",
    "LLM_MODEL",
    "STT_PROVIDER",
    "LLM_PROVIDER",
    "TTS_PROVIDER",
    "TTS_SANITIZE_ENABLED",
    "CARTESIA_SPEED",
    "FLUX_EOT_THRESHOLD",
    "DATABASE_URL",
    "RATE_STT_PER_MINUTE",
    "RATE_LLM_PER_1M_TOKENS_IN",
    "RATE_LLM_PER_1M_TOKENS_OUT",
    "RATE_TTS_PER_1M_CHARS",
)


def _clear_optional(monkeypatch):
    for name in _OPTIONAL:
        monkeypatch.delenv(name, raising=False)


def test_all_missing_required_vars_are_named(monkeypatch):
    for name in _REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as err:
        load_settings()
    for name in _REQUIRED:
        assert name in str(err.value)


def test_single_missing_var_is_named(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as err:
        load_settings()
    assert "CARTESIA_API_KEY" in str(err.value)
    assert "DEEPGRAM_API_KEY" not in str(err.value)


def test_defaults(monkeypatch):
    _clear_optional(monkeypatch)
    s = load_settings()
    assert s.llm_model == "gemini-2.5-flash"
    assert s.stt_provider == "deepgram_flux"
    assert s.llm_provider == "google"
    assert s.tts_provider == "cartesia"
    assert s.tts_sanitize_enabled is True
    assert s.cartesia_speed == 0.85
    assert s.flux_eot_threshold == 0.8
    assert s.cartesia_voice_id  # falls back to a non-empty default voice
    assert s.database_url.startswith("postgresql+asyncpg://")
    # FR-34 rates: 0.0 means "not configured" — estimates show $0, never crash
    assert s.rate_stt_per_minute == 0.0
    assert s.rate_llm_per_1m_tokens_in == 0.0
    assert s.rate_llm_per_1m_tokens_out == 0.0
    assert s.rate_tts_per_1m_chars == 0.0


def test_env_overrides(monkeypatch):
    _clear_optional(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "my-voice")
    monkeypatch.setenv("TTS_SANITIZE_ENABLED", "false")
    monkeypatch.setenv("CARTESIA_SPEED", "0.7")
    monkeypatch.setenv("FLUX_EOT_THRESHOLD", "0.95")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@host/db")
    s = load_settings()
    assert s.llm_model == "gemini-2.5-flash-lite"
    assert s.cartesia_voice_id == "my-voice"
    assert s.tts_sanitize_enabled is False
    assert s.cartesia_speed == 0.7
    assert s.flux_eot_threshold == 0.95
    assert s.database_url == "postgresql+asyncpg://x:y@host/db"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("anything-else", False),
    ],
)
def test_env_bool_values(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert _env_bool("SOME_FLAG", default=not expected) is expected


def test_env_bool_default_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert _env_bool("SOME_FLAG", default=True) is True
    assert _env_bool("SOME_FLAG", default=False) is False
    monkeypatch.setenv("SOME_FLAG", "  ")
    assert _env_bool("SOME_FLAG", default=True) is True


def test_env_float_parses_overrides_and_falls_back(monkeypatch):
    monkeypatch.setenv("SOME_NUM", "0.85")
    assert _env_float("SOME_NUM", default=1.0) == 0.85
    monkeypatch.setenv("SOME_NUM", "not-a-number")  # invalid → default
    assert _env_float("SOME_NUM", default=1.0) == 1.0
    monkeypatch.delenv("SOME_NUM", raising=False)  # unset → default
    assert _env_float("SOME_NUM", default=0.8) == 0.8
