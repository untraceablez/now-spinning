"""The policy layer.

These are the rules that make the display usable next to a real turntable, so they
are worth pinning down: don't call out until music is playing, don't call again
straight away, and never blank the screen just because one lookup missed.
"""

from __future__ import annotations

import contextlib

import pytest

from nowspinning.artwork import ArtworkCache
from nowspinning.engine import Engine, backoff_delay
from nowspinning.recognize.base import Track
from nowspinning.recognize.fake import FakeRecognizer
from nowspinning.state import StateStore
from tests.conftest import Clock, FakeCapture

TRACK_A = Track(title="So What", artist="Miles Davis", provider_id="a", provider="fake")
TRACK_B = Track(
    title="Blue In Green",
    artist="Miles Davis",
    provider_id="b",
    provider="fake",
    artwork_url="https://images.example.invalid/cover.jpg",
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def capture() -> FakeCapture:
    return FakeCapture()


def make_engine(config, capture, recognizer, clock, tmp_path):
    store = StateStore()
    artwork = ArtworkCache(tmp_path / "art", session=_NoSession())
    return Engine(config, capture, recognizer, store, artwork, clock=clock), store


class _NoSession:
    """Any artwork fetch in these tests is a bug; make it loud rather than networked."""

    def get(self, url: str):
        raise AssertionError(f"unexpected artwork download: {url}")

    async def close(self) -> None:
        return None


async def step(engine, clock, seconds: float = 0.5) -> None:
    """Advance the clock, run one tick, and let any spawned lookup finish.

    A failing lookup is swallowed here on purpose: the engine reaps the task on the
    following tick, which is exactly the behaviour under test.
    """
    clock.advance(seconds)
    await engine.tick()
    task = engine._identify_task
    if task is not None:
        with contextlib.suppress(Exception):
            await task
        await engine.tick()  # the engine reaps on the following tick; no time passes


async def play_until_match(engine, clock, capture) -> None:
    capture.mode = "music"
    await step(engine, clock, 0.5)  # gate opens
    await step(engine, clock, 5.0)  # warmup elapses, lookup runs
    await step(engine, clock, 0.5)  # result is reaped


# -- the quiet cases -------------------------------------------------------


async def test_silence_never_reaches_the_recognizer(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    for _ in range(20):
        await step(engine, clock)

    assert recognizer.call_count == 0
    assert store.snapshot().status == "idle"


async def test_room_noise_never_reaches_the_recognizer(fast_config, capture, clock, tmp_path):
    """A fan is loud. It is not a record."""
    capture.mode = "noise"
    recognizer = FakeRecognizer([TRACK_A])
    engine, _ = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    for _ in range(20):
        await step(engine, clock)

    assert recognizer.call_count == 0


# -- the happy path --------------------------------------------------------


async def test_music_is_identified_and_published(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    await play_until_match(engine, clock, capture)

    state = store.snapshot()
    assert recognizer.call_count == 1
    assert state.status == "playing"
    assert state.track is not None and state.track.title == "So What"


async def test_a_clip_is_only_taken_once_the_buffer_has_enough(
    fast_config, capture, clock, tmp_path
):
    capture.mode = "music"
    recognizer = FakeRecognizer([TRACK_A])
    engine, _ = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    await step(engine, clock, 0.5)  # gate opens; warmup still running
    assert recognizer.call_count == 0
    await step(engine, clock, 1.0)
    assert recognizer.call_count == 0, "must wait out clip_seconds of music first"
    await step(engine, clock, 5.0)
    assert recognizer.call_count == 1


async def test_a_fresh_match_buys_a_quiet_period(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A])
    engine, _ = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)
    assert recognizer.call_count == 1

    for _ in range(20):  # 10 simulated seconds, well inside the 30s quiet period
        await step(engine, clock, 0.5)
    assert recognizer.call_count == 1, "one record should not generate a call per tick"

    await step(engine, clock, 25.0)
    assert recognizer.call_count == 2


async def test_the_same_track_again_does_not_restart_the_display(
    fast_config, capture, clock, tmp_path
):
    recognizer = FakeRecognizer([TRACK_A, TRACK_A])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)
    since = store.snapshot().since

    await step(engine, clock, 35.0)
    await step(engine, clock, 0.5)

    assert recognizer.call_count == 2
    assert store.snapshot().since == since


