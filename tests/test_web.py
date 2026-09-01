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


class TestThemeCarriesTheWholeDisplay:
    """The page lays itself out from this, so it has to carry everything."""

    def test_the_whole_display_config_is_sent(self, client):
        # A hand-picked subset would silently omit any setting added later.
        payload = client.get("/api/theme").json()
        from nowspinning.config import Config

        assert payload["display"] == Config().display.model_dump(mode="json")

    def test_every_font_role_is_sent(self, client):
        fonts = client.get("/api/theme").json()["fonts"]
        assert set(fonts) == {"heading", "title", "artist", "album"}
        assert fonts["title"]["italic"] is True
        assert fonts["title"]["weight"] == 700

    def test_the_geometry_is_sent(self, client):
        geometry = client.get("/api/theme").json()["geometry"]
        for key in ("image_size", "art_window", "disc_centre", "disc_radius", "sleeve_right"):
            assert key in geometry

    def test_the_original_flat_keys_still_work(self, client):
        payload = client.get("/api/theme").json()
        assert payload["background"] and payload["accent"] and payload["rpm"]


class TestAssetRoute:
    def test_the_sleeve_is_served(self, client):
        response = client.get("/api/asset/sleeve.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_the_placeholder_is_served(self, client):
        assert client.get("/api/asset/sleeve-noart.png").status_code == 200

    @pytest.mark.parametrize(
        "name", ["../../config.py", "..%2F..%2Fconfig.py", "nope.png", "sleeve.txt"]
    )
    def test_it_refuses_anything_else(self, client, name):
        assert client.get(f"/api/asset/{name}").status_code in (404, 400)


class TestFontRoute:
    def test_an_unknown_role_is_a_404(self, client):
        assert client.get("/api/font/subtitle").status_code == 404

    def test_an_unresolvable_font_is_a_404(self, store, tmp_path):
        # The page falls back to its own stack; it must not be a 500. Pinned to
        # the built-in font so this does not depend on what happens to be cached.
        from nowspinning.ui.web.app import create_app

        config = Config()
        config.fonts.source = "builtin"
        config.cache_dir = tmp_path
        with TestClient(create_app(config, store)) as local:
            assert local.get("/api/font/title").status_code == 404

    def test_a_resolvable_font_is_served(self, tmp_path, store):
        from nowspinning.config import Config
        from nowspinning.ui.web.app import create_app

        family = tmp_path / "fonts"
        family.mkdir()
        (family / "Bitter-BoldItalic.ttf").write_bytes(b"TTF")
        config = Config()
        config.fonts.source = "local"
        config.fonts.directory = family
        config.cache_dir = tmp_path
        with TestClient(create_app(config, store)) as local:
            response = local.get("/api/font/title")
        assert response.status_code == 200
        assert response.content == b"TTF"


class TestGeometryIsShared:
    """One source of truth for the composition, or the two renderers drift."""

    def test_the_renderer_uses_the_same_constants(self):
        from nowspinning.ui import geometry
        from nowspinning.ui import pygame_display as pd

        for name in ("ART_WINDOW", "DISC_CENTRE", "DISC_RADIUS", "SLEEVE_RIGHT", "DISC_EDGE"):
            assert getattr(pd, name) == getattr(geometry, name), name

    def test_the_payload_is_self_consistent(self):
        from nowspinning.ui import geometry

        data = geometry.as_dict()
        assert data["cover_left"] == data["art_window"][0]
        assert data["cover_top"] == data["art_window"][1]
        # The disc reaches past the jacket, or there would be no crescent to see.
        assert data["disc_edge"] > data["sleeve_right"]
        # The cover overlaps where the record starts, which is why it is clipped.
        assert data["cover_right"] > data["sleeve_right"]

    def test_geometry_does_not_need_pygame(self):
        # The web-only install has no SDL; importing it must not reach for one.
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import nowspinning.ui.geometry;"
                " sys.exit(1 if 'pygame' in sys.modules else 0)",
            ],
            capture_output=True,
        )
        assert result.returncode == 0, "importing geometry pulled in pygame"
