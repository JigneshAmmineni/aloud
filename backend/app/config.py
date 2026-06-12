"""Environment-driven configuration (SDD §7). Fails fast, naming missing vars."""

import os
from dataclasses import dataclass

_REQUIRED = ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY")

# Public Cartesia voice used in Pipecat examples; override with CARTESIA_VOICE_ID.
_DEFAULT_VOICE_ID = "71a7ad14-091c-4e8e-a314-022ece01c121"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    deepgram_api_key: str
    google_api_key: str
    cartesia_api_key: str
    cartesia_voice_id: str
    llm_model: str
    stt_provider: str
    llm_provider: str
    tts_provider: str
    tts_sanitize_enabled: bool


def load_settings() -> Settings:
    missing = [name for name in _REQUIRED if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + " — add them to .env (see .env.example)."
        )
    return Settings(
        deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
        google_api_key=os.environ["GOOGLE_API_KEY"],
        cartesia_api_key=os.environ["CARTESIA_API_KEY"],
        cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID") or _DEFAULT_VOICE_ID,
        llm_model=os.getenv("LLM_MODEL") or "gemini-2.5-flash",
        stt_provider=os.getenv("STT_PROVIDER") or "deepgram_flux",
        llm_provider=os.getenv("LLM_PROVIDER") or "google",
        tts_provider=os.getenv("TTS_PROVIDER") or "cartesia",
        tts_sanitize_enabled=_env_bool("TTS_SANITIZE_ENABLED", True),
    )
