"""Deciding whether music -- as opposed to silence or room noise -- is playing.

Pure NumPy and no I/O, so the whole decision layer is unit-testable against
synthetic signals. Two measurements drive it:

* **level** (dBFS) separates "something is happening" from a quiet room;
* **spectral flatness** separates music from broadband noise. A fan or an air
  handler produces an almost-flat spectrum (flatness near 1.0); music has strong
  peaks at partials, so its flatness is low.

Requiring both keeps the recognizer from firing every time the furnace kicks on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from nowspinning.config import DetectConfig

#: Floor for the dBFS conversion, so digital silence yields a finite number.
SILENCE_FLOOR_DBFS = -120.0

_EPS = 1e-12


def rms(frame: np.ndarray) -> float:
    """Root-mean-square amplitude of a float frame in [-1.0, 1.0]."""
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))


def dbfs(frame: np.ndarray) -> float:
    """Frame level in dB relative to full scale, floored at SILENCE_FLOOR_DBFS."""
    amplitude = rms(frame)
    if amplitude <= 0.0:
        return SILENCE_FLOOR_DBFS
    return max(SILENCE_FLOOR_DBFS, 20.0 * float(np.log10(amplitude)))


def spectral_flatness(frame: np.ndarray) -> float:
    """Ratio of geometric to arithmetic mean of the power spectrum, in [0.0, 1.0].

    Near 1.0 for white noise, near 0.0 for a pure tone. DC is excluded because a
    microphone's DC offset would otherwise dominate a quiet frame.
    """
    if frame.size < 32:
        return 1.0
    windowed = frame.astype(np.float64) * np.hanning(frame.size)
    power = np.abs(np.fft.rfft(windowed)) ** 2
    power = power[1:]  # drop DC
    if power.size == 0:
        return 1.0
    power = power + _EPS
    geometric = float(np.exp(np.mean(np.log(power))))
    arithmetic = float(np.mean(power))
    if arithmetic <= 0.0:
        return 1.0
    return float(min(1.0, max(0.0, geometric / arithmetic)))


@dataclass(frozen=True, slots=True)
class FrameStats:
    """What a single analysis frame looked like. Surfaced by ``now-spinning calibrate``."""

    level_dbfs: float
    flatness: float

    def looks_like_music(self, config: DetectConfig) -> bool:
        return (
            self.level_dbfs >= config.start_threshold_dbfs and self.flatness <= config.max_flatness
        )

    def looks_like_silence(self, config: DetectConfig) -> bool:
        return self.level_dbfs < config.silence_threshold_dbfs


def analyze(frame: np.ndarray) -> FrameStats:
    return FrameStats(level_dbfs=dbfs(frame), flatness=spectral_flatness(frame))


class GateEvent(Enum):
    """What, if anything, changed as a result of the latest frame."""

    NONE = "none"
    MUSIC_STARTED = "music_started"
    MUSIC_STOPPED = "music_stopped"


class MusicGate:
    """Hysteresis around :func:`analyze`.

    Opening requires ``start_seconds`` of sustained music and closing requires
    ``silence_seconds`` of sustained quiet, with a lower threshold for closing than
    for opening. Without that asymmetry the gate flaps through every quiet passage
    and every inter-track gap, which downstream reads as the record ending.
    """

    def __init__(self, config: DetectConfig) -> None:
        self.config = config
        self._open = False
        self._music_since: float | None = None
        self._quiet_since: float | None = None
        self._last: FrameStats = FrameStats(SILENCE_FLOOR_DBFS, 1.0)

    @property
    def is_open(self) -> bool:
        """True while the gate considers music to be playing."""
        return self._open

    @property
    def last_stats(self) -> FrameStats:
        return self._last

    def reset(self) -> None:
        self._open = False
        self._music_since = None
        self._quiet_since = None

    def observe(self, frame: np.ndarray, now: float) -> GateEvent:
        """Feed one analysis frame captured at monotonic time ``now``."""
        return self.observe_stats(analyze(frame), now)

    def observe_stats(self, stats: FrameStats, now: float) -> GateEvent:
        """Feed pre-computed stats -- lets the engine analyze once and reuse."""
        self._last = stats
        config = self.config

        if not self._open:
            self._quiet_since = None
            if stats.looks_like_music(config):
                if self._music_since is None:
                    self._music_since = now
                if now - self._music_since >= config.start_seconds:
                    self._open = True
                    self._music_since = None
                    return GateEvent.MUSIC_STARTED
            else:
                self._music_since = None
            return GateEvent.NONE

        self._music_since = None
        if stats.looks_like_silence(config):
            if self._quiet_since is None:
                self._quiet_since = now
            if now - self._quiet_since >= config.silence_seconds:
                self._open = False
                self._quiet_since = None
                return GateEvent.MUSIC_STOPPED
        else:
            self._quiet_since = None
        return GateEvent.NONE
