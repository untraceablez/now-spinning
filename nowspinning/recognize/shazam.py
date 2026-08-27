"""Recognition through Shazam, via the unofficial `shazamio` client.

Response parsing is kept as a pure function so it can be tested against recorded
fixtures with no network access -- which matters, because this is the part most
likely to need adjusting if the upstream response shape ever moves.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nowspinning.recognize.base import RecognizerError, Track
from nowspinning.state import utcnow

log = logging.getLogger(__name__)

PROVIDER = "shazam"


def _find_album(sections: Any) -> str | None:
    """Pull the album name out of the response's metadata sections.

    Shazam returns metadata as a list of ``{"title": ..., "text": ...}`` pairs
    inside the SONG section, and plenty of matches carry no album at all.
    """
    if not isinstance(sections, list):
        return None
    for section in sections:
        if not isinstance(section, dict):
            continue
        for item in section.get("metadata") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("title", "")).strip().casefold() == "album":
                text = str(item.get("text", "")).strip()
                if text:
                    return text
    return None


def _artwork_url(images: Any) -> str | None:
    if not isinstance(images, dict):
        return None
    for key in ("coverarthq", "coverart", "background"):
        url = images.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def parse_response(data: Any) -> Track | None:
    """Convert a raw shazamio response into a :class:`Track`, or None for no match."""
    if not isinstance(data, dict):
        return None
    raw_track = data.get("track")
    if not isinstance(raw_track, dict):
        return None

    title = str(raw_track.get("title") or "").strip()
    if not title:
        return None
    artist = str(raw_track.get("subtitle") or "").strip() or "Unknown artist"

    return Track(
        title=title,
        artist=artist,
        album=_find_album(raw_track.get("sections")),
        artwork_url=_artwork_url(raw_track.get("images")),
        provider_id=str(raw_track["key"]) if raw_track.get("key") else None,
        provider=PROVIDER,
        matched_at=utcnow(),
    )


class ShazamRecognizer:
    """Identify a WAV clip using Shazam's fingerprinting service."""

    name = PROVIDER

    def __init__(self, timeout: float = 30.0, language: str = "en-US") -> None:
        self.timeout = timeout
        self.language = language
        self._client: Any = None

    def _shazam(self) -> Any:
        if self._client is None:
            try:
                from shazamio import Shazam
            except ImportError as exc:  # pragma: no cover - packaging guard
                raise RecognizerError(
                    "shazamio is not installed; run 'pip install shazamio'"
                ) from exc
            self._client = Shazam(language=self.language)
        return self._client

    async def identify(self, wav_path: Path) -> Track | None:
        client = self._shazam()
        try:
            data = await asyncio.wait_for(client.recognize(str(wav_path)), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise RecognizerError(f"shazam timed out after {self.timeout:.0f}s") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RecognizerError(f"shazam lookup failed: {exc}") from exc

        track = parse_response(data)
        if track is None:
            log.debug("no shazam match for %s", wav_path.name)
        return track

    async def aclose(self) -> None:
        self._client = None
