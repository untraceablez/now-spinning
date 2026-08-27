"""The web renderer's HTTP surface, including the live stream and the image route."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from nowspinning.artwork import ArtworkCache
from nowspinning.config import Config
from nowspinning.recognize.base import Track
from nowspinning.state import StateStore
from nowspinning.ui.web.app import _events, create_app, state_payload

TRACK = Track(
    title="Blue In Green",
    artist="Miles Davis",
    album="Kind of Blue",
    provider_id="40333609",
    provider="shazam",
)


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def artwork(tmp_path) -> ArtworkCache:
    return ArtworkCache(tmp_path)


@pytest.fixture
def client(store, artwork) -> TestClient:
    config = Config()
    config.cache_dir = artwork.dir.parent
    with TestClient(create_app(config, store, artwork)) as test_client:
        yield test_client


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "now spinning" in response.text
    assert "/static/app.js" in response.text


def test_static_assets_are_served(client):
    for path in ("/static/style.css", "/static/app.js"):
        assert client.get(path).status_code == 200


def test_now_playing_starts_idle(client):
    payload = client.get("/api/now-playing").json()
    assert payload["status"] == "idle"
    assert payload["track"] is None
    assert payload["artwork"] is None


def test_now_playing_reflects_the_store(client, store):
    store.update(status="playing", track=TRACK)
    payload = client.get("/api/now-playing").json()
    assert payload["status"] == "playing"
    assert payload["track"]["title"] == "Blue In Green"
    assert payload["track"]["album"] == "Kind of Blue"


def test_theme_exposes_the_configured_colours(client):
    payload = client.get("/api/theme").json()
    assert payload["background"] == "#101014"
    assert payload["accent"] == "#c8a24a"
    assert payload["rpm"] == pytest.approx(33.333)


def test_artwork_is_served_from_the_cache(client, store, artwork):
    artwork.dir.mkdir(parents=True, exist_ok=True)
    key = "a" * 64
    (artwork.dir / f"{key}.img").write_bytes(b"\xff\xd8\xffimage")

    response = client.get(f"/api/art/{key}")
    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xffimage"


def test_unknown_artwork_is_a_404(client):
    assert client.get(f"/api/art/{'b' * 64}").status_code == 404


def test_artwork_route_refuses_path_traversal(client):
    assert client.get("/api/art/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)


def test_state_payload_turns_a_cached_file_into_a_url(tmp_path):
    from nowspinning.state import NowPlaying

    path = tmp_path / ("c" * 64 + ".img")
    payload = state_payload(NowPlaying(status="playing", track=TRACK, artwork_path=path))
    assert payload["artwork"] == f"/api/art/{'c' * 64}"
    assert payload["has_artwork"] is True


def test_stream_route_is_registered(client):
    paths = {route.path for route in client.app.routes}
    assert "/api/stream" in paths


# The SSE generator is exercised directly rather than through TestClient: TestClient
# drives the app through a portal that waits for the response to finish, and an
# event stream by definition never does.


async def test_events_sends_the_current_state_immediately(store):
    store.update(status="playing", track=TRACK)
    events = _events(store)
    try:
        payload = json.loads((await asyncio.wait_for(anext(events), 1.0)).removeprefix("data: "))
    finally:
        await events.aclose()
    assert payload["status"] == "playing"
    assert payload["track"]["title"] == "Blue In Green"


async def test_events_pushes_later_updates(store):
    events = _events(store)
    try:
        first = json.loads((await asyncio.wait_for(anext(events), 1.0)).removeprefix("data: "))
        store.update(status="playing", track=TRACK)
        second = json.loads((await asyncio.wait_for(anext(events), 1.0)).removeprefix("data: "))
    finally:
        await events.aclose()
    assert first["track"] is None
    assert second["track"]["title"] == "Blue In Green"


async def test_events_emits_a_keepalive_while_nothing_happens(store, monkeypatch):
    """Idle turntables are the normal case, and proxies drop silent connections."""
    monkeypatch.setattr("nowspinning.ui.web.app.KEEPALIVE_SECONDS", 0.05)
    events = _events(store)
    try:
        await asyncio.wait_for(anext(events), 1.0)  # the initial snapshot
        assert (await asyncio.wait_for(anext(events), 1.0)).startswith(":")
    finally:
        await events.aclose()


async def test_events_ends_when_the_store_closes(store):
    events = _events(store)
    await asyncio.wait_for(anext(events), 1.0)
    store.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 1.0)


async def test_closing_the_stream_unsubscribes(store):
    """A browser tab closing must not leave a subscriber accumulating snapshots."""
    events = _events(store)
    await asyncio.wait_for(anext(events), 1.0)
    assert len(store._subscribers) == 1
    await events.aclose()
    assert store._subscribers == []
