"""The framebuffer renderer, drawn onto an offscreen surface with SDL's dummy driver.

This will not tell you whether the record looks good, but it does catch the
failure that matters: a state the renderer cannot draw at all -- no artwork, no
album, a title far too long for the panel -- taking the display down on a device
with no keyboard attached.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from pydantic import ValidationError

from nowspinning.config import Config, DisplayConfig
from nowspinning.recognize.base import Track
from nowspinning.state import NowPlaying, StateStore
from nowspinning.ui.pygame_display import (
    PygameDisplay,
    Theme,
    mix,
    parse_color,
)

pygame = pytest.importorskip("pygame")

LONG_TITLE = "Shine On You Crazy Diamond (Parts I-V) - Remastered Extended Version"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#101014", (16, 16, 20)),
        ("101014", (16, 16, 20)),
        ("#fff", (255, 255, 255)),
        ("  #C8A24A  ", (200, 162, 74)),
    ],
)
def test_parse_color(text, expected):
    assert parse_color(text) == expected


@pytest.mark.parametrize("text", ["", "#12345", "not a color", "#gggggg"])
def test_unparseable_colors_fall_back(text):
    assert parse_color(text, (1, 2, 3)) == (1, 2, 3)


def test_mix_interpolates_and_clamps():
    assert mix((0, 0, 0), (100, 200, 50), 0.5) == (50, 100, 25)
    assert mix((0, 0, 0), (10, 10, 10), -5.0) == (0, 0, 0)
    assert mix((0, 0, 0), (10, 10, 10), 5.0) == (10, 10, 10)


def test_theme_reads_the_config():
    theme = Theme.from_config(Config())
    assert theme.background == (16, 16, 20)
    assert theme.accent == (200, 162, 74)
    assert theme.muted != theme.foreground


@pytest.fixture(scope="module")
def _pygame_ready():
    pygame.init()
    pygame.font.init()
    yield
    pygame.quit()


@pytest.fixture
def display(_pygame_ready):
    """A display wired to an offscreen surface instead of a real window."""
    view = PygameDisplay(Config(), StateStore())
    view._pygame = pygame
    view._screen = pygame.Surface((1024, 600))
    return view


TRACK = Track(title="So What", artist="Miles Davis", album="Kind of Blue", provider="test")


@pytest.mark.parametrize(
    "state",
    [
        NowPlaying(status="idle"),
        NowPlaying(status="listening"),
        NowPlaying(status="identifying"),
        NowPlaying(status="playing", track=TRACK),
        NowPlaying(status="playing", track=Track(title="Untitled", artist="", provider="test")),
        NowPlaying(
            status="playing",
            track=Track(title=LONG_TITLE, artist=LONG_TITLE, album=LONG_TITLE, provider="test"),
        ),
        NowPlaying(status="listening", message="Lost the thread - still listening"),
    ],
    ids=["idle", "listening", "identifying", "playing", "no-artist", "very-long", "message"],
)
def test_every_state_draws_without_raising(display, state):
    display.draw(state)


@pytest.mark.parametrize("size", [(320, 240), (800, 480), (1920, 1080), (600, 1024)])
def test_draws_at_any_screen_size(display, size):
    display._screen = pygame.Surface(size)
    display.draw(NowPlaying(status="playing", track=TRACK))


def test_missing_artwork_file_falls_back_to_a_plain_label(display, tmp_path):
    state = NowPlaying(status="playing", track=TRACK, artwork_path=tmp_path / "gone.jpg")
    display.draw(state)


def test_corrupt_artwork_falls_back_to_a_plain_label(display, tmp_path):
    path = tmp_path / "broken.img"
    path.write_bytes(b"this is definitely not an image")
    display.draw(NowPlaying(status="playing", track=TRACK, artwork_path=path))


def test_platter_is_rendered_once_per_size(display):
    first = display._get_platter(400)
    assert display._get_platter(400) is first
    assert display._get_platter(300) is not first


def test_wrapping_respects_the_available_width(display):
    font = display.font(24)
    lines = display._wrap(LONG_TITLE, font, 200)
    assert len(lines) > 1
    assert all(font.size(line)[0] <= 200 for line in lines[:-1])


def test_wrapping_handles_empty_text(display):
    assert display._wrap("   ", display.font(24), 200) == [""]


def test_fit_shrinks_text_until_it_fits(display):
    font = display._fit(LONG_TITLE, size=90, max_width=300, minimum=10)
    assert font.size(LONG_TITLE)[0] <= 300 or font.get_height() <= 12


def test_fit_leaves_short_text_at_full_size(display):
    assert (
        display._fit("So What", size=40, max_width=1000).get_height()
        == display.font(40).get_height()
    )


def test_an_unloadable_font_path_falls_back(_pygame_ready, tmp_path):
    config = Config()
    config.display.font_path = str(tmp_path / "missing.ttf")
    view = PygameDisplay(config, StateStore())
    view._pygame = pygame
    assert view.font(20) is not None


class TestDeviceIndex:
    """A GPIO/SPI panel is a second DRM device; SDL needs pointing at it."""

    VAR = "SDL_KMSDRM_DEVICE_INDEX"

    @pytest.fixture(autouse=True)
    def _isolate(self):
        # _init_pygame writes straight to os.environ, so save and restore around
        # every test rather than letting one case decide the next one's default.
        saved = os.environ.get(self.VAR)
        os.environ.pop(self.VAR, None)
        yield
        os.environ.pop(self.VAR, None)
        if saved is not None:
            os.environ[self.VAR] = saved

    def _init(self, cfg) -> str | None:
        PygameDisplay(cfg, StateStore())._init_pygame()
        return os.environ.get(self.VAR)

    def test_no_index_is_set_when_unconfigured(self, config):
        # Default must not touch the variable: on a single-display Pi, SDL's own
        # choice is right and overriding it would be a regression.
        assert config.display.device_index is None
        assert self._init(config) is None

    def test_the_configured_card_reaches_sdl(self, config):
        config.display.device_index = 1
        assert self._init(config) == "1"

    def test_card_zero_is_honoured_and_not_mistaken_for_unset(self, config):
        # 0 is falsy; the check must be `is not None` or an explicit card0 is dropped.
        config.display.device_index = 0
        assert self._init(config) == "0"

    def test_the_environment_wins_over_the_config(self, config):
        config.display.device_index = 1
        os.environ[self.VAR] = "2"
        assert self._init(config) == "2"

    def test_a_negative_card_is_rejected(self):
        with pytest.raises(ValidationError):
            DisplayConfig(device_index=-1)
