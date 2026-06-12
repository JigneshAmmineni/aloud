"""Config contracts (SDD §7): fail fast naming missing vars; defaults hold."""

import pytest

from app.config import _env_bool, load_settings

_REQUIRED = ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY")
_OPTIONAL = (
    "CARTESIA_VOICE_ID",
    "LLM_MODEL",
    "STT_PROVIDER",
    "LLM_PROVIDER",
    "TTS_PROVIDER",
    "TTS_SANITIZE_ENABLED",
    "DATABASE_URL",
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
    assert s.cartesia_voice_id  # falls back to a non-empty default voice
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_env_overrides(monkeypatch):
    _clear_optional(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "my-voice")
    monkeypatch.setenv("TTS_SANITIZE_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@host/db")
    s = load_settings()
    assert s.llm_model == "gemini-2.5-flash-lite"
    assert s.cartesia_voice_id == "my-voice"
    assert s.tts_sanitize_enabled is False
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
