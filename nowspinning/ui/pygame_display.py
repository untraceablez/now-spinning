"""Full-screen now-playing renderer.

Runs on the framebuffer through KMS/DRM, so a Raspberry Pi OS Lite install with no
desktop environment can boot straight into this. It must own the main thread --
SDL requires it -- so the engine runs on an event loop in a background thread and
this side only ever reads snapshots.

Two styles. ``sleeve`` composites the cover behind a sleeve image and is static,
so it costs nothing per frame once the cover is cached. ``record`` rotates only the
label plus a few groove-gap arcs: turning the whole platter every frame is what a
real record looks like, and also the one thing guaranteed to cost more than a Pi
Zero has to spare.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from nowspinning.config import Config
from nowspinning.fonts import FontLibrary
from nowspinning.state import NowPlaying, StateStore

log = logging.getLogger(__name__)

#: Tried in order when SDL_VIDEODRIVER is not pinned. kmsdrm first because the
#: intended deployment is a Pi with no desktop; the rest let the same command
#: work on a desktop image.
DRIVER_PREFERENCE: tuple[str, ...] = ("kmsdrm", "wayland", "x11")

ASSETS = Path(__file__).with_name("assets")

#: Where the cover sits inside sleeve.png, as fractions of that image. Taken from
#: the original theme's stylesheet: a 355x355 window at (27, 14) in a 453x387
#: sheet, which is why the artwork reads as square despite the sleeve being taller
#: than it is wide.
ART_WINDOW = (27 / 453, 14 / 387, 355 / 453, 355 / 387)

#: The disc in sleeve.png, as fractions of that image, fitted from its opaque
#: pixels: centre (274.2, 194.0) and radius 172.6 in a 453x387 sheet, which
#: reproduces every measured column to within a pixel.
DISC_CENTRE = (274.2 / 453, 194.0 / 387)
DISC_RADIUS = 172.6 / 453

#: The composition to centre and scale by: the cover, plus the record when it is
#: drawn. Deliberately not the image's alpha bounds -- the jacket's shadow
#: reaches 23px to the left of the cover and none to the right, so centring on
#: ink puts the cover visibly right of middle. The eye centres on the artwork.
COVER_LEFT = ART_WINDOW[0]
COVER_TOP = ART_WINDOW[1]
COVER_BOTTOM = ART_WINDOW[1] + ART_WINDOW[3]
DISC_EDGE = DISC_CENTRE[0] + DISC_RADIUS

#: Where the jacket ends and the record begins, as a fraction of sleeve.png.
#: Everything left of it is jacket, everything right is record, which is what
#: lets the two be drawn -- or not drawn -- independently. It is also the left
#: edge of the visible crescent, so the only part worth putting motion into.
SLEEVE_RIGHT = 377 / 453

Color = tuple[int, int, int]

#: Fraction of the platter diameter taken up by the paper label.
LABEL_RATIO = 0.36
SPINDLE_RATIO = 0.028
GROOVE_GAPS = 4
#: Peak brightness the sweeping sheen adds, in levels. The artwork's own disc
#: already ranges from about 9 to 38 with angle, so staying inside that keeps the
#: sheen looking like part of the record rather than something laid on top.
SHEEN_STRENGTH = 22

#: Breathing room left around the artwork when it has the panel to itself,
#: as a fraction of the shorter side. Enough to not look cropped.
ARTWORK_ONLY_MARGIN = 0.04

#: How far the shadow's blur reaches at shadow_blur = 1, as a fraction of the
#: cover's shorter side.
SHADOW_SPREAD = 0.10

#: Where to stamp the outline copies of a glyph, as multiples of the width.
_OUTLINE_OFFSETS: tuple[tuple[int, int], ...] = (
    (-1, 0),
    (1, 0),
    (0, -1),
    (0, 1),
    (-1, -1),
    (1, -1),
    (-1, 1),
    (1, 1),
)


def parse_color(value: str, fallback: Color = (0, 0, 0)) -> Color:
    """Parse ``#rrggbb`` into an RGB triple, falling back on anything unparseable."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return fallback


