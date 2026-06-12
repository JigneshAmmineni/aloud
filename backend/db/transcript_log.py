"""Transcript ops log (SDD §2.7, FR-20).

A pipeline observer taps final user transcriptions (TranscriptionFrame) and
the assistant's sentence-level spoken text (AggregatedTextFrame, emitted by
the TTS service post-sanitizer — its TTSTextFrame subclass carries per-WORD
timestamped fragments and is excluded). A background writer batches inserts
so DB latency never sits on the audio path. Write failures are logged and
dropped — the ops log must never disturb a live conversation.

Not user-facing; never injected into the agent's context.
"""

import asyncio
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
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import TranscriptEvent

_STOP = object()

FLUSH_INTERVAL_S = 1.0
MAX_BATCH = 25


@dataclass
class _Row:
    session_id: str
    ts: datetime
    role: str
    kind: str
    text: str


class TranscriptLogObserver(BaseObserver):
    """Observes the pipeline and enqueues transcript rows (no I/O here)."""

    def __init__(self, session_id: str, queue: asyncio.Queue, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._queue = queue
        self._seen: set = set()

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)

        if isinstance(frame, TranscriptionFrame):
            self._enqueue(role="user", kind="final_transcript", text=frame.text)
        elif isinstance(frame, AggregatedTextFrame) and not isinstance(
            frame, TTSTextFrame
        ):
            self._enqueue(role="agent", kind="agent_text", text=frame.text)

    def _enqueue(self, *, role: str, kind: str, text: str) -> None:
        if not text.strip():
            return
        self._queue.put_nowait(
            _Row(
                session_id=self._session_id,
                ts=datetime.now(timezone.utc),
                role=role,
                kind=kind,
                text=text,
            )
        )


class TranscriptWriter:
    """Background task draining the queue into transcript_events in batches."""

    def __init__(self, session_id: str, db_sessions: async_sessionmaker):
        self._session_id = session_id
        self._db_sessions = db_sessions
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._log = logger.bind(session_id=session_id, component="db.transcripts")

    def observer(self) -> TranscriptLogObserver:
        return TranscriptLogObserver(self._session_id, self._queue)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Final flush; await before marking the session ended."""
        if self._task is None:
            return
        self._queue.put_nowait(_STOP)
        await self._task
        self._task = None

    async def _run(self) -> None:
        buffer: list[_Row] = []
        stopping = False
        while not stopping:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=FLUSH_INTERVAL_S
                )
                if item is _STOP:
                    stopping = True
                else:
                    buffer.append(item)
            except asyncio.TimeoutError:
                pass
            if buffer and (stopping or len(buffer) >= MAX_BATCH or self._queue.empty()):
                await self._flush(buffer)
                buffer = []

    async def _flush(self, rows: list[_Row]) -> None:
        try:
            async with self._db_sessions() as db:
                db.add_all(
                    TranscriptEvent(
                        session_id=r.session_id,
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
        except Exception as e:
            self._log.bind(event="transcript.write_failed", rows=len(rows)).error(
                f"dropping {len(rows)} transcript rows: {e}"
            )
