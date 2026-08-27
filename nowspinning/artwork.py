"""On-disk cache for cover art.

Downloads never block rendering: the engine publishes the track first and fills in
the artwork path afterwards, so a slow or failed image fetch costs nothing more
than a plain record label on screen. Cached files survive restarts, which matters
on a device that plays the same handful of records over and over.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Cover art is a few hundred KB at most; refuse anything that clearly is not an image.
MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 200


def cache_key(url: str) -> str:
    """Stable filename-safe key for a cover-art URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class ArtworkCache:
    """Fetch-once, reuse-forever storage for cover images."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        timeout: float = 15.0,
        session: Any = None,
    ) -> None:
        self.dir = Path(cache_dir) / "artwork"
        self.max_entries = max_entries
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None

    def path_for(self, url: str) -> Path:
        return self.dir / f"{cache_key(url)}.img"

    def path_for_key(self, key: str) -> Path | None:
        """Resolve a cache key back to a file, for the web UI's image route."""
        if not key.isalnum() or len(key) != 64:
            return None
        candidate = self.dir / f"{key}.img"
        return candidate if candidate.is_file() else None

    def lookup(self, url: str | None) -> Path | None:
        """Return the cached file for ``url`` without hitting the network."""
        if not url:
            return None
        path = self.path_for(url)
        return path if path.is_file() else None

    async def fetch(self, url: str | None) -> Path | None:
        """Return a local path for ``url``, downloading it if necessary.

        Returns None on any failure -- callers fall back to a blank record label.
        """
        if not url:
            return None
        cached = self.lookup(url)
        if cached is not None:
            return cached

        try:
            data = await self._download(url)
        except Exception as exc:
            log.warning("could not fetch cover art %s: %s", url, exc)
            return None
        if not data:
            return None

        path = self.path_for(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic, so a reader never sees a half-written image
        self.prune()
        return path

    async def _download(self, url: str) -> bytes | None:
        session = await self._get_session()
        async with session.get(url) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                log.warning("cover art %s returned HTTP %s", url, status)
                return None
            data: bytes = await response.read()
        if len(data) > MAX_BYTES:
            log.warning("cover art %s is %d bytes; ignoring", url, len(data))
            return None
        return data

    async def _get_session(self) -> Any:
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    def prune(self) -> None:
        """Drop the least recently modified files once the cache grows too large."""
        if not self.dir.is_dir():
            return
        files = sorted(self.dir.glob("*.img"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[self.max_entries :]:
            with contextlib.suppress(OSError):  # racing another prune is harmless
                stale.unlink()

    async def aclose(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None
