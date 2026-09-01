"""The framebuffer renderer, drawn onto an offscreen surface with SDL's dummy driver.

This will not tell you whether the record looks good, but it does catch the
failure that matters: a state the renderer cannot draw at all -- no artwork, no
album, a title far too long for the panel -- taking the display down on a device
with no keyboard attached.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from pydantic import ValidationError

from nowspinning.config import Config, DisplayConfig
from nowspinning.recognize.base import Track
from nowspinning.state import NowPlaying, StateStore
from nowspinning.ui.pygame_display import (
    ASSETS,
    SLEEVE_RIGHT,
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


ROLES = ("heading", "title", "artist", "album")

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
    font = display.font("title", 24)
    lines = display._wrap(LONG_TITLE, font, 200)
    assert len(lines) > 1
    assert all(font.size(line)[0] <= 200 for line in lines[:-1])


def test_wrapping_handles_empty_text(display):
    assert display._wrap("   ", display.font("title", 24), 200) == [""]


def test_fit_shrinks_text_until_it_fits(display):
    font = display._fit("title", LONG_TITLE, size=90, max_width=300, minimum=10)
    assert font.size(LONG_TITLE)[0] <= 300 or font.get_height() <= 12


def test_fit_leaves_short_text_at_full_size(display):
    assert (
        display._fit("title", "So What", size=40, max_width=1000).get_height()
        == display.font("title", 40).get_height()
    )


def test_an_unloadable_font_path_falls_back(_pygame_ready, tmp_path):
    config = Config()
    config.display.font_path = str(tmp_path / "missing.ttf")
    view = PygameDisplay(config, StateStore())
    view._pygame = pygame
    assert view.font("title", 20) is not None


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


class TestVideoDriverSelection:
    """`kmsdrm not available` should not surface as a bare traceback."""

    def test_a_pinned_driver_is_obeyed_exactly(self, config, monkeypatch):
        # The systemd unit pins kmsdrm on purpose; falling back to x11 there
        # would silently render nowhere anyone can see.
        monkeypatch.setenv("SDL_VIDEODRIVER", "kmsdrm")
        assert PygameDisplay(config, StateStore())._driver_candidates() == ["kmsdrm"]

    def test_unpinned_falls_back_through_the_desktop_backends(self, config, monkeypatch):
        monkeypatch.delenv("SDL_VIDEODRIVER", raising=False)
        assert PygameDisplay(config, StateStore())._driver_candidates() == [
            "kmsdrm",
            "wayland",
            "x11",
        ]

    def test_kmsdrm_is_tried_first(self, config, monkeypatch):
        monkeypatch.delenv("SDL_VIDEODRIVER", raising=False)
        assert PygameDisplay(config, StateStore())._driver_candidates()[0] == "kmsdrm"

    def test_failure_explains_the_compositor_problem(self, config, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "no-such-driver")
        config.display.fullscreen = False
        config.display.width, config.display.height = 64, 64
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        with pytest.raises(RuntimeError) as excinfo:
            d._open_window()
        message = str(excinfo.value)
        assert "no-such-driver" in message  # says what it actually tried
        assert "compositor" in message
        assert "raspi-config" in message  # and what to do about it

    def test_a_working_driver_still_opens(self, config, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.fullscreen = False
        config.display.width, config.display.height = 64, 64
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        assert d._screen.get_size() == (64, 64)


class TestFullscreenSizing:
    """The default config is fullscreen at the display's native size."""

    def _display(self, config):
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        return d

    def test_the_default_config_opens(self, config, monkeypatch):
        # width/height default to 0 meaning "native". Combining that with SCALED
        # raises "Cannot set 0 sized SCALED display mode", so this is the exact
        # path a stock `now-spinning run` takes on a real panel.
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        assert config.display.fullscreen is True
        assert (config.display.width, config.display.height) == (0, 0)
        d = self._display(config)
        d._open_window()
        assert d._screen is not None

    def test_native_fullscreen_does_not_request_scaled(self, config, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        d = self._display(config)
        size, flags = d._display_mode()
        assert size == (0, 0)
        assert flags & d._pygame.FULLSCREEN
        assert not flags & d._pygame.SCALED

    def test_an_explicit_size_still_gets_scaled(self, config, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.width, config.display.height = 480, 320
        d = self._display(config)
        size, flags = d._display_mode()
        assert size == (480, 320)
        assert flags & d._pygame.SCALED
        d._open_window()
        assert d._screen.get_size() == (480, 320)

    def test_windowed_uses_no_flags(self, config, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.fullscreen = False
        config.display.width, config.display.height = 320, 240
        assert self._display(config)._display_mode() == ((320, 240), 0)

    def test_a_half_specified_size_is_treated_as_native(self, config, monkeypatch):
        # Only one of the two set is not a size; it must not slip through into
        # SCALED with a zero dimension.
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.width, config.display.height = 480, 0
        d = self._display(config)
        size, flags = d._display_mode()
        assert size == (0, 0)
        assert not flags & d._pygame.SCALED


class TestSleeveStyle:
    """Cover art in a record sleeve, after the Bowtie 'Massive Vinyl' theme."""

    def _display(self, config, size=(480, 320)):
        config.display.width, config.display.height = size
        config.display.fullscreen = False
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        return d

    def test_sleeve_is_the_default_style(self, config):
        assert config.display.style == "sleeve"

    def test_the_assets_are_installed_beside_the_module(self):
        # These ship in the wheel; a packaging slip would only show at runtime
        # on someone else's machine, which is the worst place to find it.
        assert (ASSETS / "sleeve.png").is_file()
        assert (ASSETS / "sleeve-noart.png").is_file()

    def test_the_art_window_matches_the_original_theme(self, config):
        # At the asset's native size the window must land exactly where the
        # theme's stylesheet put it: 355x355 at (27, 14) in a 453x387 sheet.
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        rect = d._pygame.Rect(0, 0, 453, 387)
        window = d._art_window(rect)
        assert (window.x, window.y) == (27, 14)
        assert (window.width, window.height) == (355, 355)

    def test_the_art_window_moves_with_the_sleeve(self, config):
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        at_origin = d._art_window(d._pygame.Rect(0, 0, 453, 387))
        shifted = d._art_window(d._pygame.Rect(100, 50, 453, 387))
        assert (shifted.x - at_origin.x, shifted.y - at_origin.y) == (100, 50)
        assert shifted.size == at_origin.size

    def test_the_cover_stays_inside_the_sleeve(self, config):
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        sleeve = d._pygame.Rect(0, 0, 453, 387)
        assert sleeve.contains(d._art_window(sleeve))

    def test_it_draws_with_no_artwork(self, config):
        d = self._display(config)
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())  # placeholder path

    def test_it_draws_with_artwork(self, config, tmp_path):
        cover = tmp_path / "cover.png"
        surface = self._display(config)._pygame.Surface((300, 300))
        surface.fill((10, 120, 200))
        self._display(config)._pygame.image.save(surface, str(cover))
        d = self._display(config)
        d.store.update(status="playing", track=TRACK, artwork_path=cover)
        d.draw(d.store.snapshot())

    def test_a_missing_asset_falls_back_to_the_record(self, config, monkeypatch):
        # Rendering nothing at all would be worse than the other style.
        import nowspinning.ui.pygame_display as mod

        monkeypatch.setattr(mod, "ASSETS", Path("/nonexistent"))
        d = self._display(config)
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())

    def test_the_record_style_still_works(self, config):
        config.display.style = "record"
        d = self._display(config)
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())

    def test_an_unknown_style_is_rejected(self):
        with pytest.raises(ValidationError):
            DisplayConfig(style="hologram")

    @pytest.mark.parametrize("size", [(480, 320), (320, 480), (1280, 720), (240, 240)])
    def test_it_draws_at_any_panel_size(self, config, size):
        d = self._display(config, size)
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())


