"""Configuration model and YAML loading.

Every field has a working default, so ``now-spinning run`` does something sensible
with no config file at all. A file only needs to contain the keys it overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_NAME = "now-spinning"

#: Searched in order when no explicit ``--config`` is given.
CONFIG_SEARCH_PATH: tuple[Path, ...] = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME / "config.yaml",
    Path.home() / f".{APP_NAME}.yaml",
    Path("/etc") / APP_NAME / "config.yaml",
)


def default_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / APP_NAME


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioConfig(_Base):
    """Microphone capture settings."""

    device: int | str | None = Field(
        default=None,
        description="PortAudio device index or substring of its name. None = system default input.",
    )
    sample_rate: int = Field(
        default=16000,
        ge=8000,
        le=192000,
        description=(
            "Preferred capture rate. USB interfaces often run at exactly one rate, "
            "so capture falls back to whatever the device actually accepts."
        ),
    )
    channels: int = Field(
        default=1, ge=1, le=2, description="Preferred channel count; also negotiated."
    )
    block_size: int = Field(default=1024, ge=64, le=8192)
    buffer_seconds: float = Field(
        default=15.0, ge=1.0, le=120.0, description="Size of the rolling capture buffer."
    )
    clip_seconds: float = Field(
        default=8.0, ge=3.0, le=20.0, description="Length of audio sent to the recognizer."
    )

    @field_validator("clip_seconds")
    @classmethod
    def _clip_fits_buffer(cls, v: float, info: Any) -> float:
        buffer_seconds = info.data.get("buffer_seconds")
        if buffer_seconds is not None and v > buffer_seconds:
            raise ValueError("clip_seconds cannot exceed buffer_seconds")
        return v


class DetectConfig(_Base):
    """Thresholds for deciding when music is playing in the room.

    Tune these with ``now-spinning calibrate`` while the turntable is running --
    the right numbers depend entirely on mic gain and how far away it sits.
    """

    frame_seconds: float = Field(default=0.5, ge=0.05, le=2.0)
    start_threshold_dbfs: float = Field(default=-45.0, le=0.0)
    silence_threshold_dbfs: float = Field(default=-50.0, le=0.0)
    max_flatness: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Spectral flatness above this reads as broadband noise, not music.",
    )
    start_seconds: float = Field(
        default=2.0, ge=0.0, description="Sustained music required before the gate opens."
    )
    silence_seconds: float = Field(
        default=20.0, ge=1.0, description="Sustained quiet required before the gate closes."
    )

    @field_validator("silence_threshold_dbfs")
    @classmethod
    def _silence_below_start(cls, v: float, info: Any) -> float:
        start = info.data.get("start_threshold_dbfs")
        if start is not None and v > start:
            raise ValueError("silence_threshold_dbfs must be <= start_threshold_dbfs (hysteresis)")
        return v


class RecognizerConfig(_Base):
    """How often to ask the provider, and how long results stay on screen."""

    provider: Literal["shazam", "fake"] = "shazam"
    timeout_seconds: float = Field(default=30.0, ge=5.0)
    quiet_period_seconds: float = Field(
        default=60.0,
        ge=0.0,
        description="After a match, wait this long before asking again. Keeps request volume low.",
    )
    recheck_interval_seconds: float = Field(
        default=45.0, ge=10.0, description="Re-identify at this cadence to catch track changes."
    )
    backoff_initial_seconds: float = Field(default=10.0, ge=1.0)
    backoff_max_seconds: float = Field(default=120.0, ge=1.0)
    linger_seconds: float = Field(
        default=90.0,
        ge=0.0,
        description="Keep the last track on screen after music stops (covers a side flip).",
    )
    stale_after_seconds: float = Field(
        default=300.0,
        ge=0.0,
        description="Clear an un-reconfirmed track after this long, even if music continues.",
    )


class DisplayConfig(_Base):
    """Look and feel of the record animation."""

    backend: Literal["pygame", "web", "both", "none"] = "pygame"
    style: Literal["sleeve", "record"] = Field(
        default="sleeve",
        description=(
            "'sleeve' shows the cover in a record sleeve with the disc peeking out; "
            "'record' shows the cover on a spinning platter."
        ),
    )
    fullscreen: bool = True
    width: int = Field(default=0, ge=0, description="0 = use the display's native size.")
    height: int = Field(default=0, ge=0)
    fps: int = Field(default=30, ge=5, le=60)
    rpm: float = Field(
        default=33.333, gt=0.0, description="Record rotation speed. 45.0 for a single."
    )
    show_cursor: bool = False
    device_index: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Which /dev/dri/card to render on. Only needed when more than one exists "
            "-- a GPIO/SPI panel usually lands on card1 while HDMI holds card0, and "
            "SDL would otherwise pick card0 and leave the panel black."
        ),
    )
    background: str = "#101014"
    foreground: str = "#f5f2ea"
    accent: str = "#c8a24a"
    font_path: str | None = None


class WebConfig(_Base):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)


class LoggingConfig(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Config(_Base):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    recognizer: RecognizerConfig = Field(default_factory=RecognizerConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    cache_dir: Path = Field(default_factory=default_cache_dir)


def find_config_file() -> Path | None:
    """Return the first config file on the search path, or None if there is no file."""
    for candidate in CONFIG_SEARCH_PATH:
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    """Load configuration from ``path``, or from the search path, or from defaults.

    Raises FileNotFoundError only when an explicit path was given and does not exist --
    a missing file on the search path just means "use defaults".
    """
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
    else:
        path = find_config_file()

    if path is None:
        return Config()

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a YAML mapping: {path}")
    return Config.model_validate(raw)
