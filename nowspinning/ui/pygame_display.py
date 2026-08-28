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

from nowspinning.config import Config
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

#: Where the sleeve's right edge cuts across the disc. Only the crescent to the
#: right of this is visible, so it is the only part worth drawing motion into.
SLEEVE_RIGHT = 381 / 453

Color = tuple[int, int, int]

#: Fraction of the platter diameter taken up by the paper label.
LABEL_RATIO = 0.36
SPINDLE_RATIO = 0.028
GROOVE_GAPS = 4
#: Groove gaps on the exposed disc; lighter than the artwork's (28, 28, 28).
DISC_GROOVE: Color = (92, 92, 100)


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
        self._sleeve_cache: dict[tuple[int, int], Any] = {}
        self._cover_cache: dict[tuple[str, int, int], Any] = {}
        self._fonts: dict[int, Any] = {}
        self._platter_size = 0

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

    def font(self, size: int) -> Any:
        """Cached font at ``size``, honouring ``display.font_path`` when set."""
        cached = self._fonts.get(size)
        if cached is None:
            path = self.config.display.font_path
            try:
                cached = self._pygame.font.Font(path or None, size)
            except OSError:
                log.warning("could not load font %s; falling back to the built-in font", path)
                cached = self._pygame.font.Font(None, size)
            self._fonts[size] = cached
        return cached

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
        screen.fill(self.theme.background)

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

    # -- sleeve ----------------------------------------------------------

    def _draw_sleeve(self, box: Any, state: NowPlaying) -> Any:
        """Cover art in a record sleeve, disc protruding, after the Bowtie theme.

        Returns the rect the sleeve was drawn into, so the caller can lay text out
        beside it, or ``None`` if the asset is missing and the record was drawn.
        """
        sleeve = self._get_sleeve(box.width, box.height)
        if sleeve is None:  # asset missing; fall back rather than show nothing
            self._draw_record(box.center, min(box.width, box.height), state)
            return None
        rect = sleeve.get_rect(center=box.center)
        window = self._art_window(rect)
        cover = self._get_cover(window.width, window.height, state)
        if cover is not None:
            self._screen.blit(cover, window)
        self._screen.blit(sleeve, rect)
        self._draw_disc_motion(rect)
        return rect

    def _draw_disc_motion(self, sleeve_rect: Any) -> None:
        """Turn the exposed sliver of the disc.

        The disc in the artwork is drawn as concentric rings, which are unchanged
        by rotation -- spinning the image itself would look completely static. So
        the motion comes from groove gaps, the same trick the record style uses,
        clipped to the crescent the sleeve does not cover.
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
            thickness = max(1, round(radius / 40))
            for i in range(GROOVE_GAPS):
                # Only radii past the sleeve edge ever show, so the gaps live in
                # the outer third of the disc rather than spread across it.
                gap_radius = radius * (0.74 + 0.07 * i)
                start = math.radians(self.angle + i * (360.0 / GROOVE_GAPS))
                span = pygame.Rect(0, 0, round(gap_radius * 2), round(gap_radius * 2))
                span.center = (round(cx), round(cy))
                pygame.draw.arc(self._screen, DISC_GROOVE, span, start, start + 0.30, thickness)
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

    def _get_sleeve(self, max_width: int, max_height: int) -> Any:
        """sleeve.png scaled to fit the box, keeping its proportions."""
        key = (max_width, max_height)
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
        scale = min(max_width / image.get_width(), max_height / image.get_height())
        size = (max(1, int(image.get_width() * scale)), max(1, int(image.get_height() * scale)))
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
        blocks: list[tuple[int, Any]] = []
        heading, heading_color = self._heading(state)
        heading_font = self.font(max(14, int(height * 0.045)))
        blocks.append((0, self._render(heading.upper(), heading_font, heading_color, width)))

        if state.track is None:
            body = self.font(max(16, int(height * 0.065)))
            message = state.message or self._idle_message(state)
            leading = max(2, body.get_height() // 10)
            for index, line in enumerate(self._wrap(message, body, width)[:3]):
                gap = int(height * 0.03) if index == 0 else leading
                blocks.append((gap, self._render(line, body, self.theme.muted, width)))
            return blocks

        track = state.track
        title_font = self._fit(
            track.title, max(20, int(height * 0.12)), width, minimum=max(16, int(height * 0.055))
        )
        leading = max(2, title_font.get_height() // 10)
        for index, line in enumerate(self._wrap(track.title, title_font, width)[:2]):
            gap = int(height * 0.025) if index == 0 else leading
            blocks.append((gap, self._render(line, title_font, self.theme.foreground, width)))

        artist_font = self._fit(track.artist, max(16, int(height * 0.075)), width)
        blocks.append(
            (int(height * 0.035), self._render(track.artist, artist_font, self.theme.accent, width))
        )

        if track.album:
            album_font = self._fit(track.album, max(14, int(height * 0.05)), width)
            blocks.append(
                (int(height * 0.02), self._render(track.album, album_font, self.theme.muted, width))
            )
        return blocks

    def _heading(self, state: NowPlaying) -> tuple[str, Color]:
        if state.track is not None:
            return "Now spinning", self.theme.accent
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

    def _fit(self, text: str, size: int, max_width: int, minimum: int = 12) -> Any:
        """Largest cached font at or below ``size`` whose rendering fits ``max_width``."""
        while size > minimum:
            font = self.font(size)
            if font.size(text)[0] <= max_width:
                return font
            size -= max(1, size // 12)
        return self.font(minimum)

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
        surface = font.render(text, True, color)
        if surface.get_width() > max_width:
            surface = surface.subsurface((0, 0, max_width, surface.get_height()))
        return surface