class TestSleeveMotion:
    """The exposed sliver of disc has to actually turn."""

    def _display(self, config, size=(480, 320)):
        config.display.width, config.display.height = size
        config.display.fullscreen = False
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK)
        return d

    @staticmethod
    def _crescent_pixels(d):
        """Bytes of the region where the disc shows past the sleeve."""
        box = d._pygame.Rect(0, 0, d.config.display.width, d.config.display.height)
        sleeve = d._get_sleeve(int(box.w * 0.52), int(box.h * 0.92))
        rect = sleeve.get_rect(center=(int(box.w * 0.28), box.h // 2))
        crescent = d._pygame.Rect(
            rect.x + int(rect.w * SLEEVE_RIGHT),
            rect.y,
            rect.w - int(rect.w * SLEEVE_RIGHT),
            rect.h,
        ).clip(d._screen.get_rect())
        return d._pygame.image.tostring(d._screen.subsurface(crescent).copy(), "RGB")

    def test_turning_changes_the_visible_disc(self, config):
        # The artwork's grooves are concentric, so rotating the image itself would
        # produce an identical picture. This is the test that catches that.
        d = self._display(config)
        d.angle = 0.0
        d.draw(d.store.snapshot())
        still = self._crescent_pixels(d)
        d.angle = 40.0
        d.draw(d.store.snapshot())
        turned = self._crescent_pixels(d)
        assert still != turned

    def test_the_same_angle_draws_the_same_thing(self, config):
        d = self._display(config)
        d.angle = 17.0
        d.draw(d.store.snapshot())
        first = self._crescent_pixels(d)
        d.draw(d.store.snapshot())
        assert first == self._crescent_pixels(d)

    def test_the_clip_is_restored_afterwards(self, config):
        # Leaving a clip set would silently crop everything drawn next frame.
        d = self._display(config)
        before = d._screen.get_clip()
        d.draw(d.store.snapshot())
        assert d._screen.get_clip() == before

    def test_motion_is_skipped_when_the_disc_is_too_small(self, config):
        # Tiny panels: the arcs would be sub-pixel noise, and the clip rect could
        # collapse. It must decline rather than raise.
        d = self._display(config, (64, 48))
        d.draw(d.store.snapshot())

    def test_the_text_does_not_touch_the_sleeve(self, config, monkeypatch):
        d = self._display(config)
        captured = {}
        original = d._draw_panel
        monkeypatch.setattr(
            d,
            "_draw_panel",
            lambda rect, *a, **k: (captured.update(rect=rect), original(rect, *a, **k))[1],
        )
        d.draw(d.store.snapshot())
        sleeve = d._get_sleeve(int(480 * 0.52), int(320 * 0.92))
        right = sleeve.get_rect(center=(int(480 * 0.28), 160)).right
        assert captured["rect"][0] > right, "text starts before the sleeve ends"
        assert captured["rect"][0] - right >= 480 * 0.04, "gap is too tight"


class TestPerRoleTypography:
    """Each line of text has its own family, weight, slant and colour."""

    def _display(self, config, monkeypatch, tmp_path):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.width, config.display.height = 480, 320
        config.display.fullscreen = False
        config.fonts.source = "builtin"  # no network in tests
        config.cache_dir = tmp_path
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK)
        return d

    def test_roles_are_cached_separately(self, config, monkeypatch, tmp_path):
        d = self._display(config, monkeypatch, tmp_path)
        d.font("title", 20)
        d.font("album", 20)
        assert ("title", 20) in d._fonts
        assert ("album", 20) in d._fonts

    def test_a_role_colour_overrides_the_theme(self, config, monkeypatch, tmp_path):
        config.fonts.title.color = "#ff0000"
        d = self._display(config, monkeypatch, tmp_path)
        assert d._color("title", (1, 2, 3)) == (255, 0, 0)

    def test_no_role_colour_falls_back_to_the_theme(self, config, monkeypatch, tmp_path):
        d = self._display(config, monkeypatch, tmp_path)
        assert d._color("title", (1, 2, 3)) == (1, 2, 3)

    def test_an_unparseable_colour_falls_back(self, config, monkeypatch, tmp_path):
        # A typo in the config must not black out a line of text.
        config.fonts.artist.color = "not-a-colour"
        d = self._display(config, monkeypatch, tmp_path)
        assert d._color("artist", (9, 9, 9)) == (9, 9, 9)

    def test_font_path_still_overrides_every_role(self, config, monkeypatch, tmp_path):
        # The old single-font setting has to keep working for existing configs.
        config.display.font_path = "/some/where.ttf"
        d = self._display(config, monkeypatch, tmp_path)
        assert all(str(d._font_path(r)) == "/some/where.ttf" for r in ROLES)

    def test_an_unloadable_font_does_not_stop_the_draw(self, config, monkeypatch, tmp_path):
        config.display.font_path = str(tmp_path / "missing.ttf")
        d = self._display(config, monkeypatch, tmp_path)
        d.draw(d.store.snapshot())

    def test_every_role_draws(self, config, monkeypatch, tmp_path):
        d = self._display(config, monkeypatch, tmp_path)
        for role in ROLES:
            assert d.font(role, 18) is not None
        d.draw(d.store.snapshot())


class TestRenderResolution:
    """display.width/height choose the render size; 0 means the panel's own."""

    @pytest.mark.parametrize("size", [(480, 320), (320, 480), (800, 480), (1920, 1080)])
    def test_an_explicit_resolution_is_used(self, config, monkeypatch, size):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        config.display.width, config.display.height = size
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        assert d._screen.get_size() == size
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())
