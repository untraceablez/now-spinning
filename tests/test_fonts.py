"""Font resolution.

The rule these tests protect: resolution never raises and never blocks the
display. A wall-mounted screen with no network, a misspelt family, or an empty
font directory all have to end up drawing something.
"""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from nowspinning.config import FontChoice, FontsConfig
from nowspinning.fonts import FontLibrary, _score

BITTER = FontChoice(family="Bitter", weight=700, italic=True)

CSS = """@font-face {
  font-family: 'Bitter';
  font-style: italic;
  font-weight: 700;
  src: url(https://fonts.gstatic.com/s/bitter/v42/example.ttf) format('truetype');
}"""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen(payloads: dict[str, bytes], calls: list[str]):
    def opener(url, timeout=None):
        calls.append(url)
        for fragment, body in payloads.items():
            if fragment in url:
                return FakeResponse(body)
        raise urllib.error.URLError(f"unexpected url {url}")

    return opener


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


class TestSource:
    def test_builtin_never_resolves_a_file(self, cache, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr("nowspinning.fonts.urllib.request.urlopen", fake_urlopen({}, calls))
        library = FontLibrary(FontsConfig(source="builtin"), cache)
        assert library.resolve(BITTER) is None
        assert calls == [], "builtin must not touch the network"

    def test_no_family_means_the_builtin_font(self, cache):
        library = FontLibrary(FontsConfig(source="google"), cache)
        assert library.resolve(FontChoice(family=None)) is None


class TestGoogle:
    def _library(self, cache, monkeypatch, calls):
        monkeypatch.setattr(
            "nowspinning.fonts.urllib.request.urlopen",
            fake_urlopen({"css2": CSS.encode(), "example.ttf": b"TTF-BYTES"}, calls),
        )
        return FontLibrary(FontsConfig(source="google"), cache)

    def test_it_downloads_and_caches(self, cache, monkeypatch):
        calls: list[str] = []
        path = self._library(cache, monkeypatch, calls).resolve(BITTER)
        assert path is not None
        assert path.read_bytes() == b"TTF-BYTES"
        assert "ital,wght@1,700" in calls[0], "slant and weight must reach the request"

    def test_a_second_run_uses_the_cache(self, cache, monkeypatch):
        calls: list[str] = []
        self._library(cache, monkeypatch, calls).resolve(BITTER)
        downloaded = len(calls)
        # A fresh library, so this is the on-disk cache rather than the memo.
        self._library(cache, monkeypatch, calls).resolve(BITTER)
        assert len(calls) == downloaded, "cached font was downloaded again"

    def test_one_lookup_per_choice(self, cache, monkeypatch):
        calls: list[str] = []
        library = self._library(cache, monkeypatch, calls)
        for _ in range(5):
            library.resolve(BITTER)
        assert len(calls) == 2, "expected one CSS fetch and one download"

    def test_being_offline_falls_back_to_the_builtin_font(self, cache, monkeypatch):
        def offline(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr("nowspinning.fonts.urllib.request.urlopen", offline)
        assert FontLibrary(FontsConfig(source="google"), cache).resolve(BITTER) is None

    def test_an_unknown_family_falls_back(self, cache, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            "nowspinning.fonts.urllib.request.urlopen",
            fake_urlopen({"css2": b"/* nothing */"}, calls),
        )
        library = FontLibrary(FontsConfig(source="google"), cache)
        assert library.resolve(FontChoice(family="Notafont")) is None

    def test_a_truncated_cache_file_is_not_trusted(self, cache, monkeypatch):
        calls: list[str] = []
        library = self._library(cache, monkeypatch, calls)
        stale = library._cache_path(BITTER)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"")
        assert library.resolve(BITTER).read_bytes() == b"TTF-BYTES"

    def test_a_configured_directory_is_used_when_offline(self, cache, tmp_path, monkeypatch):
        def offline(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr("nowspinning.fonts.urllib.request.urlopen", offline)
        local = tmp_path / "fonts"
        local.mkdir()
        (local / "Bitter-BoldItalic.ttf").write_bytes(b"x")
        library = FontLibrary(FontsConfig(source="google", directory=local), cache)
        assert library.resolve(BITTER) == local / "Bitter-BoldItalic.ttf"


class TestLocalDirectory:
    def _library(self, tmp_path, names):
        local = tmp_path / "fonts"
        local.mkdir()
        for name in names:
            (local / name).write_bytes(b"x")
        return FontLibrary(FontsConfig(source="local", directory=local), tmp_path / "c"), local

    def test_it_matches_a_named_weight(self, tmp_path):
        library, local = self._library(tmp_path, ["Bitter-Regular.ttf", "Bitter-BoldItalic.ttf"])
        assert library.resolve(BITTER) == local / "Bitter-BoldItalic.ttf"

    def test_it_matches_a_numeric_weight(self, tmp_path):
        library, local = self._library(tmp_path, ["Bitter-400.ttf", "Bitter-700italic.ttf"])
        assert library.resolve(BITTER) == local / "Bitter-700italic.ttf"

    def test_it_will_not_return_the_wrong_slant(self, tmp_path):
        # Upright Bold when Bold Italic was asked for is worse than the fallback,
        # because it looks deliberate.
        library, _ = self._library(tmp_path, ["Bitter-Bold.ttf"])
        assert library.resolve(BITTER) is None

    def test_a_bare_family_name_counts_as_regular(self, tmp_path):
        library, local = self._library(tmp_path, ["Bitter.ttf"])
        assert library.resolve(FontChoice(family="Bitter", weight=400)) == local / "Bitter.ttf"

    def test_a_missing_directory_is_survivable(self, tmp_path):
        config = FontsConfig(source="local", directory=tmp_path / "nope")
        assert FontLibrary(config, tmp_path).resolve(BITTER) is None

    def test_it_searches_subfolders(self, tmp_path):
        local = tmp_path / "fonts" / "bitter" / "static"
        local.mkdir(parents=True)
        (local / "Bitter-BoldItalic.ttf").write_bytes(b"x")
        config = FontsConfig(source="local", directory=tmp_path / "fonts")
        assert FontLibrary(config, tmp_path).resolve(BITTER) == local / "Bitter-BoldItalic.ttf"


class TestScoring:
    def test_a_different_family_never_matches(self):
        assert _score(Path("Roboto-BoldItalic.ttf"), "bitter", 700, True) is None

    def test_the_tightest_family_name_wins(self):
        exact = _score(Path("Bitter-Bold.ttf"), "bitter", 700, False)
        wider = _score(Path("BitterCondensed-Bold.ttf"), "bitter", 700, False)
        assert exact is not None and wider is not None
        assert exact > wider

    def test_oblique_counts_as_italic(self):
        assert _score(Path("Bitter-BoldOblique.ttf"), "bitter", 700, True) is not None
