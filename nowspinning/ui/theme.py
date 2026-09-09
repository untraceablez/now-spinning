"""Theme discovery and loading for web display.

Themes are self-contained HTML/CSS/JavaScript applications served from the
themes/ directory. Each theme has a manifest.json describing its metadata
and configurable options.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

THEMES_DIR = Path(__file__).parent.parent.parent / "themes"


class Theme:
    """A single theme with its metadata and location."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.manifest_path = path / "manifest.json"
        self._manifest: dict[str, Any] | None = None

    @property
    def manifest(self) -> dict[str, Any]:
        """Load and cache the manifest."""
        if self._manifest is None:
            if not self.manifest_path.is_file():
                self._manifest = {"name": self.name}
            else:
                with open(self.manifest_path) as f:
                    self._manifest = json.load(f)
        return self._manifest

    @property
    def main_file(self) -> Path:
        """Path to the theme's main HTML file."""
        main = self.manifest.get("main", "index.html")
        return self.path / main

    @property
    def assets_dir(self) -> Path:
        """Path to theme assets directory."""
        return self.path / "assets"

    def file_path(self, filename: str) -> Path | None:
        """Safely get a file path within the theme, preventing directory traversal."""
        file_path = (self.path / filename).resolve()
        theme_path = self.path.resolve()
        if not str(file_path).startswith(str(theme_path)):
            return None
        if not file_path.is_file():
            return None
        return file_path

    def asset_path(self, filename: str) -> Path | None:
        """Safely get an asset file within assets/, preventing directory traversal."""
        asset_path = (self.assets_dir / filename).resolve()
        assets_path = self.assets_dir.resolve()
        if not str(asset_path).startswith(str(assets_path)):
            return None
        if not asset_path.is_file():
            return None
        return asset_path


class ThemeLoader:
    """Discovers and loads themes from the themes/ directory."""

    def __init__(self, themes_dir: Path = THEMES_DIR):
        self.themes_dir = themes_dir
        self._themes: dict[str, Theme] = {}
        self._discover()

    def _discover(self) -> None:
        """Scan themes_dir for theme directories."""
        if not self.themes_dir.is_dir():
            log.warning("themes directory not found at %s", self.themes_dir)
            return

        for theme_dir in sorted(self.themes_dir.iterdir()):
            if not theme_dir.is_dir() or theme_dir.name.startswith("."):
                continue
            theme = Theme(theme_dir)
            if theme.main_file.is_file():
                self._themes[theme.name] = theme
                log.info("loaded theme: %s", theme.name)
            else:
                log.warning("theme %s missing main file", theme.name)

    def list_themes(self) -> list[str]:
        """Return sorted list of available theme names."""
        return sorted(self._themes.keys())

    def get_theme(self, name: str) -> Theme | None:
        """Get a theme by name, or None if not found."""
        return self._themes.get(name)

    def default_theme(self) -> Theme | None:
        """Get the default theme (usually 'default' if it exists)."""
        if "default" in self._themes:
            return self._themes["default"]
        if self._themes:
            return next(iter(self._themes.values()))
        return None
