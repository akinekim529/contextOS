"""In-process async worker plane.

The design's async plane drains a Redis Stream; this is the dependency-free, in-process version
with the same contract: enqueue work off the hot path, then a consumer drains it with explicit
ack and at-least-once retry. ``drain()`` runs everything queued and returns the count processed —
deterministic and testable. A production deployment swaps the queue for Redis Streams.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

Job = Callable[[], Awaitable[None]]


class BackgroundRunner:
    def __init__(self, *, max_retries: int = 1) -> None:
        self._queue: asyncio.Queue[tuple[Job, int]] = asyncio.Queue()
        self._max_retries = max_retries
        self._processed = 0
        self._failed = 0

    def enqueue(self, job: Job) -> None:
        self._queue.put_nowait((job, 0))

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def failed(self) -> int:
        return self._failed

    async def drain(self) -> int:
        """Run every currently-queued job to completion; retry failures up to ``max_retries``."""
        count = 0
        while not self._queue.empty():
            job, attempt = self._queue.get_nowait()
            try:
                await job()
                self._processed += 1
                count += 1
            except Exception:
                if attempt < self._max_retries:
                    self._queue.put_nowait((job, attempt + 1))  # at-least-once retry
                else:
                    self._failed += 1
            finally:
                self._queue.task_done()
        return count
