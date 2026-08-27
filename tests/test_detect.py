"""The gate has to fire on music and stay shut for noise -- everything else follows."""

from __future__ import annotations

import numpy as np
import pytest

from nowspinning.audio.detect import (
    SILENCE_FLOOR_DBFS,
    GateEvent,
    MusicGate,
    analyze,
    dbfs,
    rms,
    spectral_flatness,
)
from nowspinning.config import DetectConfig
from tests.conftest import music, room_noise, silence


def test_rms_of_silence_is_zero():
    assert rms(silence()) == 0.0


def test_rms_of_full_scale_sine_is_about_root_half():
    t = np.arange(16000) / 16000
    assert rms(np.sin(2 * np.pi * 440 * t)) == pytest.approx(0.7071, abs=0.01)


def test_dbfs_floors_on_digital_silence():
    assert dbfs(silence()) == SILENCE_FLOOR_DBFS
    assert dbfs(np.zeros(0, dtype=np.float32)) == SILENCE_FLOOR_DBFS


def test_dbfs_tracks_amplitude():
    quiet = dbfs(music(amplitude=0.01))
    loud = dbfs(music(amplitude=0.5))
    assert quiet < loud < 0.0
    # Halving amplitude is -6 dB; a 50x ratio should be roughly 34 dB.
    assert loud - quiet == pytest.approx(34.0, abs=2.0)


def test_flatness_separates_tones_from_noise():
    assert spectral_flatness(music()) < 0.2
    assert spectral_flatness(room_noise()) > 0.4


def test_flatness_is_bounded():
    for frame in (music(), room_noise(), silence()):
        assert 0.0 <= spectral_flatness(frame) <= 1.0


def test_short_frames_report_as_noise():
    assert spectral_flatness(np.ones(8, dtype=np.float32)) == 1.0


def test_gate_opens_on_music_and_closes_on_silence():
    cfg = DetectConfig(start_seconds=1.0, silence_seconds=2.0)
    gate = MusicGate(cfg)

    assert gate.observe(music(), now=0.0) is GateEvent.NONE  # sustain not met yet
    assert not gate.is_open
    assert gate.observe(music(), now=1.0) is GateEvent.MUSIC_STARTED
    assert gate.is_open

    assert gate.observe(silence(), now=1.5) is GateEvent.NONE
    assert gate.observe(silence(), now=3.4) is GateEvent.NONE
    assert gate.observe(silence(), now=3.6) is GateEvent.MUSIC_STOPPED
    assert not gate.is_open


def test_gate_ignores_loud_room_noise():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    for step in range(20):
        assert gate.observe(room_noise(), now=float(step)) is GateEvent.NONE
    assert not gate.is_open


def test_gate_ignores_quiet_music():
    gate = MusicGate(DetectConfig(start_seconds=0.0, start_threshold_dbfs=-30.0))
    assert gate.observe(music(amplitude=0.005), now=0.0) is GateEvent.NONE
    assert not gate.is_open


def test_a_quiet_passage_does_not_close_the_gate():
    """Fade-outs and inter-track gaps must not read as 'the record ended'."""
    cfg = DetectConfig(start_seconds=0.0, silence_seconds=20.0)
    gate = MusicGate(cfg)
    assert gate.observe(music(), now=0.0) is GateEvent.MUSIC_STARTED

    for step in range(1, 12):  # 11 seconds of quiet, short of the 20s threshold
        assert gate.observe(silence(), now=float(step)) is GateEvent.NONE
    assert gate.is_open

    gate.observe(music(), now=12.0)  # music returns, timer resets
    for step in range(13, 25):
        assert gate.observe(silence(), now=float(step)) is GateEvent.NONE
    assert gate.is_open


def test_gate_opens_immediately_when_no_sustain_required():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    assert gate.observe(music(), now=100.0) is GateEvent.MUSIC_STARTED


def test_reset_closes_the_gate():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    gate.observe(music(), now=0.0)
    gate.reset()
    assert not gate.is_open


def test_analyze_reports_both_measurements():
    stats = analyze(music())
    assert stats.level_dbfs > -30.0
    assert stats.flatness < 0.2
    assert stats.looks_like_music(DetectConfig())
    assert not stats.looks_like_silence(DetectConfig())
