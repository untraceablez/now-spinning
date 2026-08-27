"""The listening loop and the policy that decides what the display should say.

The policy exists because vinyl is messy. A record has quiet passages, gaps
between tracks, and a two-minute pause while someone flips the side; the
fingerprinter misses matches on worn pressings and during fade-ins. So:

* nothing is sent anywhere until the gate says music is actually playing;
* a match buys a quiet period with no further lookups, which keeps request volume
  low and stops a single record from generating dozens of calls;
* a missed match never blanks the screen -- the last known track lingers, because
  showing a slightly stale title beats flickering between a title and nothing;
* silence has to persist before the display gives up, and even then the record
  lingers long enough to cover a side flip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from nowspinning.artwork import ArtworkCache
from nowspinning.audio.capture import AudioCapture
from nowspinning.audio.clip import write_wav
from nowspinning.audio.detect import GateEvent, MusicGate, analyze
from nowspinning.config import Config
from nowspinning.recognize.base import Recognizer, RecognizerError, Track
from nowspinning.state import StateStore

log = logging.getLogger(__name__)

#: Only republish the input level when it moves this much, so the SSE stream and
#: the pygame renderer are not woken up by meter noise several times a second.
LEVEL_PUBLISH_DELTA_DB = 1.5


def backoff_delay(failures: int, initial: float, maximum: float) -> float:
    """Exponential backoff after ``failures`` consecutive misses."""
    if failures <= 0:
        return 0.0
    return min(maximum, initial * (2.0 ** (failures - 1)))


class Engine:
    """Drives capture -> detection -> recognition -> state."""

    def __init__(
        self,
        config: Config,
        capture: AudioCapture,
        recognizer: Recognizer,
        store: StateStore,
        artwork: ArtworkCache,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.capture = capture
        self.recognizer = recognizer
        self.store = store
        self.artwork = artwork
        self.clock = clock

        self.gate = MusicGate(config.detect)
        self._clip_path = Path(config.cache_dir) / "clips" / "current.wav"

        self._next_identify = 0.0
        self._failures = 0
        self._last_confirmed_at: float | None = None
        self._linger_until: float | None = None
        self._published_level = -999.0
        self._identify_task: asyncio.Task[Track | None] | None = None
        self._artwork_task: asyncio.Task[Path | None] | None = None
        self._artwork_for: Track | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Poll the microphone until :meth:`stop` is called."""
        interval = self.config.detect.frame_seconds
        log.info("engine started (provider=%s)", getattr(self.recognizer, "name", "?"))
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # a bad frame must not kill the display
                    log.exception("engine tick failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=interval)
        finally:
            await self._shutdown()

    def stop(self) -> None:
        self._stopping.set()

    async def _shutdown(self) -> None:
        for task in (self._identify_task, self._artwork_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._identify_task = None
        self._artwork_task = None
        log.info("engine stopped")

    # -- one pass --------------------------------------------------------

    async def tick(self) -> None:
        now = self.clock()
        frame = self.capture.snapshot(self.config.detect.frame_seconds)
        stats = analyze(frame, self.capture.sample_rate)
        event = self.gate.observe_stats(stats, now)

        if event is GateEvent.MUSIC_STARTED:
            self._on_music_started(now)
        elif event is GateEvent.MUSIC_STOPPED:
            self._on_music_stopped(now)

        self._reap_identify(now)
        self._reap_artwork()

        if self.gate.is_open:
            await self._maybe_identify(now)
        else:
            self._maybe_end_linger(now)

        self._expire_stale_track(now)
        self._publish_level(stats.level_dbfs)

    # -- gate transitions ------------------------------------------------

    def _on_music_started(self, now: float) -> None:
        log.info("music detected (%.1f dBFS)", self.gate.last_stats.level_dbfs)
        self._linger_until = None
        self._failures = 0
        # The gate already watched start_seconds of music, so only the remainder of
        # a full clip still has to accumulate in the buffer.
        warmup = max(0.0, self.config.audio.clip_seconds - self.config.detect.start_seconds)
        self._next_identify = now + warmup
        if self.store.snapshot().track is None:
            self.store.update(status="listening", message=None)

    def _on_music_stopped(self, now: float) -> None:
        log.info("music stopped")
        state = self.store.snapshot()
        if state.track is None:
            self._clear(reason=None)
            return
        self._linger_until = now + self.config.recognizer.linger_seconds

    def _maybe_end_linger(self, now: float) -> None:
        if self._linger_until is not None and now >= self._linger_until:
            log.info("linger expired; clearing display")
            self._clear(reason=None)

    def _expire_stale_track(self, now: float) -> None:
        stale_after = self.config.recognizer.stale_after_seconds
        if stale_after <= 0 or self._last_confirmed_at is None:
            return
        if self.store.snapshot().track is None:
            return
        if now - self._last_confirmed_at < stale_after:
            return
        log.info("track not reconfirmed in %.0fs; clearing", stale_after)
        self._clear(reason="Lost the thread - still listening")

    def _clear(self, *, reason: str | None) -> None:
        self._linger_until = None
        self._last_confirmed_at = None
        self.store.update(
            status="listening" if self.gate.is_open else "idle",
            track=None,
            artwork_path=None,
            message=reason,
        )

    # -- recognition -----------------------------------------------------

    async def _maybe_identify(self, now: float) -> None:
        if self._identify_task is not None or now < self._next_identify:
            return
        if self.capture.available_seconds < self.config.audio.clip_seconds:
            return

        samples = self.capture.snapshot(self.config.audio.clip_seconds)
        if self.store.snapshot().track is None:
            self.store.update(status="identifying")
        self._identify_task = asyncio.create_task(self._identify(samples))

    async def _identify(self, samples: np.ndarray) -> Track | None:
        path = await asyncio.to_thread(
            write_wav, samples, self.capture.sample_rate, self._clip_path
        )
        return await self.recognizer.identify(path)

    def _reap_identify(self, now: float) -> None:
        task = self._identify_task
        if task is None or not task.done():
            return
        self._identify_task = None

        try:
            track = task.result()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            return
        except RecognizerError as exc:
            log.warning("recognition failed: %s", exc)
            self._on_no_match(now)
            return
        except Exception:
            log.exception("unexpected recognizer error")
            self._on_no_match(now)
            return

        if track is None:
            log.info("no match")
            self._on_no_match(now)
        else:
            self._on_match(track, now)

    def _on_match(self, track: Track, now: float) -> None:
        self._failures = 0
        self._last_confirmed_at = now
        previous = self.store.snapshot().track

        if track.is_same_recording(previous):
            self._next_identify = now + self.config.recognizer.recheck_interval_seconds
            log.debug("still playing %s", track.display_name)
            return

        log.info("now spinning: %s", track.display_name)
        self._next_identify = now + self.config.recognizer.quiet_period_seconds
        self.store.update(
            status="playing",
            track=track,
            artwork_path=self.artwork.lookup(track.artwork_url),
            consecutive_failures=0,
            message=None,
        )
        self._start_artwork_fetch(track)

    def _on_no_match(self, now: float) -> None:
        self._failures += 1
        delay = backoff_delay(
            self._failures,
            self.config.recognizer.backoff_initial_seconds,
            self.config.recognizer.backoff_max_seconds,
        )
        self._next_identify = now + delay
        state = self.store.snapshot()
        self.store.update(
            status="playing" if state.track is not None else "listening",
            consecutive_failures=self._failures,
        )

    # -- artwork ---------------------------------------------------------

    def _start_artwork_fetch(self, track: Track) -> None:
        if not track.artwork_url:
            return
        if self._artwork_task is not None and not self._artwork_task.done():
            self._artwork_task.cancel()
        self._artwork_for = track
        self._artwork_task = asyncio.create_task(self.artwork.fetch(track.artwork_url))

    def _reap_artwork(self) -> None:
        task = self._artwork_task
        if task is None or not task.done():
            return
        self._artwork_task = None
        try:
            path = task.result()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            return
        except Exception:  # artwork is best-effort; a blank label is an acceptable outcome
            log.debug("artwork fetch failed", exc_info=True)
            return
        if path is None:
            return
        current = self.store.snapshot().track
        wanted = self._artwork_for
        if current is not None and wanted is not None and current.is_same_recording(wanted):
            self.store.update(artwork_path=path)

    # -- meters ----------------------------------------------------------

    def _publish_level(self, level_dbfs: float) -> None:
        if abs(level_dbfs - self._published_level) < LEVEL_PUBLISH_DELTA_DB:
            return
        self._published_level = level_dbfs
        self.store.update(level_dbfs=level_dbfs)
