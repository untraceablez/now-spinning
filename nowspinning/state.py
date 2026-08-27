"""The single source of truth about what is on the turntable.

The engine writes; renderers only read. ``StateStore`` is deliberately usable from
both worlds at once: the pygame renderer polls ``snapshot()`` from the main thread
while the web renderer consumes an async subscription on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from nowspinning.recognize.base import Track

Status = Literal["idle", "listening", "identifying", "playing"]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Snapshots are cheap and self-contained, so a slow subscriber should drop stale
#: ones rather than stall the engine.
_QUEUE_MAXSIZE = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """An immutable description of the display's current subject."""

    status: Status = "idle"
    track: Track | None = None
    since: datetime = EPOCH
    updated_at: datetime = EPOCH
    artwork_path: Path | None = None
    consecutive_failures: int = 0
    level_dbfs: float = -120.0
    message: str | None = None

    @property
    def is_playing(self) -> bool:
        return self.track is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "track": self.track.to_dict() if self.track else None,
            "since": self.since.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "has_artwork": self.artwork_path is not None,
            "consecutive_failures": self.consecutive_failures,
            "level_dbfs": round(self.level_dbfs, 1),
            "message": self.message,
        }


@dataclass
class Subscription:
    """An async view of state changes, yielding the newest snapshot available."""

    queue: asyncio.Queue[NowPlaying | None] = field(
        default_factory=lambda: asyncio.Queue(_QUEUE_MAXSIZE)
    )
    _store: StateStore | None = None

    def __aiter__(self) -> AsyncIterator[NowPlaying]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[NowPlaying]:
        while True:
            item = await self.queue.get()
            if item is None:  # closed
                return
            yield item

    def close(self) -> None:
        if self._store is not None:
            self._store.unsubscribe(self)
            self._store = None


class StateStore:
    """Thread-safe holder of :class:`NowPlaying` with async fan-out to subscribers."""

    def __init__(
        self,
        initial: NowPlaying | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        started = clock()
        self._state = initial or NowPlaying(since=started, updated_at=started)
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, Subscription]] = []

    # -- reading ---------------------------------------------------------

    def snapshot(self) -> NowPlaying:
        with self._lock:
            return self._state

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot().to_dict()

    # -- writing ---------------------------------------------------------

    def update(self, **changes: Any) -> NowPlaying:
        """Apply field changes, stamping ``updated_at`` and notifying subscribers.

        ``since`` is refreshed automatically whenever the status or the track
        identity changes, so renderers can show "playing for N minutes" without
        the engine having to remember to set it.
        """
        with self._lock:
            previous = self._state
            candidate = replace(previous, **changes)
            # Compared before the timestamps are stamped, so a redundant write does
            # not wake every renderer with a state that says exactly the same thing.
            if candidate == previous:
                return previous
            now = self._clock()
            if "since" not in changes and self._is_new_subject(previous, candidate):
                candidate = replace(candidate, since=now)
            candidate = replace(candidate, updated_at=now)
            self._state = candidate
            listeners = list(self._subscribers)

        for loop, sub in listeners:
            self._deliver(loop, sub, candidate)
        return candidate

    @staticmethod
    def _is_new_subject(previous: NowPlaying, candidate: NowPlaying) -> bool:
        if previous.status != candidate.status:
            return True
        if candidate.track is None:
            return previous.track is not None
        return not candidate.track.is_same_recording(previous.track)

    # -- subscription ----------------------------------------------------

    def subscribe(self) -> Subscription:
        """Register for updates. Must be called from the consuming event loop."""
        loop = asyncio.get_running_loop()
        sub = Subscription()
        sub._store = self
        with self._lock:
            self._subscribers.append((loop, sub))
            current = self._state
        sub.queue.put_nowait(current)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subscribers = [(lp, s) for lp, s in self._subscribers if s is not sub]

    def close(self) -> None:
        """Signal every subscriber to finish iterating."""
        with self._lock:
            listeners = list(self._subscribers)
            self._subscribers = []
        for loop, sub in listeners:
            self._deliver(loop, sub, None)

    # -- internals -------------------------------------------------------

    @staticmethod
    def _deliver(
        loop: asyncio.AbstractEventLoop, sub: Subscription, item: NowPlaying | None
    ) -> None:
        def push() -> None:
            if sub.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):  # consumer got there first
                    sub.queue.get_nowait()  # drop the stale snapshot
            with contextlib.suppress(asyncio.QueueFull):  # consumer refilled it
                sub.queue.put_nowait(item)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            push()
        else:
            with contextlib.suppress(RuntimeError):  # the consumer's loop has closed
                loop.call_soon_threadsafe(push)
