"""Transcript ops log (SDD §2.7, FR-20).

A pipeline observer taps final user transcriptions (TranscriptionFrame) and
the assistant's sentence-level spoken text (AggregatedTextFrame, emitted by
the TTS service post-sanitizer — its TTSTextFrame subclass carries per-WORD
timestamped fragments and is excluded). Rows ride the shared
BackgroundBatchWriter (NFR-10): the audio path only enqueues, write failures
log and drop — the ops log must never disturb a live conversation.

Not user-facing; never injected into the agent's context.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from pipecat.frames.frames import (
    AggregatedTextFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from db.batch_writer import BackgroundBatchWriter
from db.engine import user_scoped_session
from db.models import TranscriptEvent


@dataclass
class _Row:
    session_id: str
    ts: datetime
    role: str
    kind: str
    text: str


class TranscriptLogObserver(BaseObserver):
    """Observes the pipeline and enqueues transcript rows (no I/O here)."""

    def __init__(self, session_id: str, enqueue, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._enqueue = enqueue
        self._seen: set = set()

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)

        if isinstance(frame, TranscriptionFrame):
            self._record(role="user", kind="final_transcript", text=frame.text)
        elif isinstance(frame, AggregatedTextFrame) and not isinstance(
            frame, TTSTextFrame
        ):
            self._record(role="agent", kind="agent_text", text=frame.text)

    def _record(self, *, role: str, kind: str, text: str) -> None:
        if not text.strip():
            return
        self._enqueue(
            _Row(
                session_id=self._session_id,
                ts=datetime.now(timezone.utc),
                role=role,
                kind=kind,
                text=text,
            )
        )


class TranscriptWriter:
    """Per-session, so per-user: transactions are scoped with the user_id
    handed over at session start — the FR-31 path for writers that bypass
    the HTTP layer; a write batch never spans users."""

    def __init__(self, session_id: str, user_id: str):
        self._session_id = session_id
        self._user_id = user_id
        self._log = logger.bind(session_id=session_id, component="db.transcripts")
        self._writer = BackgroundBatchWriter(self._flush, self._log)

    def observer(self) -> TranscriptLogObserver:
        return TranscriptLogObserver(self._session_id, self._writer.enqueue)

    def start(self) -> None:
        self._writer.start()

    async def stop(self) -> None:
        """Final flush; await before marking the session ended."""
        await self._writer.stop()

    async def _flush(self, rows: list) -> None:
        async with user_scoped_session(self._user_id) as db:
            db.add_all(
                TranscriptEvent(
                    session_id=r.session_id,
                    user_id=self._user_id,
                    ts=r.ts,
                    role=r.role,
                    kind=r.kind,
                    text=r.text,
                )
                for r in rows
            )
            await db.commit()
        self._log.bind(event="transcript.flushed", rows=len(rows)).debug(
            f"wrote {len(rows)} transcript rows"
        )
