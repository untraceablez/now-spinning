"""Cover art is best-effort: it must never raise into the engine, only return None."""

from __future__ import annotations

import pytest

from nowspinning.artwork import MAX_BYTES, ArtworkCache, cache_key

URL = "https://images.example.invalid/800x800cc.jpg"
OTHER = "https://images.example.invalid/other.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"pretend jpeg" * 4


class StubResponse:
    def __init__(self, body: bytes = JPEG, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def read(self) -> bytes:
        return self.body

    async def __aenter__(self) -> StubResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class StubSession:
    """Enough of aiohttp.ClientSession for the cache, and it counts requests."""

    def __init__(self, response: StubResponse | Exception | None = None) -> None:
        self.response = response if response is not None else StubResponse()
        self.requests: list[str] = []
        self.closed = False

    def get(self, url: str):
        self.requests.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def session() -> StubSession:
    return StubSession()


@pytest.fixture
def cache(tmp_path, session) -> ArtworkCache:
    return ArtworkCache(tmp_path, session=session)


def test_cache_key_is_stable_and_url_specific():
    assert cache_key(URL) == cache_key(URL)
    assert cache_key(URL) != cache_key(OTHER)
    assert len(cache_key(URL)) == 64


async def test_fetch_downloads_and_stores(cache, session):
    path = await cache.fetch(URL)
    assert path is not None and path.read_bytes() == JPEG
    assert session.requests == [URL]


async def test_second_fetch_is_served_from_disk(cache, session):
    first = await cache.fetch(URL)
    second = await cache.fetch(URL)
    assert first == second
    assert session.requests == [URL], "a cached cover must not be downloaded again"


async def test_no_url_is_not_an_error(cache, session):
    assert await cache.fetch(None) is None
    assert await cache.fetch("") is None
    assert session.requests == []


async def test_http_error_returns_none(tmp_path):
    cache = ArtworkCache(tmp_path, session=StubSession(StubResponse(status=404)))
    assert await cache.fetch(URL) is None
    assert cache.lookup(URL) is None


async def test_transport_error_returns_none(tmp_path):
    cache = ArtworkCache(tmp_path, session=StubSession(OSError("connection reset")))
    assert await cache.fetch(URL) is None


async def test_oversized_download_is_rejected(tmp_path):
    huge = StubSession(StubResponse(body=b"\x00" * (MAX_BYTES + 1)))
    cache = ArtworkCache(tmp_path, session=huge)
    assert await cache.fetch(URL) is None


async def test_no_partial_file_is_left_behind(cache):
    await cache.fetch(URL)
    assert list(cache.dir.glob("*.part")) == []


async def test_lookup_finds_a_previously_cached_file(cache):
    assert cache.lookup(URL) is None
    await cache.fetch(URL)
    assert cache.lookup(URL) is not None


async def test_prune_keeps_only_the_newest_entries(tmp_path, session):
    cache = ArtworkCache(tmp_path, max_entries=3, session=session)
    for index in range(6):
        await cache.fetch(f"{URL}?v={index}")
    assert len(list(cache.dir.glob("*.img"))) == 3


async def test_path_for_key_round_trips(cache):
    path = await cache.fetch(URL)
    assert cache.path_for_key(path.stem) == path


@pytest.mark.parametrize("key", ["", "short", "../../etc/passwd", "!" * 64, "a" * 63])
def test_path_for_key_rejects_anything_unexpected(cache, key):
    """This feeds a URL route, so it has to refuse traversal and near misses alike."""
    assert cache.path_for_key(key) is None


async def test_aclose_leaves_an_injected_session_alone(cache, session):
    await cache.aclose()
    assert not session.closed, "the cache did not open it, so it must not close it"
