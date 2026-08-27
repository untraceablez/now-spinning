"""Music recognition providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nowspinning.recognize.base import Recognizer, RecognizerError, Track

if TYPE_CHECKING:
    from nowspinning.config import Config

__all__ = ["Recognizer", "RecognizerError", "Track", "build_recognizer"]


def build_recognizer(config: Config) -> Recognizer:
    """Construct the recognizer named by ``config.recognizer.provider``.

    Provider imports are deferred so a machine that only ever runs the fake
    provider never has to import the network client.
    """
    provider = config.recognizer.provider
    if provider == "shazam":
        from nowspinning.recognize.shazam import ShazamRecognizer

        return ShazamRecognizer(timeout=config.recognizer.timeout_seconds)
    if provider == "fake":
        from nowspinning.recognize.fake import FakeRecognizer

        return FakeRecognizer.demo()
    raise ValueError(f"unknown recognizer provider: {provider!r}")
