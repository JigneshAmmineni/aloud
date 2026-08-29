"""Background batch writer — the NFR-10 discipline, extracted.

Hot-path code only ever enqueues (instant, in-memory); this task drains the
queue into the database in batches; a failed flush logs and DROPS the batch
rather than retrying into a live conversation. Shared by the transcript
writer (FR-20) and the usage recorder (FR-32/33).
"""

import asyncio

_STOP = object()

FLUSH_INTERVAL_S = 1.0
MAX_BATCH = 25


class BackgroundBatchWriter:
    """Drains an asyncio queue into `flush` (an async callable taking a list
    of items). `log` is a bound loguru logger carrying session_id."""

    def __init__(self, flush, log):
        self._flush = flush
        self._log = log
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def enqueue(self, item) -> None:
        self._queue.put_nowait(item)

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
        buffer: list = []
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
                try:
                    await self._flush(buffer)
                except Exception as e:
                    self._log.bind(
                        event="batch_writer.write_failed", rows=len(buffer)
                    ).error(f"dropping {len(buffer)} rows: {e}")
                buffer = []