def mix(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


@dataclass
class Theme:
    background: Color
    foreground: Color
    accent: Color

    @classmethod
    def from_config(cls, config: Config) -> Theme:
        display = config.display
        return cls(
            background=parse_color(display.background, (16, 16, 20)),
            foreground=parse_color(display.foreground, (245, 242, 234)),
            accent=parse_color(display.accent, (200, 162, 74)),
        )

    @property
    def muted(self) -> Color:
        return mix(self.background, self.foreground, 0.55)


class PygameDisplay:
    """Draws :class:`NowPlaying` as a rotating record with a metadata panel."""

    def __init__(self, config: Config, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.theme = Theme.from_config(config)
        self.angle = 0.0

        self._pygame: Any = None
        self._screen: Any = None
        self._clock: Any = None
        self._platter: Any = None
        self._label_cache: dict[str, Any] = {}
        self._sleeve_cache: dict[tuple[int, int, float], Any] = {}
        self._cover_cache: dict[tuple[str, int, int], Any] = {}
        self._background_cache: dict[tuple[Any, ...], Any] = {}
        self._shadow_cache: dict[tuple[Any, ...], Any] = {}
        self._fonts: dict[tuple[str, int], Any] = {}
        self._library = FontLibrary(config.fonts, config.cache_dir)
        self._platter_size = 0
        self._sheen: Any = None
        self._sheen_size = 0

    # -- setup -----------------------------------------------------------

    def _init_pygame(self) -> Any:
        # An SPI panel (a 3.5" ILI9486 hat, say) is its own DRM device, usually
        # card1, and SDL takes card0 unless told otherwise -- which on a Pi with
        # HDMI present means rendering to the port nobody is looking at.
        if self.config.display.device_index is not None:
            os.environ.setdefault("SDL_KMSDRM_DEVICE_INDEX", str(self.config.display.device_index))
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import pygame
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise RuntimeError(
                "the pygame display needs the 'pygame' extra: pip install 'now-spinning[pygame]'"
            ) from exc

        pygame.init()
        pygame.font.init()
        return pygame

    def _driver_candidates(self) -> list[str]:
        """SDL video drivers to try, in order.

        A pinned ``SDL_VIDEODRIVER`` is obeyed exactly -- the systemd unit sets it,
        and so do the tests. Otherwise prefer the framebuffer console, then the two
        desktop backends, so the same command works on Pi OS Lite and on a desktop
        image without anyone having to know which is which.
        """
        pinned = os.environ.get("SDL_VIDEODRIVER")
        return [pinned] if pinned else list(DRIVER_PREFERENCE)

    def _display_mode(self) -> tuple[tuple[int, int], int]:
        """The ``size, flags`` pair to hand ``set_mode``.

        Width and height of 0 mean "whatever the display already is". That cannot
        be combined with ``SCALED`` -- SDL rejects it outright with "Cannot set 0
        sized SCALED display mode" -- so scaling is only requested when there is a
        real size to scale from.
        """
        pygame = self._pygame
        display = self.config.display
        explicit = bool(display.width and display.height)
        size = (display.width, display.height) if explicit else (0, 0)
        if not display.fullscreen:
            return size, 0
        return size, (pygame.FULLSCREEN | pygame.SCALED if explicit else pygame.FULLSCREEN)

    def _open_window(self) -> None:
        pygame = self._pygame
        display = self.config.display
        size, flags = self._display_mode()

        failures: list[str] = []
        for driver in self._driver_candidates():
            os.environ["SDL_VIDEODRIVER"] = driver
            try:
                # Re-init: the driver is chosen when the display subsystem starts,
                # so it has to be torn down before another one can be tried.
                pygame.display.quit()
                pygame.display.init()
                self._screen = pygame.display.set_mode(size, flags)
            except pygame.error as exc:
                failures.append(f"{driver}: {exc}")
                continue
            pygame.display.set_caption("now-spinning")
            pygame.mouse.set_visible(display.show_cursor)
            self._clock = pygame.time.Clock()
            return

        # Only offer the compositor explanation when a driver actually reported
        # itself unavailable; for any other failure it is a red herring.
        hint = ""
        if any("not available" in failure for failure in failures):
            hint = (
                '\n\n"kmsdrm not available" almost always means a desktop compositor already'
                "\nholds the DRM device -- SDL cannot take it while wayfire, labwc or Xorg has"
                "\nit. Either boot to the console (sudo raspi-config -> System Options ->"
                "\nBoot / Auto Login -> Console Autologin), which is what the systemd unit"
                "\nexpects, or run from inside the desktop session so the wayland driver can"
                "\nbe used. Over SSH into a desktop session, neither works."
            )
        raise RuntimeError("could not open a display.\n  " + "\n  ".join(failures) + hint)

    def font(self, role: str, size: int) -> Any:
        """Cached font for a text role at ``size``."""
        key = (role, size)
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        path = self._font_path(role)
        try:
            cached = self._pygame.font.Font(str(path) if path else None, size)
        except OSError:
            log.warning("could not load font %s; falling back to the built-in font", path)
            cached = self._pygame.font.Font(None, size)
        self._fonts[key] = cached
        return cached

    def _font_path(self, role: str) -> Path | None:
        # display.font_path is the old single-font setting; when it is set it wins
        # for every role, so upgrading a config does not silently change anything.
        override = self.config.display.font_path
        if override:
            return Path(override)
        return self._library.resolve(getattr(self.config.fonts, role))

    def _color(self, role: str, default: Color) -> Color:
        """The role's configured colour, or ``default`` from the theme."""
        configured = getattr(self.config.fonts, role).color
        return parse_color(configured, default) if configured else default

    # -- main loop -------------------------------------------------------

    def run(self, stop: threading.Event | None = None) -> None:
        """Render until the window closes or ``stop`` is set. Must be the main thread."""
        self._pygame = self._init_pygame()
        pygame = self._pygame
        self._open_window()
        log.info(
            "display open at %dx%d via %s",
            self._screen.get_width(),
            self._screen.get_height(),
            pygame.display.get_driver(),
        )
        try:
            while stop is None or not stop.is_set():
                if not self._pump_events():
                    break
                dt = self._clock.tick(self.config.display.fps) / 1000.0
                self.angle = (self.angle + self.config.display.rpm * 6.0 * dt) % 360.0
                self.draw(self.store.snapshot())
                pygame.display.flip()
        finally:
            pygame.quit()
            log.info("display closed")

    def _pump_events(self) -> bool:
        pygame = self._pygame
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
        return True

    # -- drawing ---------------------------------------------------------

    def draw(self, state: NowPlaying) -> None:
        screen = self._screen
        width, height = screen.get_size()
        self._paint_background(state)

        if not self._any_text_shown():
            self._draw_artwork_only(state)
            return

        stacked = width < height * 1.2  # portrait or square panels stack instead
        if stacked:
            diameter = int(min(width * 0.82, height * 0.5))
            centre = (width // 2, int(height * 0.34))
            text_rect = (
                int(width * 0.08),
                int(height * 0.64),
                int(width * 0.84),
                int(height * 0.3),
            )
        else:
            diameter = int(min(width * 0.46, height * 0.86))
            centre = (int(width * 0.28), height // 2)
            text_left = int(width * 0.54)
            # Full height: the block centres itself, which lines it up with the
            # middle of the record rather than floating above it.
            text_rect = (text_left, 0, width - text_left - int(width * 0.06), height)

        if self.config.display.style == "sleeve":
            # The sleeve is wider than it is tall, so it gets its own box rather
            # than the platter's diameter, which would leave it floating in a
            # square and wasting most of the panel's height.
            if stacked:
                box_size = (int(width * 0.86), int(height * 0.54))
            else:
                box_size = (int(width * 0.52), int(height * 0.92))
            box = self._pygame.Rect((0, 0), box_size)
            box.center = centre
            drawn = self._draw_sleeve(box, state)
            if drawn is not None and not stacked:
                # Measure the gap from where the artwork actually ends. The image
                # keeps its own proportions inside the box, so a fraction of the
                # panel width would sometimes leave the text touching the disc.
                text_left = drawn.right + int(width * 0.05)
                text_rect = (text_left, 0, max(1, width - text_left - int(width * 0.05)), height)
        else:
            self._draw_record(centre, diameter, state)
        self._draw_panel(text_rect, state, centred=stacked)

    def _any_text_shown(self) -> bool:
        display = self.config.display
        return any(
            (display.show_heading, display.show_title, display.show_artist, display.show_album)
        )

    def _draw_artwork_only(self, state: NowPlaying) -> None:
        """Fill the panel with the artwork when there is no text to make room for.

        With every line switched off, the column layout is just wasted space, so
        the artwork centres and grows to the panel less a small margin. The idle
        message goes too: "no text" has to mean no text, or the one screen that
        was supposed to be purely artwork would still say "Drop the needle".
        """
        width, height = self._screen.get_size()
        margin = round(min(width, height) * ARTWORK_ONLY_MARGIN)
        box = self._pygame.Rect(
            margin, margin, max(1, width - margin * 2), max(1, height - margin * 2)
        )
        if self.config.display.style == "sleeve":
            self._draw_sleeve(box, state)
        else:
            self._draw_record(box.center, min(box.width, box.height), state)

    # -- background ------------------------------------------------------

    def _paint_background(self, state: NowPlaying) -> None:
        display = self.config.display
        if display.background_mode == "artwork" and state.artwork_path is not None:
            wash = self._get_background(self._screen.get_size(), Path(state.artwork_path))
            if wash is not None:
                self._screen.blit(wash, (0, 0))
                return
        self._screen.fill(self.theme.background)

    def _get_background(self, size: tuple[int, int], path: Path) -> Any:
        """The cover, zoomed to fill and blurred into a wash."""
        display = self.config.display
        key = (size, str(path), display.background_blur, display.background_dim)
        cached = self._background_cache.get(key)
        if cached is not None:
            return cached

        pygame = self._pygame
        try:
            image = pygame.image.load(str(path))
        except Exception as exc:
            log.warning("could not load background %s: %s", path, exc)
            return None
        with contextlib.suppress(pygame.error):
            image = image.convert()

        width, height = size
        source_w, source_h = image.get_size()
        if source_w <= 0 or source_h <= 0:
            return None
        # Cover, not fit: fill the panel and let the overflow crop, so there are
        # never bars down the side.
        scale = max(width / source_w, height / source_h)
        scaled = pygame.transform.smoothscale(
            image, (max(1, round(source_w * scale)), max(1, round(source_h * scale)))
        )
        surface = pygame.Surface(size)
        surface.blit(
            scaled, ((width - scaled.get_width()) // 2, (height - scaled.get_height()) // 2)
        )

        # Blur by shrinking and growing again. Twice, because one pass leaves the
        # edges of the original detail faintly visible.
        if display.background_blur > 0.0:
            divisor = max(2, round(2 + display.background_blur * 60))
            small = (max(1, width // divisor), max(1, height // divisor))
            for _ in range(2):
                surface = pygame.transform.smoothscale(
                    pygame.transform.smoothscale(surface, small), size
                )

        # Blur alone does not make pale artwork safe to put text on.
        if display.background_dim > 0.0:
            veil = pygame.Surface(size)
            veil.fill(self.theme.background)
            veil.set_alpha(round(255 * display.background_dim))
            surface.blit(veil, (0, 0))

        self._background_cache = {key: surface}  # only ever one background
        return surface

    # -- sleeve ----------------------------------------------------------

    def _draw_sleeve(self, box: Any, state: NowPlaying) -> Any:
        """Cover art in a record sleeve, disc protruding, after the Bowtie theme.

        Returns the rect the sleeve was drawn into, so the caller can lay text out
        beside it, or ``None`` if the asset is missing and the record was drawn.
        """
        display = self.config.display
        # With the record hidden there is nothing to the right of the jacket, so
        # the jacket alone is what has to fit the box -- otherwise the cover
        # shrinks to leave room for something that is not drawn.
        # The cover is clipped where the jacket ends, so with the record hidden
        # that is also where the composition ends.
        right = DISC_EDGE if display.show_vinyl else SLEEVE_RIGHT
        sleeve = self._get_sleeve(box.width, box.height, right)
        if sleeve is None:  # asset missing; fall back rather than show nothing
            self._draw_record(box.center, min(box.width, box.height), state)
            return None

        width, height = sleeve.get_size()
        left_px, right_px = COVER_LEFT * width, right * width
        top_px, bottom_px = COVER_TOP * height, COVER_BOTTOM * height

        # Round the composition to whole pixels and centre *that*, then place the
        # image relative to it. Centring the image and rounding afterwards lets
        # two roundings stack, which lands the artwork a pixel off centre.
        composition = self._pygame.Rect(0, 0, 0, 0)
        composition.width = max(1, round(right_px - left_px))
        composition.height = max(1, round(bottom_px - top_px))
        composition.center = box.center
        rect = sleeve.get_rect()
        rect.x = composition.x - round(left_px)
        rect.y = composition.y - round(top_px)

        split = rect.x + round(width * SLEEVE_RIGHT)
        window = self._art_window(rect)
        # Under everything, including the record: a shadow drawn over the disc
        # would look like a smudge on the vinyl rather than a shadow beneath it.
        self._draw_shadow(window)
        cover = self._get_cover(window.width, window.height, state)
        if cover is not None:
            # Clipped to where the jacket ends. The cover window reaches five
            # pixels further right than the jacket does, and with the record
            # hidden nothing covers that strip -- it shows as a sliver of
            # unglossed artwork down the edge.
            self._blit_clipped(cover, window, window.left, split)

        if display.show_gloss:
            self._blit_clipped(sleeve, rect, rect.left, split)
        if display.show_vinyl:
            self._blit_clipped(sleeve, rect, split, rect.right)
            self._draw_disc_motion(rect)

        # Report the artwork, so the text column sits beside it rather than
        # beside the image's transparent margin.
        return composition

    def _draw_shadow(self, window: Any) -> None:
        """A soft shadow under the cover.

        The artwork carries no shadow of its own, so this is drawn rather than
        painted in -- which is what makes it adjustable.
        """
        display = self.config.display
        if not display.show_shadow or display.shadow_opacity <= 0.0:
            return
        shadow = self._get_shadow(window.width, window.height)
        if shadow is None:
            return
        pad = (shadow.get_width() - window.width) // 2
        self._screen.blit(
            shadow,
            (
                window.x - pad + round(window.width * display.shadow_offset_x),
                window.y - pad + round(window.height * display.shadow_offset_y),
            ),
        )

    def _get_shadow(self, width: int, height: int) -> Any:
        display = self.config.display
        key = (
            width,
            height,
            display.shadow_blur,
            display.shadow_opacity,
            display.shadow_color,
        )
        cached = self._shadow_cache.get(key)
        if cached is not None:
            return cached
        if width <= 0 or height <= 0:
            return None

        pygame = self._pygame
        # Room for the blur to spread into; without it the haze is cut off square
        # and the shadow gains the hard edge it was supposed to lose.
        spread = round(display.shadow_blur * min(width, height) * SHADOW_SPREAD)
        pad = max(1, spread * 2)
        surface = pygame.Surface((width + pad * 2, height + pad * 2), pygame.SRCALPHA)
        colour = parse_color(display.shadow_color, (0, 0, 0))
        surface.fill(
            (*colour, round(255 * display.shadow_opacity)),
            pygame.Rect(pad, pad, width, height),
        )

        if spread > 0:
            size = surface.get_size()
            small = (
                max(1, size[0] // max(2, spread)),
                max(1, size[1] // max(2, spread)),
            )
            for _ in range(2):
                surface = pygame.transform.smoothscale(
                    pygame.transform.smoothscale(surface, small), size
                )

        self._shadow_cache = {key: surface}  # one shadow on screen at a time
        return surface

    def _blit_clipped(self, surface: Any, rect: Any, left: int, right: int) -> None:
        """Blit ``surface`` at ``rect``, showing only the columns in [left, right)."""
        pygame = self._pygame
        area = pygame.Rect(left, rect.y, max(0, right - left), rect.height)
        area = area.clip(self._screen.get_rect())
        if area.width <= 0 or area.height <= 0:
            return
        previous = self._screen.get_clip()
        self._screen.set_clip(area)
        try:
            self._screen.blit(surface, rect)
        finally:
            self._screen.set_clip(previous)

    def _get_sheen(self, diameter: int) -> Any:
        """A disc-sized sheen: two soft lobes of light, brightest along one axis.

        Rotating this is what makes the record look like it is turning. It is a
        smooth gradient on purpose -- any stroke with ends, however thin, reads as
        a bar lying on the record rather than as light moving across it.
        """
        if self._sheen is not None and self._sheen_size == diameter:
            return self._sheen

        pygame = self._pygame
        half = diameter / 2.0
        yy, xx = np.mgrid[0:diameter, 0:diameter]
        dx, dy = xx - half, yy - half
        radius = np.hypot(dx, dy) / half
        theta = np.arctan2(dy, dx)

        # Two lobes, so light crosses the visible crescent twice per turn.
        lobes = (0.5 + 0.5 * np.cos(2.0 * theta)) ** 2
        # Fade in past the label and out before the rim, so neither edge is a line.
        band = np.clip((radius - 0.30) / 0.25, 0.0, 1.0) * np.clip((0.98 - radius) / 0.12, 0.0, 1.0)
        value = (lobes * band * SHEEN_STRENGTH).astype(np.uint8)

        surface = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        pixels = pygame.surfarray.pixels3d(surface)
        pixels[:] = value.T[:, :, None]
        alpha = pygame.surfarray.pixels_alpha(surface)
        alpha[:] = 255
        del pixels, alpha  # release the surface lock before it is used

        self._sheen = surface
        self._sheen_size = diameter
        return surface

    def _draw_disc_motion(self, sleeve_rect: Any) -> None:
        """Turn the exposed sliver of the disc.

        The record's grooves are concentric, so rotating the artwork shows
        nothing. The motion is a sheen instead: a smooth two-lobed gradient,
        rotated and added over the crescent the sleeve does not cover, so light
        sweeps across the grooves the way it does on a real record.
        """
        pygame = self._pygame
        cx = sleeve_rect.x + sleeve_rect.width * DISC_CENTRE[0]
        cy = sleeve_rect.y + sleeve_rect.height * DISC_CENTRE[1]
        radius = sleeve_rect.width * DISC_RADIUS
        edge = sleeve_rect.x + sleeve_rect.width * SLEEVE_RIGHT
        if radius < 8 or cx + radius <= edge:
            return

        crescent = pygame.Rect(
            round(edge), round(cy - radius), round(cx + radius - edge) + 1, round(radius * 2) + 1
        )
        previous = self._screen.get_clip()
        self._screen.set_clip(crescent.clip(self._screen.get_rect()))
        try:
            sheen = self._get_sheen(round(radius * 2))
            turned = pygame.transform.rotate(sheen, -self.angle)
            self._screen.blit(
                turned,
                turned.get_rect(center=(round(cx), round(cy))),
                special_flags=pygame.BLEND_RGB_ADD,
            )
        finally:
            self._screen.set_clip(previous)

    @staticmethod
    def _art_window(sleeve_rect: Any) -> Any:
        """The cover's rect inside a drawn sleeve."""
        fx, fy, fw, fh = ART_WINDOW
        rect = sleeve_rect.copy()
        rect.x = sleeve_rect.x + round(sleeve_rect.width * fx)
        rect.y = sleeve_rect.y + round(sleeve_rect.height * fy)
        rect.width = round(sleeve_rect.width * fw)
        rect.height = round(sleeve_rect.height * fh)
        return rect

    def _get_sleeve(self, max_width: int, max_height: int, right: float = DISC_EDGE) -> Any:
        """sleeve.png scaled so the composition, out to ``right``, fits the box.

        ``right`` is the record's outer edge when it is drawn and the jacket's
        when it is not, so the cover fills the space either way. Measured from
        the cover rather than the image, whose padding and shadow would
        otherwise eat into the box and pull the artwork off centre.
        """
        key = (max_width, max_height, round(right, 4))
        cached = self._sleeve_cache.get(key)
        if cached is not None:
            return cached
        pygame = self._pygame
        try:
            image = pygame.image.load(str(ASSETS / "sleeve.png"))
        except Exception as exc:
            log.warning("could not load the sleeve asset: %s", exc)
            return None
        with contextlib.suppress(pygame.error):
            image = image.convert_alpha()
        span_width = image.get_width() * (right - COVER_LEFT)
        span_height = image.get_height() * (COVER_BOTTOM - COVER_TOP)
        scale = min(max_width / span_width, max_height / span_height)
        size = (max(1, round(image.get_width() * scale)), max(1, round(image.get_height() * scale)))
        scaled = pygame.transform.smoothscale(image, size)
        self._sleeve_cache[key] = scaled
        return scaled

    def _get_cover(self, width: int, height: int, state: NowPlaying) -> Any:
        """The cover to sit behind the sleeve, or the theme's placeholder."""
        path = state.artwork_path
        key = (str(path) if path else "", width, height)
        cached = self._cover_cache.get(key)
        if cached is not None:
            return cached
        pygame = self._pygame
        source = Path(path) if path else ASSETS / "sleeve-noart.png"
        try:
            image = pygame.image.load(str(source))
        except Exception as exc:
            log.warning("could not load cover %s: %s", source, exc)
            return None
        with contextlib.suppress(pygame.error):
            image = image.convert_alpha()
        scaled = pygame.transform.smoothscale(image, (max(1, width), max(1, height)))
        # One entry is enough: the cover only changes when the track does.
        self._cover_cache = {key: scaled}
        return scaled

    def _draw_record(self, centre: tuple[int, int], diameter: int, state: NowPlaying) -> None:
        pygame = self._pygame
        platter = self._get_platter(diameter)
        rect = platter.get_rect(center=centre)
        self._screen.blit(platter, rect)

        radius = diameter // 2
        # Groove gaps: the visible bands between tracks. Rotating these is what
        # actually reads as motion when there is no artwork on the label.
        for i in range(GROOVE_GAPS):
            gap_radius = int(radius * (0.52 + 0.11 * i))
            start = math.radians(self.angle + i * 90.0)
            box = pygame.Rect(0, 0, gap_radius * 2, gap_radius * 2)
            box.center = centre
            width = max(1, radius // 90)
            pygame.draw.arc(self._screen, (58, 58, 64), box, start, start + 0.16, width)

        label = self._get_label(int(diameter * LABEL_RATIO), state)
        rotated = pygame.transform.rotozoom(label, -self.angle, 1.0)
        self._screen.blit(rotated, rotated.get_rect(center=centre))

        spindle = max(3, int(diameter * SPINDLE_RATIO))
        pygame.draw.circle(self._screen, self.theme.background, centre, spindle)

    def _get_platter(self, diameter: int) -> Any:
        """The static vinyl disc. Re-rendered only when the size changes."""
        if self._platter is not None and self._platter_size == diameter:
            return self._platter

        pygame = self._pygame
        surface = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        centre = (diameter // 2, diameter // 2)
        radius = diameter // 2

        pygame.draw.circle(surface, (12, 12, 14), centre, radius)
        pygame.draw.circle(surface, (44, 44, 50), centre, radius, max(1, diameter // 220))

        step = max(2, diameter // 110)
        for r in range(int(radius * LABEL_RATIO * 0.55), radius - step, step):
            shade = 26 + int(16 * (0.5 + 0.5 * math.sin(r * 0.5)))
            pygame.draw.circle(surface, (shade, shade, shade + 3), centre, r, 1)

        self._platter = surface
        self._platter_size = diameter
        return surface

    def _get_label(self, size: int, state: NowPlaying) -> Any:
        """The paper label -- cover art if we have it, otherwise a plain disc."""
        art_path = state.artwork_path
        key = f"{size}:{art_path or 'blank'}"
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached

        pygame = self._pygame
        surface = pygame.Surface((size, size), pygame.SRCALPHA)
        centre = (size // 2, size // 2)
        radius = size // 2

        artwork = self._load_artwork(art_path, size) if art_path else None
        if artwork is not None:
            mask = pygame.Surface((size, size), pygame.SRCALPHA)
            pygame.draw.circle(mask, (255, 255, 255, 255), centre, radius)
            surface.blit(artwork, (0, 0))
            surface.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        else:
            pygame.draw.circle(surface, self.theme.accent, centre, radius)
            pygame.draw.circle(surface, mix(self.theme.accent, (0, 0, 0), 0.25), centre, radius, 2)
            # A tick mark, so a blank label still visibly turns.
            pygame.draw.line(
                surface,
                mix(self.theme.accent, (0, 0, 0), 0.4),
                (size // 2, int(size * 0.14)),
                (size // 2, int(size * 0.28)),
                max(2, size // 40),
            )

        self._label_cache = {key: surface}  # only ever one label on screen
        return surface

    def _load_artwork(self, path: Path, size: int) -> Any:
        pygame = self._pygame
        try:
            image = pygame.image.load(str(path))
        except Exception as exc:
            log.warning("could not load artwork %s: %s", path, exc)
            return None
        # convert_alpha needs a display surface; without one (offscreen rendering,
        # tests) the unconverted surface blits perfectly well, just more slowly.
        with contextlib.suppress(pygame.error):
            image = image.convert_alpha()
        return pygame.transform.smoothscale(image, (size, size))

    # -- text panel ------------------------------------------------------

    def _draw_panel(
        self, rect: tuple[int, int, int, int], state: NowPlaying, *, centred: bool = False
    ) -> None:
        """Render the metadata block, vertically centred against the record."""
        left, top, width, height = rect
        blocks = self._panel_blocks(state, width, height)
        total = sum(gap + surface.get_height() for gap, surface in blocks)
        y = top + max(0, (height - total) // 2)
        for gap, surface in blocks:
            y += gap
            x = left + (width - surface.get_width()) // 2 if centred else left
            self._screen.blit(surface, (x, y))
            y += surface.get_height()

    def _panel_blocks(self, state: NowPlaying, width: int, height: int) -> list[tuple[int, Any]]:
        """(gap above, rendered line) pairs, top to bottom.

        Laying the panel out as measured blocks rather than blitting as we go is
        what lets it centre itself, and keeps long titles from crowding the artist.
        """
        display = self.config.display
        blocks: list[tuple[int, Any]] = []

        def add(gap: int, surface: Any) -> None:
            # Whatever ends up first has no gap above it, so switching a line off
            # closes the space rather than leaving the block hanging low.
            blocks.append((0 if not blocks else gap, surface))

        if display.show_heading:
            heading, heading_color = self._heading(state)
            heading_font = self.font("heading", max(14, int(height * 0.045)))
            add(
                0,
                self._render(
                    heading.upper(), heading_font, self._color("heading", heading_color), width
                ),
            )

        if state.track is None:
            body = self.font("title", max(16, int(height * 0.065)))
            message = state.message or self._idle_message(state)
            leading = max(2, body.get_height() // 10)
            for index, line in enumerate(self._wrap(message, body, width)[:3]):
                gap = int(height * 0.03) if index == 0 else leading
                add(gap, self._render(line, body, self.theme.muted, width))
            return blocks

        track = state.track
        if display.show_title:
            title_font = self._fit(
                "title",
                track.title,
                max(20, int(height * 0.12)),
                width,
                minimum=max(16, int(height * 0.055)),
            )
            title_color = self._color("title", self.theme.foreground)
            leading = max(2, title_font.get_height() // 10)
            for index, line in enumerate(self._wrap(track.title, title_font, width)[:2]):
                gap = int(height * 0.025) if index == 0 else leading
                add(gap, self._render(line, title_font, title_color, width))

        if display.show_artist:
            artist_font = self._fit("artist", track.artist, max(16, int(height * 0.075)), width)
            artist_color = self._color("artist", self.theme.accent)
            add(int(height * 0.035), self._render(track.artist, artist_font, artist_color, width))

        if display.show_album and track.album:
            album_font = self._fit("album", track.album, max(14, int(height * 0.05)), width)
            album_color = self._color("album", self.theme.muted)
            add(int(height * 0.02), self._render(track.album, album_font, album_color, width))
        return blocks

    def _heading(self, state: NowPlaying) -> tuple[str, Color]:
        if state.track is not None:
            return self.config.display.heading_text or "Now spinning", self.theme.accent
        if state.status == "identifying":
            return "Identifying", self.theme.muted
        if state.status == "listening":
            return "Listening", self.theme.muted
        return "Ready", self.theme.muted

    @staticmethod
    def _idle_message(state: NowPlaying) -> str:
        if state.status == "identifying":
            return "Working out what this is..."
        if state.status == "listening":
            return "Music is playing - waiting for a match."
        return "Drop the needle and I'll tell you what it is."

    def _fit(self, role: str, text: str, size: int, max_width: int, minimum: int = 12) -> Any:
        """Largest cached font at or below ``size`` whose rendering fits ``max_width``."""
        while size > minimum:
            font = self.font(role, size)
            if font.size(text)[0] <= max_width:
                return font
            size -= max(1, size // 12)
        return self.font(role, minimum)

    @staticmethod
    def _wrap(text: str, font: Any, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return [""]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if font.size(candidate)[0] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _render(self, text: str, font: Any, color: Color, max_width: int) -> Any:
        display = self.config.display
        if not display.text_outline:
            surface = font.render(text, True, color)
        else:
            # Scale down for small text: two pixels around a 14px album line is
            # proportionally enormous and reads as furry. The setting is the
            # ceiling, which is what someone tuning it expects.
            width = max(1, min(display.text_outline_width, round(font.get_height() / 10)))
            surface = self._render_outlined(text, font, color, width)
        if surface.get_width() > max_width:
            surface = surface.subsurface((0, 0, max_width, surface.get_height()))
        return surface

    def _render_outlined(self, text: str, font: Any, color: Color, width: int) -> Any:
        """Text with a border, for reading over a busy background."""
        pygame = self._pygame
        outline = parse_color(self.config.display.text_outline_color, (0, 0, 0))
        body = font.render(text, True, color)
        edge = font.render(text, True, outline)
        pad = width
        surface = pygame.Surface(
            (body.get_width() + pad * 2, body.get_height() + pad * 2), pygame.SRCALPHA
        )
        # Ring of offsets rather than a filled square: the corners of a square
        # thicken the diagonals and make small text look furry.
        for dx, dy in _OUTLINE_OFFSETS:
            surface.blit(edge, (pad + dx * width, pad + dy * width))
        surface.blit(body, (pad, pad))
        return surface
