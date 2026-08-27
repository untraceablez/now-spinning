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
from tests.conftest import SAMPLE_RATE as RATE
from tests.conftest import music, record_like, room_noise, silence

#: Every rate the capture layer will negotiate down to, end to end.
SUPPORTED_RATES = (8000, 11025, 16000, 20000, 22050, 32000, 44100, 48000, 88200, 96000)


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
    assert spectral_flatness(music(), RATE) < 0.2
    assert spectral_flatness(room_noise(), RATE) > 0.4


def test_flatness_is_bounded():
    for frame in (music(), room_noise(), silence()):
        assert 0.0 <= spectral_flatness(frame, RATE) <= 1.0


def test_short_frames_report_as_noise():
    assert spectral_flatness(np.ones(8, dtype=np.float32), RATE) == 1.0


def test_gate_opens_on_music_and_closes_on_silence():
    cfg = DetectConfig(start_seconds=1.0, silence_seconds=2.0)
    gate = MusicGate(cfg)

    assert gate.observe(music(), RATE, now=0.0) is GateEvent.NONE  # sustain not met yet
    assert not gate.is_open
    assert gate.observe(music(), RATE, now=1.0) is GateEvent.MUSIC_STARTED
    assert gate.is_open

    assert gate.observe(silence(), RATE, now=1.5) is GateEvent.NONE
    assert gate.observe(silence(), RATE, now=3.4) is GateEvent.NONE
    assert gate.observe(silence(), RATE, now=3.6) is GateEvent.MUSIC_STOPPED
    assert not gate.is_open


def test_gate_ignores_loud_room_noise():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    for step in range(20):
        assert gate.observe(room_noise(), RATE, now=float(step)) is GateEvent.NONE
    assert not gate.is_open


def test_gate_ignores_quiet_music():
    gate = MusicGate(DetectConfig(start_seconds=0.0, start_threshold_dbfs=-30.0))
    assert gate.observe(music(amplitude=0.005), RATE, now=0.0) is GateEvent.NONE
    assert not gate.is_open


def test_a_quiet_passage_does_not_close_the_gate():
    """Fade-outs and inter-track gaps must not read as 'the record ended'."""
    cfg = DetectConfig(start_seconds=0.0, silence_seconds=20.0)
    gate = MusicGate(cfg)
    assert gate.observe(music(), RATE, now=0.0) is GateEvent.MUSIC_STARTED

    for step in range(1, 12):  # 11 seconds of quiet, short of the 20s threshold
        assert gate.observe(silence(), RATE, now=float(step)) is GateEvent.NONE
    assert gate.is_open

    gate.observe(music(), RATE, now=12.0)  # music returns, timer resets
    for step in range(13, 25):
        assert gate.observe(silence(), RATE, now=float(step)) is GateEvent.NONE
    assert gate.is_open


def test_gate_opens_immediately_when_no_sustain_required():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    assert gate.observe(music(), RATE, now=100.0) is GateEvent.MUSIC_STARTED


def test_reset_closes_the_gate():
    gate = MusicGate(DetectConfig(start_seconds=0.0))
    gate.observe(music(), RATE, now=0.0)
    gate.reset()
    assert not gate.is_open


def test_analyze_reports_both_measurements():
    stats = analyze(music(), RATE)
    assert stats.level_dbfs > -30.0
    assert stats.flatness < 0.2
    assert stats.looks_like_music(DetectConfig())
    assert not stats.looks_like_silence(DetectConfig())


# -- behaviour across sample rates ----------------------------------------
#
# Microphones range from 16 kHz USB conference mics to 96 kHz audio interfaces,
# and the capture layer takes whatever the hardware offers. A single
# ``max_flatness`` setting therefore has to mean the same thing at every rate,
# which is why flatness is measured over a fixed band rather than the whole
# spectrum.


@pytest.mark.parametrize("rate", SUPPORTED_RATES)
def test_music_reads_as_music_at_every_rate(rate):
    stats = analyze(music(0.5, rate), rate)
    assert stats.looks_like_music(DetectConfig())
    assert not stats.looks_like_silence(DetectConfig())


