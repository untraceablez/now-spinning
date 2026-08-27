"""State is the seam between the engine and every renderer, so it has to be strict."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from nowspinning.recognize.base import Track
from nowspinning.state import NowPlaying, StateStore

TRACK_A = Track(title="So What", artist="Miles Davis", provider_id="a", provider="test")
TRACK_B = Track(title="Freddie Freeloader", artist="Miles Davis", provider_id="b", provider="test")


def test_snapshot_is_immutable():
    store = StateStore()
    first = store.snapshot()
    store.update(status="listening")
    assert first.status == "idle"
    assert store.snapshot().status == "listening"


def test_update_stamps_updated_at():
    times = iter(
        [datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)]
    )
    store = StateStore(clock=lambda: next(times, datetime(2026, 1, 3, tzinfo=timezone.utc)))
    store.update(status="playing", track=TRACK_A)
    assert store.snapshot().updated_at.day == 2


def test_since_refreshes_on_a_new_track_only():
    store = StateStore()
    store.update(status="playing", track=TRACK_A)
    since = store.snapshot().since

    store.update(level_dbfs=-30.0)
    assert store.snapshot().since == since, "a meter update must not restart the clock"

    store.update(track=TRACK_B)
    assert store.snapshot().since > since


def test_identical_update_is_a_no_op():
    store = StateStore()
    store.update(status="listening")
    before = store.snapshot()
    after = store.update(status="listening")
    assert after is before


def test_to_dict_is_json_shaped():
    store = StateStore()
    store.update(status="playing", track=TRACK_A)
    payload = store.to_dict()
    assert payload["status"] == "playing"
    assert payload["track"]["title"] == "So What"
    assert payload["track"]["album"] is None
    assert payload["has_artwork"] is False


async def test_subscriber_receives_current_state_immediately():
    store = StateStore()
    store.update(status="listening")
    sub = store.subscribe()
    first = await asyncio.wait_for(sub.queue.get(), 1.0)
    assert first is not None and first.status == "listening"
    sub.close()


async def test_subscribers_all_see_updates():
    store = StateStore()
    subs = [store.subscribe() for _ in range(3)]
    for sub in subs:
        await sub.queue.get()  # drain the initial snapshot

    store.update(status="playing", track=TRACK_A)
    for sub in subs:
        state = await asyncio.wait_for(sub.queue.get(), 1.0)
        assert state is not None and state.track == TRACK_A
    for sub in subs:
        sub.close()


async def test_unsubscribed_consumer_stops_receiving():
    store = StateStore()
    sub = store.subscribe()
    await sub.queue.get()
    sub.close()
    store.update(status="playing", track=TRACK_A)
    assert sub.queue.empty()


async def test_slow_subscriber_drops_stale_snapshots_rather_than_blocking():
    store = StateStore()
    sub = store.subscribe()
    for i in range(50):
        store.update(level_dbfs=float(-i))
    assert sub.queue.qsize() <= 8
    newest = None
    while not sub.queue.empty():
        newest = sub.queue.get_nowait()
    assert newest is not None and newest.level_dbfs == -49.0
    sub.close()


async def test_close_terminates_iteration():
    store = StateStore()
    sub = store.subscribe()
    received = []

    async def consume():
        async for state in sub:
            received.append(state)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    store.close()
    await asyncio.wait_for(task, 1.0)
    assert received  # got the initial snapshot, then the stream ended


def test_updates_from_another_thread_reach_the_loop():
    """The pygame build runs the engine off-loop; cross-thread writes must still fan out."""
    import threading

    async def main():
        store = StateStore()
        sub = store.subscribe()
        await sub.queue.get()
        thread = threading.Thread(target=lambda: store.update(status="playing", track=TRACK_A))
        thread.start()
        thread.join()
        state = await asyncio.wait_for(sub.queue.get(), 2.0)
        assert state is not None and state.track == TRACK_A
        sub.close()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TRACK_A, TRACK_A, True),
        (TRACK_A, TRACK_B, False),
        (TRACK_A, None, False),
        (
            Track(title="So What", artist="miles davis", provider="x"),
            Track(title="SO WHAT", artist="Miles Davis", provider="y"),
            True,
        ),
    ],
)
def test_is_same_recording(left, right, expected):
    assert left.is_same_recording(right) is expected


def test_now_playing_defaults_are_idle():
    state = NowPlaying()
    assert state.status == "idle"
    assert not state.is_playing
