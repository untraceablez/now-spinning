"""Shared fixtures. Nothing here touches the network or real audio hardware."""

from __future__ import annotations

import numpy as np
import pytest

from nowspinning.config import Config

SAMPLE_RATE = 16000


def music(
    seconds: float = 0.5, sample_rate: int = SAMPLE_RATE, amplitude: float = 0.3
) -> np.ndarray:
    """A chord with harmonics: loud, and spectrally peaky like real music."""
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    signal = np.zeros_like(t)
    for freq in (220.0, 277.2, 330.0, 440.0, 660.0):
        signal += np.sin(2 * np.pi * freq * t)
    signal /= np.max(np.abs(signal))
    return (signal * amplitude).astype(np.float32)


def silence(seconds: float = 0.5, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)


def room_noise(
    seconds: float = 0.5, sample_rate: int = SAMPLE_RATE, amplitude: float = 0.3
) -> np.ndarray:
    """Loud broadband noise -- a fan or an air handler, not music."""
    rng = np.random.default_rng(1959)
    return (rng.standard_normal(int(seconds * sample_rate)) * amplitude).astype(np.float32)


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def fast_config(tmp_path) -> Config:
    """Same policy, compressed timings, so engine tests do not wait on real seconds."""
    cfg = Config()
    cfg.cache_dir = tmp_path / "cache"
    cfg.audio.clip_seconds = 3.0
    cfg.detect.start_seconds = 0.0
    cfg.detect.silence_seconds = 2.0
    cfg.recognizer.provider = "fake"
    cfg.recognizer.quiet_period_seconds = 30.0
    cfg.recognizer.recheck_interval_seconds = 20.0
    cfg.recognizer.backoff_initial_seconds = 5.0
    cfg.recognizer.backoff_max_seconds = 40.0
    cfg.recognizer.linger_seconds = 10.0
    cfg.recognizer.stale_after_seconds = 120.0
    return cfg


class Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


class FakeCapture:
    """Stands in for AudioCapture, serving whichever signal the test selects."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, mode: str = "silence") -> None:
        self.sample_rate = sample_rate
        self.mode = mode

    @property
    def available_seconds(self) -> float:
        return 60.0

    def snapshot(self, seconds: float) -> np.ndarray:
        if self.mode == "music":
            return music(seconds, self.sample_rate)
        if self.mode == "noise":
            return room_noise(seconds, self.sample_rate)
        return silence(seconds, self.sample_rate)
