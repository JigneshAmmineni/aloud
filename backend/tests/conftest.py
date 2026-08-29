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
# Never a real file: auth deps are overridden in tests, and app.auth inits
# Firebase lazily, so this path is never opened.
os.environ.setdefault("FIREBASE_SERVICE_ACCOUNT_PATH", "test-not-a-real-key.json")


@pytest.fixture
def auth_as():
    """Authenticate test requests as a chosen user by overriding the auth
    dependencies (the documented FastAPI seam — routes and everything
    downstream run for real)."""
    import app.main as main
    from app.auth import AuthedUser, get_current_user, get_current_user_checked

    def _as(user_id="user-test", name=None, admin=False, email_verified=True):
        user = AuthedUser(
            user_id=user_id,
            email=f"{user_id}@example.com",
            email_verified=email_verified,
            name=name,
            is_admin=admin,
        )
        main.app.dependency_overrides[get_current_user] = lambda: user
        main.app.dependency_overrides[get_current_user_checked] = lambda: user
        return user

    yield _as
    import app.main as main_after

    main_after.app.dependency_overrides.clear()


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

    return _make
