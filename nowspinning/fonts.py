"""Finding font files for the renderer.

Three sources, chosen in the config:

``builtin``
    Whatever pygame ships. Always available, never pretty.
``local``
    A directory of ``.ttf``/``.otf`` files, matched by family, weight and slant.
``google``
    Downloaded from Google Fonts once and cached on disk.

Resolution never raises. A wall-mounted display that cannot reach the network,
or that is pointed at a family nobody installed, must still draw the track --
falling back to the built-in font is a cosmetic problem, and a traceback is not.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

from nowspinning.config import FontChoice, FontsConfig

log = logging.getLogger(__name__)

#: Google serves TTF to plain clients and WOFF2 to browsers. pygame cannot read
#: WOFF2, so the request deliberately does not pretend to be a browser.
GOOGLE_CSS = "https://fonts.googleapis.com/css2?family={family}:ital,wght@{italic},{weight}"
_FONT_URL = re.compile(r"src:\s*url\((https://[^)]+\.(?:ttf|otf))\)")
_TIMEOUT = 15.0

WEIGHT_NAMES: dict[int, str] = {
    100: "thin",
    200: "extralight",
    300: "light",
    400: "regular",
    500: "medium",
    600: "semibold",
    700: "bold",
    800: "extrabold",
    900: "black",
}

FONT_SUFFIXES = (".ttf", ".otf", ".ttc")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class FontLibrary:
    """Turns a :class:`FontChoice` into a font file path, or ``None``."""

    def __init__(self, config: FontsConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = cache_dir / "fonts"
        self._resolved: dict[tuple[str | None, int, bool], Path | None] = {}

    # -- public ----------------------------------------------------------

    def resolve(self, choice: FontChoice) -> Path | None:
        """A usable font file, or ``None`` to mean "use pygame's built-in"."""
        key = (choice.family, choice.weight, choice.italic)
        if key in self._resolved:
            return self._resolved[key]
        path = self._resolve_uncached(choice)
        self._resolved[key] = path
        return path

    def _resolve_uncached(self, choice: FontChoice) -> Path | None:
        if not choice.family:
            return None
        source = self.config.source
        if source == "builtin":
            return None
        if source == "local":
            return self._from_directory(choice)
        found = self._from_cache(choice) or self._from_google(choice)
        if found is None:
            # A configured directory is a better fallback than the built-in font.
            found = self._from_directory(choice, quiet=True)
        return found

    # -- local files -----------------------------------------------------

    def _candidates(self) -> list[Path]:
        directory = self.config.directory
        if directory is None:
            return []
        directory = directory.expanduser()
        if not directory.is_dir():
            log.warning("font directory %s does not exist", directory)
            return []
        return [p for p in sorted(directory.rglob("*")) if p.suffix.lower() in FONT_SUFFIXES]

    def _from_directory(self, choice: FontChoice, *, quiet: bool = False) -> Path | None:
        assert choice.family is not None
        family = _slug(choice.family)
        best: tuple[int, Path] | None = None
        for path in self._candidates():
            score = _score(path, family, choice.weight, choice.italic)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, path)
        if best is None:
            if not quiet:
                log.warning(
                    "no font for %s %d%s in %s; using the built-in font",
                    choice.family,
                    choice.weight,
                    " italic" if choice.italic else "",
                    self.config.directory,
                )
            return None
        return best[1]

    # -- google ----------------------------------------------------------

    def _cache_path(self, choice: FontChoice) -> Path:
        assert choice.family is not None
        slant = "i" if choice.italic else ""
        return self.cache_dir / f"{_slug(choice.family)}-{choice.weight}{slant}.ttf"

    def _from_cache(self, choice: FontChoice) -> Path | None:
        path = self._cache_path(choice)
        # A zero-byte file is a half-finished download from a previous run.
        return path if path.is_file() and path.stat().st_size > 0 else None

    def _from_google(self, choice: FontChoice) -> Path | None:
        assert choice.family is not None
        url = GOOGLE_CSS.format(
            family=choice.family.replace(" ", "+"),
            italic=int(choice.italic),
            weight=choice.weight,
        )
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
                css = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("could not reach Google Fonts for %s: %s", choice.family, exc)
            return None

        match = _FONT_URL.search(css)
        if match is None:
            log.warning(
                "Google Fonts has no %s at weight %d%s",
                choice.family,
                choice.weight,
                " italic" if choice.italic else "",
            )
            return None

        try:
            with urllib.request.urlopen(match.group(1), timeout=_TIMEOUT) as response:
                data = response.read()
        except (urllib.error.URLError, OSError) as exc:
            log.warning("could not download %s: %s", match.group(1), exc)
            return None

        path = self._cache_path(choice)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target and move, so an interrupted download never
            # leaves a truncated file that later runs would treat as cached.
            temporary = path.with_suffix(".part")
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError as exc:
            log.warning("could not cache %s: %s", path, exc)
            return None

        log.info("downloaded %s %d%s", choice.family, choice.weight, "i" if choice.italic else "")
        return path


def _score(path: Path, family: str, weight: int, italic: bool) -> int | None:
    """How well ``path`` matches, or ``None`` if it is the wrong family or slant."""
    stem = _slug(path.stem)
    if family not in stem:
        return None
    rest = stem.replace(family, "", 1)
    # "italic" also covers "bolditalic"; "oblique" is the same intent.
    is_italic = "italic" in rest or "oblique" in rest
    if is_italic != italic:
        return None

    score = 0
    if str(weight) in rest:
        score += 4
    name = WEIGHT_NAMES.get(weight, "")
    if name and name in rest:
        score += 3
    # "Bitter-Italic" with no weight token is the regular weight.
    if score == 0 and weight == 400 and rest.replace("italic", "").replace("oblique", "") == "":
        score += 2
    if score == 0:
        return None
    # Prefer the tightest name: "Bitter-Bold" over "BitterCondensed-Bold".
    return score * 100 - len(stem)