@pytest.mark.parametrize("rate", SUPPORTED_RATES)
def test_a_record_with_surface_noise_reads_as_music_at_every_rate(rate):
    assert analyze(record_like(0.5, rate), rate).looks_like_music(DetectConfig())


@pytest.mark.parametrize("rate", SUPPORTED_RATES)
def test_room_noise_is_rejected_at_every_rate(rate):
    assert not analyze(room_noise(0.5, rate), rate).looks_like_music(DetectConfig())


@pytest.mark.parametrize("rate", SUPPORTED_RATES)
def test_silence_reads_as_silence_at_every_rate(rate):
    assert analyze(silence(0.5, rate), rate).looks_like_silence(DetectConfig())


@pytest.mark.parametrize("rate", SUPPORTED_RATES)
def test_the_gate_opens_and_closes_at_every_rate(rate):
    gate = MusicGate(DetectConfig(start_seconds=0.0, silence_seconds=1.0))
    assert gate.observe(music(0.5, rate), rate, now=0.0) is GateEvent.MUSIC_STARTED
    assert gate.observe(silence(0.5, rate), rate, now=0.5) is GateEvent.NONE
    assert gate.observe(silence(0.5, rate), rate, now=1.5) is GateEvent.MUSIC_STOPPED


def test_flatness_of_a_tone_is_the_same_at_every_rate():
    values = [spectral_flatness(music(0.5, rate), rate) for rate in SUPPORTED_RATES]
    assert max(values) - min(values) < 0.05


def test_flatness_of_noise_is_the_same_at_every_rate():
    values = [spectral_flatness(room_noise(0.5, rate), rate) for rate in SUPPORTED_RATES]
    assert max(values) - min(values) < 0.05
    assert min(values) > DetectConfig().max_flatness


def test_a_higher_rate_never_makes_a_record_look_less_musical():
    """The residual drift on mixed signals only ever errs toward 'this is music'.

    Broadband surface noise spread over a wider spectrum contributes less inside
    the fixed analysis band, so the same take reads as slightly *more* musical at
    96 kHz than at 16 kHz. That is the safe direction: thresholds tuned on a 16 kHz
    mic keep working on a 96 kHz one, and every rate stays far below the default.
    """
    baseline = spectral_flatness(record_like(0.5, 16000), 16000)
    for rate in SUPPORTED_RATES:
        value = spectral_flatness(record_like(0.5, rate), rate)
        assert value <= baseline + 0.005, f"{rate} Hz reads less musical than 16 kHz"
        assert value < DetectConfig().max_flatness / 2


def test_flatness_does_not_depend_on_how_loud_the_room_is():
    """Gain is the level gate's business; flatness must only see spectral shape."""
    values = [
        spectral_flatness(record_like(0.5, RATE, amplitude=a), RATE) for a in (0.01, 0.1, 0.9)
    ]
    assert max(values) - min(values) < 0.01


def test_the_band_ignores_content_a_record_cannot_contain():
    """A 96 kHz mic hears mostly empty spectrum above the music; it must not count."""
    rate = 96000
    signal = record_like(0.5, rate)
    rng = np.random.default_rng(7)
    spectrum = np.fft.rfft(rng.standard_normal(signal.size))
    spectrum[np.fft.rfftfreq(signal.size, 1 / rate) < 12000] = 0.0
    ultrasonic = np.fft.irfft(spectrum, n=signal.size)
    ultrasonic = ultrasonic / np.max(np.abs(ultrasonic)) * 0.3

    with_hash = (signal + ultrasonic).astype(np.float32)
    assert analyze(with_hash, rate).looks_like_music(DetectConfig())
    assert spectral_flatness(with_hash, rate) == pytest.approx(
        spectral_flatness(signal, rate), abs=0.02
    )


def test_the_band_is_clamped_at_nyquist_for_low_rate_devices():
    """An 8 kHz mic has no spectrum at the band's upper edge; it must still work."""
    assert spectral_flatness(music(0.5, 8000), 8000) < 0.2
    assert spectral_flatness(room_noise(0.5, 8000), 8000) > 0.4


@pytest.mark.parametrize("rate", [0, -1])
def test_a_nonsense_rate_is_treated_as_noise(rate):
    assert spectral_flatness(music(), rate) == 1.0