async def test_a_new_track_replaces_the_old_one(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A, TRACK_B], cycle=False)
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)
    assert store.snapshot().track.provider_id == "a"

    await step(engine, clock, 35.0)
    await step(engine, clock, 0.5)

    state = store.snapshot()
    assert state.track.provider_id == "b"
    assert state.status == "playing"


# -- when recognition misses ----------------------------------------------


async def test_a_miss_backs_off_instead_of_retrying_immediately(
    fast_config, capture, clock, tmp_path
):
    recognizer = FakeRecognizer([None])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    await play_until_match(engine, clock, capture)
    assert recognizer.call_count == 1
    assert store.snapshot().consecutive_failures == 1

    await step(engine, clock, 2.0)
    assert recognizer.call_count == 1, "backoff is 5s; a 2s step must not retry"

    await step(engine, clock, 4.0)
    assert recognizer.call_count == 2
    assert store.snapshot().consecutive_failures == 2


async def test_backoff_grows_and_is_capped(fast_config, capture, clock, tmp_path):
    assert backoff_delay(0, 10.0, 120.0) == 0.0
    assert backoff_delay(1, 10.0, 120.0) == 10.0
    assert backoff_delay(2, 10.0, 120.0) == 20.0
    assert backoff_delay(3, 10.0, 120.0) == 40.0
    assert backoff_delay(9, 10.0, 120.0) == 120.0


async def test_a_provider_failure_is_treated_as_a_miss(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([RuntimeError("service unavailable")])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)

    await play_until_match(engine, clock, capture)

    assert store.snapshot().consecutive_failures == 1
    assert store.snapshot().status == "listening"


async def test_a_miss_never_blanks_a_track_that_is_still_playing(
    fast_config, capture, clock, tmp_path
):
    """Worn pressings and long fades miss. Showing a stale title beats showing nothing."""
    recognizer = FakeRecognizer([TRACK_A, None, None, None], cycle=False)
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)

    for _ in range(2):  # two more lookups, still inside stale_after_seconds
        await step(engine, clock, 40.0)

    state = store.snapshot()
    assert state.track is not None and state.track.provider_id == "a"
    assert state.status == "playing"
    assert state.consecutive_failures >= 2


# -- when the music stops --------------------------------------------------


async def test_silence_lingers_before_the_display_clears(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)

    capture.mode = "silence"
    for _ in range(6):  # 3s of quiet, past the 2s gate threshold
        await step(engine, clock, 0.5)
    assert store.snapshot().track is not None, "a side flip must not clear the screen"

    await step(engine, clock, 12.0)  # past the 10s linger
    state = store.snapshot()
    assert state.track is None
    assert state.status == "idle"


async def test_music_returning_during_the_linger_keeps_the_track(
    fast_config, capture, clock, tmp_path
):
    recognizer = FakeRecognizer([TRACK_A])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)

    capture.mode = "silence"
    for _ in range(6):
        await step(engine, clock, 0.5)
    capture.mode = "music"
    await step(engine, clock, 0.5)
    await step(engine, clock, 20.0)

    assert store.snapshot().track is not None


async def test_silence_with_nothing_playing_goes_straight_to_idle(
    fast_config, capture, clock, tmp_path
):
    recognizer = FakeRecognizer([None])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)

    capture.mode = "silence"
    for _ in range(6):
        await step(engine, clock, 0.5)

    assert store.snapshot().status == "idle"


async def test_an_unconfirmed_track_eventually_goes_stale(fast_config, capture, clock, tmp_path):
    recognizer = FakeRecognizer([TRACK_A, None], cycle=False)
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    await play_until_match(engine, clock, capture)

    await step(engine, clock, 200.0)  # past stale_after_seconds

    state = store.snapshot()
    assert state.track is None
    assert state.status == "listening"
    assert state.message


# -- meters ----------------------------------------------------------------


async def test_the_level_meter_is_published_but_not_on_every_tick(
    fast_config, capture, clock, tmp_path
):
    recognizer = FakeRecognizer([None])
    engine, store = make_engine(fast_config, capture, recognizer, clock, tmp_path)
    capture.mode = "music"

    seen = []
    for _ in range(10):
        await step(engine, clock, 0.5)
        seen.append(store.snapshot().updated_at)

    assert store.snapshot().level_dbfs > -60.0
    assert len(set(seen)) < 10, "a steady level must not wake renderers every tick"
