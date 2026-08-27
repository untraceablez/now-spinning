"""A scripted recognizer for tests and for ``--demo`` runs with no microphone."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from nowspinning.recognize.base import RecognizerError, Track
from nowspinning.state import utcnow

PROVIDER = "fake"


def demo_tracks() -> list[Track]:
    """Obvious placeholders -- never mistake a demo run for a real match."""
    return [
        Track(
            title="Side One, Track One",
            artist="The Demo Pressing",
            album="A Record That Does Not Exist",
            provider=PROVIDER,
            provider_id="demo-1",
        ),
        Track(
            title="Second Groove",
            artist="The Demo Pressing",
            album="A Record That Does Not Exist",
            provider=PROVIDER,
            provider_id="demo-2",
        ),
    ]


class FakeRecognizer:
    """Returns a scripted sequence of results, cycling once it runs out.

    Sequence items may be a :class:`Track` (a match), ``None`` (no match), or an
    exception instance (a provider failure), which is enough to drive every branch
    of the engine's policy.
    """

    name = PROVIDER

    def __init__(
        self,
        script: Sequence[Track | BaseException | None] | None = None,
        *,
        delay: float = 0.0,
        cycle: bool = True,
    ) -> None:
        self.script: list[Track | BaseException | None] = list(script or [None])
        self.delay = delay
        self.cycle = cycle
        self.calls: list[Path] = []
        self.closed = False
        self._index = 0

    @classmethod
    def demo(cls) -> FakeRecognizer:
        return cls(demo_tracks(), cycle=True)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def identify(self, wav_path: Path) -> Track | None:
        self.calls.append(wav_path)
        if self.delay:
            await asyncio.sleep(self.delay)

        if self._index >= len(self.script):
            if not self.cycle:
                return None
            self._index = 0
        result = self.script[self._index]
        self._index += 1

        if isinstance(result, BaseException):
            raise RecognizerError(str(result) or "scripted failure") from result
        if result is None:
            return None
        return result.with_match_time(utcnow())

    async def aclose(self) -> None:
        self.closed = True
