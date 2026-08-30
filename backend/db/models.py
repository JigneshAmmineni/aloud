"""Schema (recorded in CURRENT-ARCHITECTURE.md). Sensitive content columns (🔒, encrypted post-MVP per
NFR-6) are kept separate from metadata — only `transcript_events.text` is
sensitive in this slice; timestamps/IDs/status stay plaintext so queries and
dashboards keep working when encryption lands.

Migrations: `create_all` at boot for the MVP; Alembic when the schema starts
churning.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Firebase uid
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # FR-24/FR-30: from the verified ID token's `name` claim, length-capped.
    preferred_name: Mapped[str | None] = mapped_column(String(80))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # /start-minted UUID
    # indexed: every RLS predicate and admin aggregate filters on user_id
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|ended
    # user (End tap / disconnect) | error | interrupted (process death →
    # boot sweep, or a graceful-shutdown drain)
    end_reason: Mapped[str | None] = mapped_column(String(16))


class TranscriptEvent(Base):
    __tablename__ = "transcript_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    # Denormalized from sessions so the FR-31 RLS policy is a direct column
    # check, not a subquery through another RLS'd table.
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(8))  # user|agent
    kind: Mapped[str] = mapped_column(String(24))  # final_transcript|agent_text
    text: Mapped[str] = mapped_column(Text)  # 🔒 sensitive
    turn_id: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class UsageEvent(Base):
    """FR-32: append-only raw usage. Metadata only — no sensitive columns
    (the `detail` field carries labels like an artifact kind, never content).
    Cost is never stored; it is derived from these units at read time.

    Deliberately NO foreign keys (here and on TurnMetric): the boot sweep
    writes usage rows for sessions it is simultaneously closing, `turn_id`
    has no table at all, and NFR-7's future delete-everything cascade will
    be app-level (RLS already fences reads). Don't "fix" this without that
    context."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    turn_id: Mapped[int | None] = mapped_column(Integer)  # null: session-level
    # indexed: FR-37's overview filters 24h/7d/30d windows on ts
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stage: Mapped[str] = mapped_column(String(16))  # stt|llm|tts|artifact
    unit: Mapped[str] = mapped_column(String(24))  # seconds|tokens_in|tokens_out|characters|count
    quantity: Mapped[float] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(String(64))  # e.g. artifact kind


class TurnMetric(Base):
    """FR-33: per-turn latency, persisted. Metadata only."""

    __tablename__ = "turn_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    turn_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    eot_to_first_audio_ms: Mapped[int] = mapped_column(Integer)
    stages_ms: Mapped[dict | None] = mapped_column(JSON)  # per-stage TTFBs (names+ms)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    kind: Mapped[str] = mapped_column(String(24))  # summary|action_items|cleaned_idea
    title: Mapped[str] = mapped_column(Text)  # 🔒 sensitive
    content: Mapped[str] = mapped_column(Text)  # 🔒 sensitive
