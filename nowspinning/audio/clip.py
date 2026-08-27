"""Turning a slice of the capture buffer into a WAV file the recognizer can read."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

#: Leave a little headroom so int16 conversion never clips on the loudest sample.
DEFAULT_HEADROOM_DB = 1.0

_MIN_PEAK = 1e-6


def normalize_peak(samples: np.ndarray, headroom_db: float = DEFAULT_HEADROOM_DB) -> np.ndarray:
    """Scale a clip so its loudest sample sits just under full scale.

    Room recordings of a turntable are usually far below 0 dBFS, and fingerprinting
    is noticeably more reliable on a normalized clip. Frames that are essentially
    silent are returned untouched rather than amplified into noise.
    """
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < _MIN_PEAK:
        return samples.astype(np.float32, copy=False)
    target = 10.0 ** (-abs(headroom_db) / 20.0)
    return (samples * (target / peak)).astype(np.float32, copy=False)


def to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float samples in [-1.0, 1.0] to clipped 16-bit PCM."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def write_wav(
    samples: np.ndarray,
    sample_rate: int,
    path: Path,
    *,
    normalize: bool = True,
) -> Path:
    """Write mono float samples to a 16-bit PCM WAV file and return its path."""
    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    if normalize:
        mono = normalize_peak(mono)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(to_int16(mono).tobytes())
    return path
