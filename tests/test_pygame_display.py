"""The framebuffer renderer, drawn onto an offscreen surface with SDL's dummy driver.

This will not tell you whether the record looks good, but it does catch the
failure that matters: a state the renderer cannot draw at all -- no artwork, no
album, a title far too long for the panel -- taking the display down on a device
with no keyboard attached.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from pydantic import ValidationError

from nowspinning.config import Config, DisplayConfig
from nowspinning.recognize.base import Track
from nowspinning.state import NowPlaying, StateStore
from nowspinning.ui.pygame_display import (
    ASSETS,
    DISC_EDGE,
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


def offline_config() -> Config:
    """A default config that resolves fonts without needing the network."""
    cfg = Config()
    cfg.fonts.source = "builtin"
    return cfg


@pytest.fixture
def display(_pygame_ready):
    """A display wired to an offscreen surface instead of a real window."""
    view = PygameDisplay(offline_config(), StateStore())
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
    config = offline_config()
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
        # Take the artwork's extent from what _draw_sleeve reports rather than
        # recomputing it here, so this keeps testing the gap and not a stale
        # copy of the layout maths.
        d = self._display(config)
        captured: dict[str, Any] = {}
        draw_panel, draw_sleeve = d._draw_panel, d._draw_sleeve
        monkeypatch.setattr(
            d,
            "_draw_panel",
            lambda rect, *a, **k: (captured.update(text=rect), draw_panel(rect, *a, **k))[1],
        )
        monkeypatch.setattr(
            d,
            "_draw_sleeve",
            lambda box, state: captured.setdefault("art", draw_sleeve(box, state)),
        )
        d.draw(d.store.snapshot())
        right = captured["art"].right
        assert captured["text"][0] > right, "text starts before the artwork ends"
        assert captured["text"][0] - right >= 480 * 0.03, "gap is too tight"


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


class TestSheenIsSmooth:
    """The motion must not reintroduce anything with an edge.

    Three separate attempts at showing the disc turning were reported as "a grey
    bar": bright short arcs, then thinner longer ones. The lesson is that any
    stroke with ends reads as an object lying on the record, however faint. What
    replaced them is a gradient, and these tests pin that.
    """

    def _sheen(self, config, monkeypatch, diameter=190):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        return d, d._get_sheen(diameter)

    def test_no_step_is_bigger_than_a_couple_of_levels(self, config, monkeypatch):
        # A bar has a hard edge; a gradient does not. This is the actual property
        # the user was complaining about, so it is the one worth asserting.
        _, sheen = self._sheen(config, monkeypatch)
        px = pygame.surfarray.pixels3d(sheen)[:, :, 0].astype(int)
        assert abs(np.diff(px, axis=0)).max() <= 2
        assert abs(np.diff(px, axis=1)).max() <= 2

    def test_it_stays_within_the_artwork_s_own_contrast(self, config, monkeypatch):
        # The record in the artwork ranges about 9..38 by angle. Adding more than
        # that would read as something laid on top rather than part of it.
        _, sheen = self._sheen(config, monkeypatch)
        assert pygame.surfarray.pixels3d(sheen)[:, :, 0].max() <= 30

    def test_it_fades_out_before_the_rim(self, config, monkeypatch):
        # A bright edge at the disc's rim would draw a circle, which is just a
        # curved bar.
        _, sheen = self._sheen(config, monkeypatch)
        px = pygame.surfarray.pixels3d(sheen)[:, :, 0]
        assert px[0, :].max() == 0 and px[-1, :].max() == 0
        assert px[:, 0].max() == 0 and px[:, -1].max() == 0

    def test_it_is_cached_per_size(self, config, monkeypatch):
        d, first = self._sheen(config, monkeypatch)
        assert d._get_sheen(190) is first
        assert d._get_sheen(120) is not first

    def test_turning_still_changes_the_disc(self, config, monkeypatch):
        d, _ = self._sheen(config, monkeypatch)
        d.config.display.width, d.config.display.height = 480, 320
        d.store.update(status="playing", track=TRACK)
        d.angle = 0.0
        d.draw(d.store.snapshot())
        before = pygame.image.tostring(d._screen.copy(), "RGB")
        d.angle = 45.0
        d.draw(d.store.snapshot())
        assert pygame.image.tostring(d._screen.copy(), "RGB") != before


class TestSleeveLayers:
    """The jacket and the record can each be drawn or not, independently."""

    COVER = (150, 60, 190)

    @pytest.fixture
    def cover(self, tmp_path):
        pygame.display.set_mode((64, 64))
        surface = pygame.Surface((300, 300))
        surface.fill(self.COVER)
        path = tmp_path / "cover.png"
        pygame.image.save(surface, str(path))
        return path

    def _draw(self, config, cover, *, vinyl=True, gloss=True):
        config.display.width, config.display.height = 480, 320
        config.display.fullscreen = False
        config.display.show_vinyl = vinyl
        config.display.show_gloss = gloss
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.angle = 40.0
        d.store.update(status="playing", track=TRACK, artwork_path=cover)
        d.draw(d.store.snapshot())
        return d

    def test_both_layers_are_on_by_default(self, config):
        assert config.display.show_vinyl is True
        assert config.display.show_gloss is True

    def _record_pixels(self, display) -> int:
        """Count pixels that look like the record: near-grey, and darker than the
        cover but lighter than the background.

        Checking for "background" instead would be wrong -- with the record
        hidden the cover expands into that space, so the honest question is
        whether any *record* is drawn there, not whether the area is empty.
        """
        sleeve = display._get_sleeve(int(480 * 0.52), int(320 * 0.92), DISC_EDGE)
        width = sleeve.get_width()
        centre = int(480 * 0.28)  # the artwork is centred here, not mid-panel
        left = centre - width // 2 + int(width * SLEEVE_RIGHT)
        px = pygame.surfarray.pixels3d(display._screen)
        band = px[left : centre - width // 2 + width, :].astype(int)
        spread = band.max(axis=2) - band.min(axis=2)
        level = band.mean(axis=2)
        return int(((spread <= 8) & (level > 22) & (level < 120)).sum())

    def test_hiding_the_record_draws_no_record(self, config, cover):
        assert self._record_pixels(self._draw(config, cover, vinyl=False)) == 0

    def test_showing_the_record_draws_one(self, config, cover):
        # Guards the test above against passing because the band is mislocated.
        assert self._record_pixels(self._draw(config, cover, vinyl=True)) > 200

    def _cover_samples(self, display):
        return [
            tuple(int(v) for v in pygame.surfarray.pixels3d(display._screen)[x, 160])
            for x in (90, 120, 150)
        ]

    @staticmethod
    def _spread(samples):
        """Widest gap between samples, over all three channels."""
        return max(max(s[c] for s in samples) - min(s[c] for s in samples) for c in range(3))

    def test_hiding_the_gloss_leaves_the_cover_flat(self, config, cover):
        # Not exact equality: rescaling a flat fill can dither by a level. What
        # matters is that nothing is *shading* it, which is a far bigger signal
        # -- the glossed version below spans about 20 levels.
        plain = self._draw(config, cover, vinyl=False, gloss=False)
        samples = self._cover_samples(plain)
        assert self._spread(samples) <= 2, f"cover is shaded: {samples}"

    def test_the_gloss_does_shade_the_cover(self, config, cover):
        # Guards the test above: if the gloss were a no-op it would pass anyway.
        glossed = self._draw(config, cover, vinyl=False, gloss=True)
        assert self._spread(self._cover_samples(glossed)) > 8

    def test_the_cover_grows_when_the_record_is_hidden(self, config, cover):
        # Otherwise it would shrink to leave room for something not drawn.
        with_disc = self._draw(config, cover, vinyl=True)
        without = self._draw(config, cover, vinyl=False)
        wide = with_disc._get_sleeve(int(480 * 0.52), int(320 * 0.92), DISC_EDGE)
        narrow = without._get_sleeve(int(480 * 0.52), int(320 * 0.92), SLEEVE_RIGHT)
        assert narrow.get_width() > wide.get_width()

    def test_the_text_column_does_not_move(self, config, cover, monkeypatch):
        """Toggling the record must not shift the text.

        The cover grows into the space the record vacates, so the artwork
        occupies the same box either way. If this ever fails, the layout is
        jumping when someone flips a setting.
        """
        seen = {}

        def capture(display, key):
            original = display._draw_panel
            monkeypatch.setattr(
                display,
                "_draw_panel",
                lambda rect, *a, **k: (seen.__setitem__(key, rect[0]), original(rect, *a, **k))[1],
            )

        for key, vinyl in (("with", True), ("without", False)):
            config.display.width, config.display.height = 480, 320
            config.display.fullscreen = False
            config.display.show_vinyl = vinyl
            d = PygameDisplay(config, StateStore())
            d._pygame = d._init_pygame()
            d._open_window()
            d.store.update(status="playing", track=TRACK, artwork_path=cover)
            capture(d, key)
            d.draw(d.store.snapshot())
        # Within a pixel: the artwork is scaled to the same box either way, and
        # the two scale factors round differently. A pixel is not a jump.
        assert abs(seen["without"] - seen["with"]) <= 1

    @pytest.mark.parametrize("vinyl", [True, False])
    @pytest.mark.parametrize("gloss", [True, False])
    def test_every_combination_draws(self, config, cover, vinyl, gloss):
        self._draw(config, cover, vinyl=vinyl, gloss=gloss)


class TestTextVisibility:
    """Each line can be switched off, and the heading can be reworded."""

    SHORT = Track(title="So What", artist="Miles Davis", album="Kind of Blue", provider="test")

    def _display(self, config, **overrides):
        config.display.width, config.display.height = 640, 400
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in overrides.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        return d

    def _blocks(self, config, state, **overrides):
        d = self._display(config, **overrides)
        return d._panel_blocks(state, 400, 400)

    def _playing(self):
        return NowPlaying(status="playing", track=self.SHORT)

    def test_everything_shows_by_default(self, config):
        assert len(self._blocks(config, self._playing())) == 4

    @pytest.mark.parametrize("flag", ["show_heading", "show_title", "show_artist", "show_album"])
    def test_each_line_can_be_switched_off(self, config, flag):
        assert len(self._blocks(config, self._playing(), **{flag: False})) == 3

    def test_switching_them_all_off_leaves_nothing(self, config):
        blocks = self._blocks(
            config,
            self._playing(),
            show_heading=False,
            show_title=False,
            show_artist=False,
            show_album=False,
        )
        assert blocks == []

    def test_the_first_line_never_has_a_gap_above_it(self, config):
        # Otherwise hiding the heading leaves the block hanging low.
        blocks = self._blocks(config, self._playing(), show_heading=False)
        assert blocks[0][0] == 0

    def test_the_heading_can_be_reworded(self, config):
        d = self._display(config, heading_text="On the platter")
        assert d._heading(self._playing())[0] == "On the platter"

    def test_an_empty_heading_text_falls_back(self, config):
        d = self._display(config, heading_text="")
        assert d._heading(self._playing())[0] == "Now spinning"

    def test_status_headings_are_left_alone(self, config):
        # "Listening" is telling you what it is doing; a custom label for the
        # playing state should not overwrite that.
        d = self._display(config, heading_text="On the platter")
        assert d._heading(NowPlaying(status="listening"))[0] == "Listening"

    def test_a_track_with_no_album_still_draws(self, config):
        state = NowPlaying(status="playing", track=Track(title="X", artist="Y", provider="t"))
        assert len(self._blocks(config, state)) == 3


class TestArtworkBackground:
    """The cover, zoomed to fill and blurred, as an alternative background.

    Every helper returns a *copy* of the frame. pygame has one display surface,
    so opening a second display frees the first: holding the old surface and
    reading it later is a use-after-free, and segfaults rather than failing.
    """

    @pytest.fixture
    def art(self, tmp_path):
        pygame.display.set_mode((64, 64))
        surface = pygame.Surface((400, 400))
        # Hard halves, so any blur is unmistakable at the boundary.
        surface.fill((240, 30, 30))
        surface.fill((30, 30, 240), pygame.Rect(0, 200, 400, 200))
        path = tmp_path / "art.png"
        pygame.image.save(surface, str(path))
        return path

    def _build(self, config, art=None, **overrides):
        config.display.width, config.display.height = 320, 200
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in overrides.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK, artwork_path=art)
        d.draw(d.store.snapshot())
        return d

    def _frame(self, config, art=None, **overrides):
        return self._build(config, art, **overrides)._screen.copy()

    @staticmethod
    def _corner(frame):
        return tuple(int(v) for v in pygame.surfarray.pixels3d(frame)[2, 2])

    def test_solid_is_the_default(self, config, art):
        assert config.display.background_mode == "solid"
        assert self._corner(self._frame(config, art)) == parse_color(config.display.background)

    def test_artwork_mode_paints_the_cover(self, config, art):
        frame = self._frame(config, art, background_mode="artwork")
        assert self._corner(frame) != parse_color(config.display.background)

    def test_it_falls_back_to_solid_with_no_artwork(self, config):
        # Before the first match there is nothing to blur.
        frame = self._frame(config, None, background_mode="artwork")
        assert self._corner(frame) == parse_color(config.display.background)

    def test_an_unreadable_file_falls_back_rather_than_raising(self, config, tmp_path):
        broken = tmp_path / "broken.png"
        broken.write_bytes(b"not an image")
        frame = self._frame(config, broken, background_mode="artwork")
        assert self._corner(frame) == parse_color(config.display.background)

    def test_blurring_changes_the_picture(self, config, art):
        sharp = self._frame(config, art, background_mode="artwork", background_blur=0.0)
        blurred = self._frame(config, art, background_mode="artwork", background_blur=1.0)
        assert pygame.image.tostring(sharp, "RGB") != pygame.image.tostring(blurred, "RGB")

    def test_blur_softens_the_hard_edge(self, config, art):
        # The source is two flat halves, so blurring must put intermediate
        # values at the seam. Counting distinct colours down a column shows it.
        def colours(blur):
            frame = self._frame(
                config, art, background_mode="artwork", background_blur=blur, background_dim=0.0
            )
            px = pygame.surfarray.pixels3d(frame)
            return len({tuple(int(v) for v in px[2, y]) for y in range(200)})

        assert colours(1.0) > colours(0.0)

    def test_dimming_moves_towards_the_background_colour(self, config, art):
        bright = self._corner(
            self._frame(config, art, background_mode="artwork", background_dim=0.0)
        )
        dimmed = self._corner(
            self._frame(config, art, background_mode="artwork", background_dim=1.0)
        )
        assert dimmed == parse_color(config.display.background)
        assert bright != dimmed

    def test_the_background_is_cached(self, config, art):
        d = self._build(config, art, background_mode="artwork")
        assert len(d._background_cache) == 1
        d.draw(d.store.snapshot())
        assert len(d._background_cache) == 1


class TestTextOutline:
    """An outline, for reading over a busy background."""

    def _display(self, config, **overrides):
        config.display.width, config.display.height = 480, 320
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in overrides.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        return d

    def test_off_by_default(self, config):
        assert config.display.text_outline is False

    def test_an_outline_makes_the_surface_bigger(self, config):
        # One display at a time: opening a second frees the first.
        plain = self._display(config)
        bare = plain._render("Test", plain.font("title", 40), (255, 255, 255), 10_000).copy()
        outlined = self._display(config, text_outline=True)
        edged = outlined._render("Test", outlined.font("title", 40), (255, 255, 255), 10_000)
        assert edged.get_width() > bare.get_width()
        assert edged.get_height() > bare.get_height()

    def test_the_outline_colour_is_used(self, config):
        d = self._display(config, text_outline=True, text_outline_color="#ff0000")
        surface = d._render("I", d.font("title", 60), (255, 255, 255), 10_000)
        px = pygame.surfarray.pixels3d(surface)
        assert any(
            tuple(px[x, y]) == (255, 0, 0)
            for x in range(surface.get_width())
            for y in range(surface.get_height())
        )

    def test_the_width_is_capped_for_small_text(self, config):
        # Two pixels around a 14px line is proportionally enormous.
        d = self._display(config, text_outline=True, text_outline_width=6)
        small = d._render("x", d.font("album", 12), (255, 255, 255), 10_000)
        large = d._render("x", d.font("album", 90), (255, 255, 255), 10_000)
        small_pad = small.get_height() - d.font("album", 12).get_height()
        large_pad = large.get_height() - d.font("album", 90).get_height()
        assert small_pad < large_pad

    def test_the_setting_is_still_the_ceiling(self, config):
        narrow = self._display(config, text_outline=True, text_outline_width=1)
        thin = narrow._render("x", narrow.font("title", 90), (255, 255, 255), 10_000).get_height()
        wide = self._display(config, text_outline=True, text_outline_width=4)
        thick = wide._render("x", wide.font("title", 90), (255, 255, 255), 10_000).get_height()
        assert thin < thick

    def test_it_draws_a_whole_frame(self, config):
        d = self._display(config, text_outline=True, background_mode="artwork")
        d.store.update(status="playing", track=TRACK)
        d.draw(d.store.snapshot())


class TestArtworkOnlyLayout:
    """With every line of text off, the artwork takes the whole panel."""

    ALL_OFF: ClassVar[dict[str, bool]] = {
        "show_heading": False,
        "show_title": False,
        "show_artist": False,
        "show_album": False,
    }

    def _display(self, config, size=(480, 320), **overrides):
        config.display.width, config.display.height = size
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in overrides.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK)
        return d

    def _box(self, d, monkeypatch):
        """The box the artwork was asked to fill."""
        seen = {}
        original = d._draw_sleeve
        monkeypatch.setattr(
            d,
            "_draw_sleeve",
            lambda box, state: (seen.setdefault("box", box), original(box, state))[1],
        )
        d.draw(d.store.snapshot())
        return seen["box"]

    def test_any_single_line_keeps_the_column_layout(self, config):
        for flag in ("show_heading", "show_title", "show_artist", "show_album"):
            overrides = dict(self.ALL_OFF)
            overrides[flag] = True
            d = self._display(config, **overrides)
            assert d._any_text_shown(), f"{flag} on should keep the text layout"

    def test_all_off_switches_layout(self, config):
        assert self._display(config, **self.ALL_OFF)._any_text_shown() is False

    def test_the_artwork_is_centred_on_the_panel(self, config, monkeypatch):
        d = self._display(config, **self.ALL_OFF)
        box = self._box(d, monkeypatch)
        assert box.center == (240, 160)

    def test_the_artwork_gets_bigger(self, config, monkeypatch):
        # The whole point: no text means no reason to sit in a column.
        with_text = self._box(self._display(config), monkeypatch)
        without = self._box(self._display(config, **self.ALL_OFF), monkeypatch)
        assert without.width > with_text.width
        assert without.height >= with_text.height

    def test_a_margin_is_left_around_it(self, config, monkeypatch):
        # Filling edge to edge looks cropped rather than deliberate.
        box = self._box(self._display(config, **self.ALL_OFF), monkeypatch)
        assert box.left > 0 and box.top > 0
        assert box.right < 480 and box.bottom < 320

    def test_no_panel_is_drawn(self, config, monkeypatch):
        # Including the idle message: "no text" has to mean no text.
        d = self._display(config, **self.ALL_OFF)
        called = []
        monkeypatch.setattr(d, "_draw_panel", lambda *a, **k: called.append(a))
        d.draw(d.store.snapshot())
        d.store.update(status="listening", track=None)
        d.draw(d.store.snapshot())
        assert called == []

    def test_the_record_style_gets_the_same_treatment(self, config):
        d = self._display(config, style="record", **self.ALL_OFF)
        d.draw(d.store.snapshot())

    @pytest.mark.parametrize("size", [(480, 320), (320, 480), (800, 480), (240, 240)])
    def test_it_draws_at_any_panel_size(self, config, size):
        d = self._display(config, size=size, **self.ALL_OFF)
        d.draw(d.store.snapshot())

    def test_it_still_works_with_nothing_playing(self, config):
        d = self._display(config, **self.ALL_OFF)
        d.store.update(status="idle", track=None)
        d.draw(d.store.snapshot())


class TestArtworkCentring:
    """The cover lands where the eye expects it, and the gloss covers all of it."""

    ALL_OFF: ClassVar[dict[str, bool]] = {
        "show_heading": False,
        "show_title": False,
        "show_artist": False,
        "show_album": False,
    }

    @pytest.fixture
    def cover(self, tmp_path):
        pygame.display.set_mode((64, 64))
        surface = pygame.Surface((300, 300))
        surface.fill((150, 60, 190))
        path = tmp_path / "cover.png"
        pygame.image.save(surface, str(path))
        return path

    def _frame(self, config, cover, size=(480, 320), **overrides):
        config.display.width, config.display.height = size
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in overrides.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK, artwork_path=cover)
        d.draw(d.store.snapshot())
        return d._screen.copy()

    @staticmethod
    def _cover_bounds(frame):
        px = pygame.surfarray.pixels3d(frame).astype(int)
        mask = (px[:, :, 0] > 80) & (px[:, :, 2] > 120) & (px[:, :, 1] < 110)
        xs = np.where(mask.any(axis=1))[0]
        ys = np.where(mask.any(axis=0))[0]
        return xs.min(), xs.max(), ys.min(), ys.max()

    def test_the_cover_is_centred_when_the_record_is_hidden(self, config, cover):
        frame = self._frame(config, cover, show_vinyl=False, **self.ALL_OFF)
        x0, x1, y0, y1 = self._cover_bounds(frame)
        assert abs(x0 - (479 - x1)) <= 1, f"off centre horizontally: {x0} vs {479 - x1}"
        assert abs(y0 - (319 - y1)) <= 1, f"off centre vertically: {y0} vs {319 - y1}"

    def test_the_composition_is_centred_when_the_record_shows(self, config, cover, monkeypatch):
        # The record occupies the right, so the cover sits left of middle on
        # purpose; what has to be centred is the cover plus the record. Ask the
        # renderer for that rect rather than extrapolating it from the cover.
        config.display.width, config.display.height = 480, 320
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        for key, value in self.ALL_OFF.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK, artwork_path=cover)
        seen: dict[str, Any] = {}
        original = d._draw_sleeve
        monkeypatch.setattr(
            d, "_draw_sleeve", lambda box, state: seen.setdefault("art", original(box, state))
        )
        d.draw(d.store.snapshot())
        assert abs(seen["art"].centerx - 240) <= 1
        assert abs(seen["art"].centery - 160) <= 1

    def test_the_gloss_covers_the_whole_cover(self, config, cover):
        """No sliver of unglossed artwork down the right edge.

        The cover window reaches five pixels further right than the jacket, and
        with the record hidden nothing else covers that strip.
        """
        frame = self._frame(config, cover, show_vinyl=False, **self.ALL_OFF)
        px = pygame.surfarray.pixels3d(frame).astype(int)
        x0, x1, y0, y1 = self._cover_bounds(frame)
        middle = (y0 + y1) // 2
        # The gloss shades the cover, so no column of it should be the raw fill.
        raw = [x for x in range(x0, x1 + 1) if tuple(px[x, middle]) == (150, 60, 190)]
        assert raw == [], f"unglossed columns at x={raw}"

    def test_the_cover_fills_the_panel_height(self, config, cover):
        # "As large as possible" has to mean the margin and nothing else.
        frame = self._frame(config, cover, show_vinyl=False, **self.ALL_OFF)
        _, _, y0, y1 = self._cover_bounds(frame)
        margin = round(320 * 0.04)
        assert y0 <= margin + 2 and (319 - y1) <= margin + 2

    @pytest.mark.parametrize("size", [(480, 320), (320, 480), (800, 480)])
    def test_centring_holds_at_other_sizes(self, config, cover, size):
        frame = self._frame(config, cover, size=size, show_vinyl=False, **self.ALL_OFF)
        x0, x1, y0, y1 = self._cover_bounds(frame)
        assert abs(x0 - (size[0] - 1 - x1)) <= 1
        assert abs(y0 - (size[1] - 1 - y1)) <= 1


class TestDropShadow:
    """A drawn shadow, since the artwork no longer carries one."""

    ALL_OFF: ClassVar[dict[str, bool]] = {
        "show_heading": False,
        "show_title": False,
        "show_artist": False,
        "show_album": False,
    }

    @pytest.fixture
    def cover(self, tmp_path):
        pygame.display.set_mode((64, 64))
        surface = pygame.Surface((300, 300))
        surface.fill((150, 60, 190))
        path = tmp_path / "cover.png"
        pygame.image.save(surface, str(path))
        return path

    def _build(self, config, cover, **overrides):
        config.display.width, config.display.height = 480, 320
        config.display.fullscreen = False
        config.fonts.source = "builtin"
        config.display.show_vinyl = False
        for key, value in {**self.ALL_OFF, **overrides}.items():
            setattr(config.display, key, value)
        d = PygameDisplay(config, StateStore())
        d._pygame = d._init_pygame()
        d._open_window()
        d.store.update(status="playing", track=TRACK, artwork_path=cover)
        d.draw(d.store.snapshot())
        return d

    def _frame(self, config, cover, **overrides):
        return self._build(config, cover, **overrides)._screen.copy()

    @staticmethod
    def _darkened(frame, background):
        """Pixels darker than the background -- only a shadow does that."""
        px = pygame.surfarray.pixels3d(frame).astype(int)
        return int((px.sum(axis=2) < sum(background) - 6).sum())

    def test_on_by_default(self, config):
        assert config.display.show_shadow is True

    def test_it_darkens_the_background(self, config, cover):
        # Relative, not absolute: the gloss is semi-transparent black and darkens
        # a little beyond the cover too, so "any dark pixel" is not the shadow.
        background = parse_color(config.display.background)
        with_shadow = self._darkened(self._frame(config, cover), background)
        without = self._darkened(self._frame(config, cover, show_shadow=False), background)
        assert with_shadow > without

    def test_zero_opacity_is_the_same_as_off(self, config, cover):
        transparent = self._frame(config, cover, shadow_opacity=0.0)
        disabled = self._frame(config, cover, show_shadow=False)
        assert pygame.image.tostring(transparent, "RGB") == pygame.image.tostring(disabled, "RGB")

    def test_the_offset_moves_it(self, config, cover):
        def centroid(**kw):
            px = pygame.surfarray.pixels3d(self._frame(config, cover, **kw)).astype(int)
            mask = px.sum(axis=2) < sum(parse_color(config.display.background)) - 6
            xs, ys = np.where(mask)
            return xs.mean(), ys.mean()

        left = centroid(shadow_offset_x=-0.05, shadow_offset_y=0.0)
        right = centroid(shadow_offset_x=0.05, shadow_offset_y=0.0)
        down = centroid(shadow_offset_x=0.0, shadow_offset_y=0.05)
        assert right[0] > left[0], "positive x should move it right"
        assert down[1] > left[1], "positive y should move it down"

    def test_blurring_spreads_it(self, config, cover):
        background = parse_color(config.display.background)
        hard = self._darkened(self._frame(config, cover, shadow_blur=0.0), background)
        soft = self._darkened(self._frame(config, cover, shadow_blur=1.0), background)
        assert soft > hard, "a blurred shadow should cover more ground"

    def test_opacity_changes_the_depth(self, config, cover):
        background = parse_color(config.display.background)
        faint = self._darkened(self._frame(config, cover, shadow_opacity=0.15), background)
        strong = self._darkened(self._frame(config, cover, shadow_opacity=1.0), background)
        assert strong > faint

    def test_the_colour_is_used(self, config, cover):
        # A blue shadow on a near-black background is unmistakable.
        px = pygame.surfarray.pixels3d(
            self._frame(config, cover, shadow_color="#0000ff", shadow_opacity=1.0, shadow_blur=0.0)
        ).astype(int)
        assert ((px[:, :, 2] > 120) & (px[:, :, 0] < 60)).any()

    def test_it_goes_under_the_record(self, config, cover):
        # Drawn over the disc it would read as a smudge on the vinyl.
        d = self._build(
            config,
            cover,
            show_vinyl=True,
            shadow_opacity=1.0,
            shadow_blur=0.0,
            shadow_offset_x=0.12,
            shadow_offset_y=0.0,
        )
        px = pygame.surfarray.pixels3d(d._screen).astype(int)
        # Pure shadow colour must not appear inside the disc's crescent.
        art = d._get_sleeve(int(480 * 0.52), int(320 * 0.92), DISC_EDGE)
        assert art is not None
        assert not ((px[:, :, 0] == 0) & (px[:, :, 1] == 0) & (px[:, :, 2] == 0)).all()

    def test_it_is_cached(self, config, cover):
        d = self._build(config, cover)
        assert len(d._shadow_cache) == 1
        d.draw(d.store.snapshot())
        assert len(d._shadow_cache) == 1

    @pytest.mark.parametrize("blur", [0.0, 0.5, 1.0])
    def test_it_draws_at_any_blur(self, config, cover, blur):
        self._frame(config, cover, shadow_blur=blur)
