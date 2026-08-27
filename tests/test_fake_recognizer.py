"""The scripted recognizer drives every engine branch, so it has to behave exactly."""

from __future__ import annotations

from pathlib import Path

import pytest

from nowspinning.recognize import build_recognizer
from nowspinning.recognize.base import Recognizer, RecognizerError, Track
from nowspinning.recognize.fake import FakeRecognizer, demo_tracks

CLIP = Path("/tmp/clip.wav")
TRACK = Track(title="So What", artist="Miles Davis", provider="fake", provider_id="a")


async def test_returns_scripted_results_in_order():
    recognizer = FakeRecognizer([TRACK, None], cycle=False)
    assert (await recognizer.identify(CLIP)).title == "So What"
    assert await recognizer.identify(CLIP) is None


async def test_cycles_when_asked_to():
    recognizer = FakeRecognizer([TRACK], cycle=True)
    for _ in range(3):
        assert await recognizer.identify(CLIP) is not None


async def test_stops_matching_once_a_finite_script_runs_out():
    recognizer = FakeRecognizer([TRACK], cycle=False)
    await recognizer.identify(CLIP)
    assert await recognizer.identify(CLIP) is None


async def test_scripted_exceptions_surface_as_recognizer_errors():
    recognizer = FakeRecognizer([RuntimeError("upstream is down")])
    with pytest.raises(RecognizerError, match="upstream is down"):
        await recognizer.identify(CLIP)


async def test_calls_are_recorded():
    recognizer = FakeRecognizer([None])
    await recognizer.identify(CLIP)
    assert recognizer.calls == [CLIP]
    assert recognizer.call_count == 1


async def test_matches_are_stamped_with_a_match_time():
    recognizer = FakeRecognizer([TRACK])
    assert (await recognizer.identify(CLIP)).matched_at.year > 2000


async def test_aclose_is_recorded():
    recognizer = FakeRecognizer()
    await recognizer.aclose()
    assert recognizer.closed


def test_demo_tracks_are_obviously_placeholders():
    """A demo run must never be mistaken for a real match."""
    for track in demo_tracks():
        assert track.provider == "fake"
        assert "Demo" in track.artist


def test_fake_satisfies_the_recognizer_protocol():
    assert isinstance(FakeRecognizer(), Recognizer)


def test_build_recognizer_dispatches_on_the_config(config):
    config.recognizer.provider = "fake"
    assert isinstance(build_recognizer(config), FakeRecognizer)


def test_build_recognizer_rejects_an_unknown_provider(config):
    config.recognizer.provider = "gramophone"
    with pytest.raises(ValueError, match="gramophone"):
        build_recognizer(config)
