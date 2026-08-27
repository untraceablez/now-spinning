"""Command line entry point.

``run`` is the daemon; the other three subcommands exist because the two things
that actually go wrong on a new install are "the mic is not the device you think
it is" and "the thresholds are wrong for this room". ``devices``, ``calibrate``,
and ``identify`` each answer one of those without starting the display.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from nowspinning import __version__
from nowspinning.artwork import ArtworkCache
from nowspinning.audio.capture import AudioCapture, AudioError, list_input_devices
from nowspinning.audio.clip import write_wav
from nowspinning.audio.detect import GateEvent, MusicGate, analyze
from nowspinning.config import Config, load_config
from nowspinning.engine import Engine
from nowspinning.recognize import build_recognizer
from nowspinning.recognize.base import Recognizer, RecognizerError
from nowspinning.recognize.fake import demo_tracks
from nowspinning.state import StateStore

log = logging.getLogger("nowspinning")

DEMO_TRACK_SECONDS = 25.0


# ---------------------------------------------------------------- plumbing


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def config_from_args(args: argparse.Namespace) -> Config:
    """Load the config file, then layer on the flags that override it."""
    config = load_config(Path(args.config) if args.config else None)
    if getattr(args, "device", None) is not None:
        config.audio.device = args.device
    if getattr(args, "backend", None):
        config.display.backend = args.backend
    if getattr(args, "windowed", False):
        config.display.fullscreen = False
    if getattr(args, "port", None):
        config.web.port = args.port
    if getattr(args, "log_level", None):
        config.logging.level = args.log_level.upper()
    if getattr(args, "demo", False):
        config.recognizer.provider = "fake"
    return config


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.info("received %s; shutting down", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):  # not the main thread
            signal.signal(sig, handler)


# ------------------------------------------------------------------- run


async def _demo_loop(store: StateStore, stop: threading.Event) -> None:
    """Cycle placeholder tracks so the display can be worked on with no mic."""
    tracks = demo_tracks()
    index = 0
    store.update(status="listening", message="Demo mode - no microphone in use")
    while not stop.is_set():
        track = tracks[index % len(tracks)]
        store.update(status="playing", track=track, artwork_path=None, message=None)
        index += 1
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(stop.wait), DEMO_TRACK_SECONDS)


async def _async_side(
    config: Config,
    store: StateStore,
    artwork: ArtworkCache,
    capture: AudioCapture | None,
    recognizer: Recognizer | None,
    stop: threading.Event,
    demo: bool,
) -> None:
    """Everything that lives on the event loop: the engine, and optionally the web server."""
    tasks: list[asyncio.Task[Any]] = []
    engine: Engine | None = None
    server: Any = None

    if demo:
        tasks.append(asyncio.create_task(_demo_loop(store, stop), name="demo"))
    else:
        assert capture is not None and recognizer is not None
        engine = Engine(config, capture, recognizer, store, artwork)
        tasks.append(asyncio.create_task(engine.run(), name="engine"))

    if config.display.backend in ("web", "both"):
        from nowspinning.ui.web.app import build_server

        server = build_server(config, store, artwork)
        tasks.append(asyncio.create_task(server.serve(), name="web"))
        log.info("web display on http://%s:%d", config.web.host, config.web.port)

    waiter = asyncio.create_task(asyncio.to_thread(stop.wait), name="stop")
    try:
        await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        if engine is not None:
            engine.stop()
        if server is not None:
            server.should_exit = True
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5.0)
            if not task.done():
                task.cancel()
        waiter.cancel()
        store.close()
        await artwork.aclose()
        if recognizer is not None:
            await recognizer.aclose()


def cmd_run(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    setup_logging(config.logging.level)
    log.info("now-spinning %s starting (backend=%s)", __version__, config.display.backend)

    demo = bool(args.demo)
    store = StateStore()
    artwork = ArtworkCache(config.cache_dir)
    stop = threading.Event()
    install_signal_handlers(stop)

    capture: AudioCapture | None = None
    recognizer: Recognizer | None = None
    if not demo:
        capture = AudioCapture(config.audio)
        try:
            capture.start()
        except AudioError as exc:
            log.error("%s", exc)
            log.error("run 'now-spinning devices' to see what is available")
            return 2
        recognizer = build_recognizer(config)

    def async_side() -> None:
        try:
            asyncio.run(_async_side(config, store, artwork, capture, recognizer, stop, demo))
        except Exception:
            log.exception("background loop failed")
        finally:
            stop.set()

    try:
        if config.display.backend in ("pygame", "both"):
            # SDL insists on the main thread, so the event loop moves to a worker.
            worker = threading.Thread(target=async_side, name="nowspinning-loop", daemon=True)
            worker.start()
            from nowspinning.ui.pygame_display import PygameDisplay

            try:
                PygameDisplay(config, store).run(stop)
            finally:
                stop.set()
                worker.join(timeout=10.0)
        else:
            async_side()
    finally:
        if capture is not None:
            capture.stop()
    return 0


# --------------------------------------------------------------- devices


def cmd_devices(_args: argparse.Namespace) -> int:
    try:
        devices = list_input_devices()
    except AudioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not devices:
        print("no input devices found", file=sys.stderr)
        return 1
    print("Input devices:")
    for device in devices:
        print(f"  {device.describe()}")
    print("\nSet the one you want with 'audio.device' in your config, or --device.")
    print("A name substring works too, and survives the cards being renumbered.")
    return 0


# ------------------------------------------------------------- calibrate


def cmd_calibrate(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    setup_logging("WARNING")
    detect = config.detect

    print(
        f"Watching input. Music opens the gate at >= {detect.start_threshold_dbfs:.0f} dBFS "
        f"with flatness <= {detect.max_flatness:.2f}.\n"
        "Play a record, note the levels, then set the thresholds a few dB below "
        "what you see.\nCtrl-C to stop.\n"
    )
    gate = MusicGate(detect)
    capture = AudioCapture(config.audio)
    try:
        capture.start()
    except AudioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Logging is turned down here so the meter stays readable, but the negotiated
    # format is worth seeing: it is often not the one in the config file.
    print(f"Capturing at {capture.sample_rate} Hz, {capture.channels} ch.\n")

    try:
        while True:
            time.sleep(detect.frame_seconds)
            frame = capture.snapshot(detect.frame_seconds)
            if frame.size == 0:
                continue
            stats = analyze(frame, capture.sample_rate)
            event = gate.observe_stats(stats, time.monotonic())
            bar = "#" * max(0, min(40, int((stats.level_dbfs + 80.0) / 2.0)))
            marker = "MUSIC " if gate.is_open else "quiet "
            note = f"  <- {event.value}" if event is not GateEvent.NONE else ""
            print(
                f"\r{marker} {stats.level_dbfs:7.1f} dBFS  flat {stats.flatness:4.2f} "
                f"|{bar:<40}|{note}",
                end="" if not note else "\n",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        capture.stop()


# -------------------------------------------------------------- identify


def cmd_identify(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    setup_logging(config.logging.level)
    seconds = args.seconds or config.audio.clip_seconds

    capture = AudioCapture(config.audio)
    try:
        capture.start()
    except AudioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        print(f"Recording {seconds:.0f}s...", file=sys.stderr)
        deadline = time.monotonic() + seconds + 0.5
        while time.monotonic() < deadline:
            time.sleep(0.2)
        samples = capture.snapshot(seconds)
    finally:
        capture.stop()

    stats = analyze(samples, capture.sample_rate)
    print(
        f"Captured {samples.size / capture.sample_rate:.1f}s at "
        f"{stats.level_dbfs:.1f} dBFS (flatness {stats.flatness:.2f})",
        file=sys.stderr,
    )

    default_out = Path(config.cache_dir) / "clips" / "identify.wav"
    out_path = Path(args.output) if args.output else default_out
    write_wav(samples, capture.sample_rate, out_path)
    print(f"Wrote {out_path}", file=sys.stderr)

    recognizer = build_recognizer(config)
    try:
        track = asyncio.run(_identify_once(recognizer, out_path))
    except RecognizerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if track is None:
        print("null")
        print("No match. Try more volume, a closer mic, or a longer clip.", file=sys.stderr)
        return 1
    print(json.dumps(track.to_dict(), indent=2))
    return 0


async def _identify_once(recognizer: Recognizer, path: Path) -> Any:
    try:
        return await recognizer.identify(path)
    finally:
        await recognizer.aclose()


# ----------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="now-spinning",
        description="Listen to a record player and show what is playing.",
    )
    parser.add_argument("--version", action="version", version=f"now-spinning {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", help="path to a config file")
        p.add_argument("-d", "--device", help="input device index or name substring")
        p.add_argument(
            "--log-level",
            choices=["debug", "info", "warning", "error"],
            help="override the configured log level",
        )

    run = sub.add_parser("run", help="listen and drive the display (the main command)")
    common(run)
    run.add_argument("--backend", choices=["pygame", "web", "both", "none"], help="renderer to use")
    run.add_argument("--windowed", action="store_true", help="do not go fullscreen")
    run.add_argument("--port", type=int, help="port for the web display")
    run.add_argument(
        "--demo",
        action="store_true",
        help="cycle placeholder tracks without touching the microphone",
    )
    run.set_defaults(func=cmd_run)

    devices = sub.add_parser("devices", help="list microphones PortAudio can see")
    devices.set_defaults(func=cmd_devices)

    calibrate = sub.add_parser("calibrate", help="live level meter for tuning the music gate")
    common(calibrate)
    calibrate.set_defaults(func=cmd_calibrate)

    identify = sub.add_parser("identify", help="record one clip and print what it matched")
    common(identify)
    identify.add_argument("-s", "--seconds", type=float, help="clip length to record")
    identify.add_argument("-o", "--output", help="where to write the captured WAV")
    identify.set_defaults(func=cmd_identify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except KeyboardInterrupt:
        return 130
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
