"""WAV encoding, including the normalization that makes quiet room audio matchable."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from nowspinning.audio.clip import normalize_peak, to_int16, write_wav
from tests.conftest import SAMPLE_RATE, music, silence


def test_normalize_brings_a_quiet_clip_up():
    quiet = music(amplitude=0.01)
    loud = normalize_peak(quiet)
    assert np.max(np.abs(loud)) == pytest.approx(10 ** (-1.0 / 20.0), abs=0.01)


def test_normalize_leaves_headroom_so_int16_never_clips():
    boosted = normalize_peak(music(amplitude=0.9))
    assert np.max(np.abs(boosted)) < 1.0
    assert np.abs(to_int16(boosted)).max() < 32767


def test_normalize_does_not_amplify_silence():
    """Boosting a silent frame would turn dither into a full-scale hiss."""
    assert np.max(np.abs(normalize_peak(silence()))) == 0.0


def test_to_int16_clips_out_of_range_input():
    out = to_int16(np.array([-4.0, 0.0, 4.0], dtype=np.float32))
    assert out.tolist() == [-32767, 0, 32767]


def test_write_wav_round_trips(tmp_path):
    samples = music(1.0)
    path = write_wav(samples, SAMPLE_RATE, tmp_path / "clips" / "out.wav")

    assert path.is_file()
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnframes() == samples.size
        decoded = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    assert np.corrcoef(decoded.astype(float), samples.astype(float))[0, 1] > 0.99


def test_write_wav_creates_missing_directories(tmp_path):
    path = write_wav(music(0.2), SAMPLE_RATE, tmp_path / "a" / "b" / "c.wav")
    assert path.is_file()


def test_write_wav_can_skip_normalization(tmp_path):
    path = write_wav(music(0.2, amplitude=0.05), SAMPLE_RATE, tmp_path / "raw.wav", normalize=False)
    with wave.open(str(path), "rb") as wav:
        decoded = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    assert np.abs(decoded).max() < 0.06 * 32767
