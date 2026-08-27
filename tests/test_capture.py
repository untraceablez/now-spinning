"""Ring-buffer behaviour, exercised through write() so no sound card is involved."""

from __future__ import annotations

import numpy as np
import pytest

from nowspinning.audio.capture import AudioCapture, AudioError, DeviceInfo, resolve_device
from nowspinning.config import AudioConfig

# The smallest config the validator allows: a 3 second buffer at 8 kHz, so the
# ring holds exactly 24000 samples and every expectation below is plain arithmetic.
RATE = 8000
CAPACITY = 24000


@pytest.fixture
def capture() -> AudioCapture:
    return AudioCapture(AudioConfig(sample_rate=RATE, buffer_seconds=3.0, clip_seconds=3.0))


def ramp(start: int, count: int) -> np.ndarray:
    return np.arange(start, start + count, dtype=np.float32)


def test_empty_buffer_returns_nothing(capture):
    assert capture.snapshot(0.5).size == 0
    assert capture.available_seconds == 0.0


def test_snapshot_returns_the_most_recent_audio(capture):
    capture.write(ramp(0, 8000))
    out = capture.snapshot(0.5)
    assert out.tolist() == list(range(4000, 8000))


def test_snapshot_is_clamped_to_what_has_arrived(capture):
    capture.write(ramp(0, 100))
    assert capture.snapshot(3.0).size == 100


def test_buffer_wraps_and_keeps_the_newest_samples(capture):
    capture.write(ramp(0, 20000))
    capture.write(ramp(20000, 20000))  # 40000 samples through a 24000 sample ring
    out = capture.snapshot(3.0)
    assert out.size == CAPACITY
    assert out.tolist() == list(range(40000 - CAPACITY, 40000))


def test_a_single_oversized_write_keeps_only_the_tail(capture):
    capture.write(ramp(0, 100000))
    assert capture.snapshot(3.0).tolist() == list(range(100000 - CAPACITY, 100000))


def test_snapshot_copies_so_later_writes_do_not_mutate_it(capture):
    capture.write(ramp(0, CAPACITY))
    snap = capture.snapshot(3.0)
    capture.write(ramp(900000, CAPACITY))
    assert snap.tolist() == list(range(0, CAPACITY))


def test_available_seconds_tracks_fill(capture):
    capture.write(np.zeros(4000, dtype=np.float32))
    assert capture.available_seconds == pytest.approx(0.5)
    capture.write(np.zeros(CAPACITY, dtype=np.float32))
    assert capture.available_seconds == pytest.approx(3.0)


def test_empty_write_is_ignored(capture):
    capture.write(np.zeros(0, dtype=np.float32))
    assert capture.available_seconds == 0.0


def test_stereo_callback_input_is_folded_to_mono(capture):
    stereo = np.stack([ramp(0, 10), ramp(100, 10)], axis=1)
    capture._callback(stereo, 10, None, None)
    assert capture.snapshot(3.0).tolist() == list(range(0, 10))


def test_resolve_device_passes_through_indexes():
    assert resolve_device(3) == 3
    assert resolve_device(None) is None
    assert resolve_device("2") == 2


def test_resolve_device_matches_a_name_substring(monkeypatch):
    devices = [
        DeviceInfo(0, "bcm2835 Headphones", 0, 44100.0),
        DeviceInfo(1, "USB Audio Device", 1, 48000.0),
    ]
    monkeypatch.setattr("nowspinning.audio.capture.list_input_devices", lambda: devices)
    assert resolve_device("usb audio") == 1


def test_resolve_device_reports_what_was_available(monkeypatch):
    monkeypatch.setattr(
        "nowspinning.audio.capture.list_input_devices",
        lambda: [DeviceInfo(0, "Built-in Mic", 1, 44100.0)],
    )
    with pytest.raises(AudioError, match="Built-in Mic"):
        resolve_device("Blue Yeti")


def test_device_description_is_human_readable():
    info = DeviceInfo(2, "USB Audio", 2, 48000.0, is_default=True)
    assert info.describe() == "[2] USB Audio (default) - 2 in @ 48000 Hz"
