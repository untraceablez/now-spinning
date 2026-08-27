"""Continuous microphone capture into a rolling in-memory buffer.

PortAudio calls back on its own high-priority thread, so the callback does nothing
but copy into a ring buffer. Everything else -- analysis, encoding, recognition --
reads snapshots out of that buffer and never blocks the audio thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from nowspinning.config import AudioConfig

log = logging.getLogger(__name__)


class AudioError(RuntimeError):
    """The microphone could not be opened or read."""


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    default_sample_rate: float
    is_default: bool = False

    def describe(self) -> str:
        marker = " (default)" if self.is_default else ""
        return (
            f"[{self.index}] {self.name}{marker} - "
            f"{self.max_input_channels} in @ {self.default_sample_rate:.0f} Hz"
        )


def _sounddevice() -> Any:
    """Import sounddevice on demand.

    Deferred so that importing this module -- which the CLI and the tests do --
    does not require PortAudio to be installed.
    """
    try:
        import sounddevice
    except OSError as exc:  # pragma: no cover - depends on the host
        raise AudioError(
            "PortAudio is not available. On Debian/Raspberry Pi OS: sudo apt install libportaudio2"
        ) from exc
    return sounddevice


def list_input_devices() -> list[DeviceInfo]:
    """Every capture device PortAudio can see."""
    sd = _sounddevice()
    try:
        default_input = sd.default.device[0]
    except (TypeError, IndexError):  # pragma: no cover - platform dependent
        default_input = None

    devices = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        devices.append(
            DeviceInfo(
                index=index,
                name=str(dev["name"]),
                max_input_channels=int(dev["max_input_channels"]),
                default_sample_rate=float(dev["default_samplerate"]),
                is_default=(index == default_input),
            )
        )
    return devices


def resolve_device(device: int | str | None) -> int | None:
    """Turn a config ``device`` value into a PortAudio index.

    Accepts an index directly, or a case-insensitive substring of a device name so
    a config file can say ``device: "USB Audio"`` and survive a reboot renumbering
    the cards.
    """
    if device is None or isinstance(device, int):
        return device

    text = device.strip()
    if text.isdigit():
        return int(text)

    needle = text.casefold()
    matches = [d for d in list_input_devices() if needle in d.name.casefold()]
    if not matches:
        available = ", ".join(d.name for d in list_input_devices()) or "none"
        raise AudioError(f"no input device matching {device!r}. Available: {available}")
    if len(matches) > 1:
        log.warning(
            "device %r matched %d devices; using %s",
            device,
            len(matches),
            matches[0].describe(),
        )
    return matches[0].index


class AudioCapture:
    """A rolling buffer of the most recent ``buffer_seconds`` of microphone audio."""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.sample_rate = config.sample_rate
        self._capacity = max(1, int(config.buffer_seconds * config.sample_rate))
        self._buffer = np.zeros(self._capacity, dtype=np.float32)
        self._write = 0
        self._filled = 0
        self._lock = threading.Lock()
        self._stream: Any = None
        self._overflows = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._stream is not None:
            return
        sd = _sounddevice()
        device = resolve_device(self.config.device)
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.config.block_size,
                device=device,
                channels=self.config.channels,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # sounddevice raises several unrelated types
            self._stream = None
            raise AudioError(f"could not open audio input device {device!r}: {exc}") from exc
        log.info(
            "capturing from device %s at %d Hz (%.0fs buffer)",
            device if device is not None else "default",
            self.sample_rate,
            self.config.buffer_seconds,
        )

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        if self._overflows:
            log.debug("audio input reported %d overflow(s)", self._overflows)

    def __enter__(self) -> AudioCapture:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- the audio thread ------------------------------------------------

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            self._overflows += 1
        mono = indata[:, 0] if indata.ndim > 1 else indata
        self.write(np.asarray(mono, dtype=np.float32))

    def write(self, samples: np.ndarray) -> None:
        """Append samples to the ring buffer. Also the seam tests write through."""
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        if data.size == 0:
            return
        if data.size >= self._capacity:
            data = data[-self._capacity :]

        with self._lock:
            end = self._write + data.size
            if end <= self._capacity:
                self._buffer[self._write : end] = data
            else:
                split = self._capacity - self._write
                self._buffer[self._write :] = data[:split]
                self._buffer[: end - self._capacity] = data[split:]
            self._write = end % self._capacity
            self._filled = min(self._capacity, self._filled + data.size)

    # -- readers ---------------------------------------------------------

    @property
    def available_seconds(self) -> float:
        with self._lock:
            return self._filled / self.sample_rate

    def snapshot(self, seconds: float) -> np.ndarray:
        """The most recent ``seconds`` of audio, oldest sample first.

        Returns fewer samples than requested when the buffer has not filled yet;
        callers check the length rather than blocking.
        """
        wanted = int(seconds * self.sample_rate)
        with self._lock:
            count = min(wanted, self._filled)
            if count == 0:
                return np.zeros(0, dtype=np.float32)
            start = (self._write - count) % self._capacity
            end = start + count
            if end <= self._capacity:
                return self._buffer[start:end].copy()
            head = self._buffer[start:].copy()
            tail = self._buffer[: end - self._capacity].copy()
            return np.concatenate((head, tail))
