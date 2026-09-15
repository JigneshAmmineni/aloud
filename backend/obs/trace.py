"""LLM trace capture (FR-49). Per-session like every writer (FR-31: a
write batch never spans users), riding the shared BackgroundBatchWriter —
the hot path only enqueues, a dropped batch logs and drops, never touching
the conversation (NFR-10).

The one named NFR-10 deviation: input_messages is SERIALIZED AT ENQUEUE
TIME, never a reference to the live message list a later append_step
mutates (a lazy trace would show messages that were never sent). O(context)
— ~1ms per step at long-session sizes — sitting between step N's stream
and step N+1's call.
"""

import json
from datetime import datetime, timezone

from loguru import logger

from db.batch_writer import BackgroundBatchWriter
from db.engine import user_scoped_session
from db.models import LLMTrace


class TraceRecorder:
    """One per session. The loop enqueues a row it already holds every
    field of — including the interrupted partial from the cancellation
    path itself (FR-49: never behind the cancelled await)."""

    def __init__(self, session_id: str, user_id: str):
        self._session_id = session_id
        self._user_id = user_id
        self._log = logger.bind(session_id=session_id, component="obs.trace")
        self._writer = BackgroundBatchWriter(self._flush, self._log)

    def start(self) -> None:
        self._writer.start()

    async def stop(self) -> None:
        await self._writer.stop()

    def record_trace(
        self,
        *,
        turn_id: int | None,
        step: int,
        model: str,
        purpose: str,
        finish_reason: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        ttfb_ms: int | None,
        duration_ms: int | None,
        input_messages: list[dict],
        output: str,
    ) -> None:
        self._writer.enqueue(
            LLMTrace(
                user_id=self._user_id,
                session_id=self._session_id,
                turn_id=turn_id,
                step=step,
                ts=datetime.now(timezone.utc),
                model=model,
                purpose=purpose,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                ttfb_ms=ttfb_ms,
                duration_ms=duration_ms,
                # serialized NOW — see the module docstring
                input_messages=json.dumps(
                    input_messages, ensure_ascii=False, default=str
                ),
                output=output,
            )
        )

    async def _flush(self, rows: list) -> None:
        async with user_scoped_session(self._user_id) as db:
            db.add_all(rows)
            await db.commit()
