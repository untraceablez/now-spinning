"""Microphone capture, music detection, and clip encoding."""

from nowspinning.audio.detect import FrameStats, GateEvent, MusicGate, analyze, dbfs, rms

__all__ = ["FrameStats", "GateEvent", "MusicGate", "analyze", "dbfs", "rms"]
