"""Environment-driven configuration (SDD §7). Fails fast, naming missing vars."""

import os
from dataclasses import dataclass

_REQUIRED = (
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "FIREBASE_SERVICE_ACCOUNT_PATH",
)

# The LLM key that must be present depends on the configured provider; the
# other provider's key may stay unset. An unknown provider requires neither
# here and fails in the providers.py factory, which names it.
_LLM_KEY = {"anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY"}
_DEFAULT_LLM_MODEL = {"anthropic": "claude-sonnet-5-5", "google": "gemini-2.5-flash"}
# The FR-60 judge's smaller model. A provider with no entry judges on
# its loop model.
_DEFAULT_JUDGE_MODEL = {"anthropic": "claude-haiku-5-5"}
# The loop runs with thinking off, and the API rejects every call at a
# higher effort than these when thinking is off.
_LLM_EFFORTS = ("low", "medium", "high")

# Public Cartesia voice used in Pipecat examples; override with CARTESIA_VOICE_ID.
_DEFAULT_VOICE_ID = "71a7ad14-091c-4e8e-a314-022ece01c121"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    deepgram_api_key: str
    anthropic_api_key: str  # "" unless LLM_PROVIDER=anthropic
    google_api_key: str  # "" unless LLM_PROVIDER=google
    cartesia_api_key: str
    cartesia_voice_id: str
    llm_model: str
    judge_model: str  # the FR-60 gate's model; smaller/faster than llm_model
    llm_effort: str  # main-loop effort (Anthropic only; ignored on google)
    stt_provider: str
    llm_provider: str
    tts_provider: str
    tts_sanitize_enabled: bool
    cartesia_speed: float  # Cartesia Sonic-3 speech rate; 1.0 = normal
    flux_eot_threshold: float  # Deepgram Flux end-of-turn confidence (0-1)
    database_url: str
    firebase_service_account_path: str  # C-4; app/auth.py reads the file lazily
    # FR-34 provider rates — cost is DERIVED from these at read time, never
    # stored. Defaults of 0.0 mean "not configured": estimates show $0 until
    # real rates from the provider consoles are set in .env.
    rate_stt_per_minute: float
    rate_llm_per_1m_tokens_in: float
    rate_llm_per_1m_tokens_out: float
    rate_tts_per_1m_chars: float


def load_settings() -> Settings:
    llm_provider = os.getenv("LLM_PROVIDER") or "anthropic"
    required = _REQUIRED + ((_LLM_KEY[llm_provider],) if llm_provider in _LLM_KEY else ())
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + " — add them to .env (see .env.example)."
        )
    llm_effort = os.getenv("LLM_EFFORT") or "medium"
    if llm_effort not in _LLM_EFFORTS:
        raise RuntimeError(
            f"LLM_EFFORT must be one of {', '.join(_LLM_EFFORTS)} — got {llm_effort!r}."
        )
    llm_model = os.getenv("LLM_MODEL") or _DEFAULT_LLM_MODEL.get(llm_provider, "")
    default_judge = _DEFAULT_JUDGE_MODEL.get(llm_provider, llm_model)
    return Settings(
        deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or "",
        google_api_key=os.getenv("GOOGLE_API_KEY") or "",
        cartesia_api_key=os.environ["CARTESIA_API_KEY"],
        cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID") or _DEFAULT_VOICE_ID,
        llm_model=llm_model,
        judge_model=os.getenv("JUDGE_MODEL") or default_judge,
        llm_effort=llm_effort,
        stt_provider=os.getenv("STT_PROVIDER") or "deepgram_flux",
        llm_provider=llm_provider,
        tts_provider=os.getenv("TTS_PROVIDER") or "cartesia",
        tts_sanitize_enabled=_env_bool("TTS_SANITIZE_ENABLED", True),
        cartesia_speed=_env_float("CARTESIA_SPEED", 0.85),
        flux_eot_threshold=_env_float("FLUX_EOT_THRESHOLD", 0.8),
        database_url=os.getenv("DATABASE_URL")
        or "postgresql+asyncpg://aloud:aloud@localhost:5432/aloud",
        firebase_service_account_path=os.environ["FIREBASE_SERVICE_ACCOUNT_PATH"],
        rate_stt_per_minute=_env_float("RATE_STT_PER_MINUTE", 0.0),
        rate_llm_per_1m_tokens_in=_env_float("RATE_LLM_PER_1M_TOKENS_IN", 0.0),
        rate_llm_per_1m_tokens_out=_env_float("RATE_LLM_PER_1M_TOKENS_OUT", 0.0),
        rate_tts_per_1m_chars=_env_float("RATE_TTS_PER_1M_CHARS", 0.0),
    )
