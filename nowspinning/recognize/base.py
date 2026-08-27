"""The provider-agnostic contract every recognizer implements."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class RecognizerError(RuntimeError):
    """A provider failed to answer. The engine treats this as 'no match, back off'."""


@dataclass(frozen=True, slots=True)
class Track:
    """One identified piece of music.

    ``album`` and ``artwork_url`` are frequently missing from real responses, so
    everything downstream has to render without them.
    """

    title: str
    artist: str
    album: str | None = None
    artwork_url: str | None = None
    provider_id: str | None = None
    provider: str = "unknown"
    matched_at: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def with_match_time(self, when: datetime) -> Track:
        return replace(self, matched_at=when)

    def is_same_recording(self, other: Track | None) -> bool:
        """Whether two matches refer to the same thing on the platter.

        Prefers the provider's stable id and falls back to a case-folded
        title/artist comparison, since not every provider returns an id.
        """
        if other is None:
            return False
        if self.provider_id and other.provider_id:
            return self.provider_id == other.provider_id
        return (
            self.title.casefold() == other.title.casefold()
            and self.artist.casefold() == other.artist.casefold()
        )

    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork_url": self.artwork_url,
            "provider_id": self.provider_id,
            "provider": self.provider,
            "matched_at": self.matched_at.isoformat(),
        }


@runtime_checkable
class Recognizer(Protocol):
    """Identify the music in a WAV file, or return None when nothing matches."""

    name: str

    async def identify(self, wav_path: Path) -> Track | None: ...

    async def aclose(self) -> None: ...
