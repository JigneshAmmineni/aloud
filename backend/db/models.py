"""Schema (SDD §3). Sensitive content columns (🔒, encrypted post-MVP per
NFR-6) are kept separate from metadata — only `transcript_events.text` is
sensitive in this slice; timestamps/IDs/status stay plaintext so queries and
dashboards keep working when encryption lands.

Migrations: `create_all` at boot for the MVP; Alembic when the schema starts
churning.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # transport pc_id
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|ended
    end_reason: Mapped[str | None] = mapped_column(String(16))  # user|error


class TranscriptEvent(Base):
    __tablename__ = "transcript_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(8))  # user|agent
    kind: Mapped[str] = mapped_column(String(24))  # final_transcript|agent_text
    text: Mapped[str] = mapped_column(Text)  # 🔒 sensitive
    turn_id: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


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
