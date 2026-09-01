"""Argument parsing and the flag-over-file override order."""

from __future__ import annotations

import logging
import threading

import pytest

from nowspinning.audio.capture import AudioError, DeviceInfo
from nowspinning.cli import build_parser, cmd_devices, config_from_args


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit):
        parse([])


@pytest.mark.parametrize("command", ["run", "devices", "calibrate", "identify"])
def test_every_subcommand_parses(command):
    assert parse([command]).command == command


def test_run_defaults_to_the_configured_backend():
    config = config_from_args(parse(["run"]))
    assert config.display.backend == "pygame"
    assert config.audio.device is None


def test_flags_override_the_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("display:\n  backend: pygame\naudio:\n  device: 3\nweb:\n  port: 9000\n")
    args = parse(
        ["run", "--config", str(path), "--backend", "web", "--device", "USB", "--port", "9999"]
    )
    config = config_from_args(args)
    assert config.display.backend == "web"
    assert config.audio.device == "USB"
    assert config.web.port == 9999


def test_windowed_disables_fullscreen():
    assert config_from_args(parse(["run", "--windowed"])).display.fullscreen is False


def test_demo_selects_the_fake_provider():
    assert config_from_args(parse(["run", "--demo"])).recognizer.provider == "fake"


def test_log_level_flag_is_normalized():
    assert config_from_args(parse(["run", "--log-level", "debug"])).logging.level == "DEBUG"


def test_identify_accepts_a_clip_length():
    args = parse(["identify", "--seconds", "12", "--output", "/tmp/clip.wav"])
    assert args.seconds == 12.0
    assert args.output == "/tmp/clip.wav"


def test_devices_lists_what_it_finds(monkeypatch, capsys):
    monkeypatch.setattr(
        "nowspinning.cli.list_input_devices",
        lambda: [DeviceInfo(1, "USB Audio", 1, 48000.0, is_default=True)],
    )
    assert cmd_devices(parse(["devices"])) == 0
    assert "USB Audio" in capsys.readouterr().out


def test_devices_reports_a_missing_portaudio(monkeypatch, capsys):
    def boom():
        raise AudioError("PortAudio is not available")

    monkeypatch.setattr("nowspinning.cli.list_input_devices", boom)
    assert cmd_devices(parse(["devices"])) == 2
    assert "PortAudio" in capsys.readouterr().err


def test_devices_says_so_when_there_are_none(monkeypatch, capsys):
    monkeypatch.setattr("nowspinning.cli.list_input_devices", list)
    assert cmd_devices(parse(["devices"])) == 1
    assert "no input devices" in capsys.readouterr().err


class TestWebBackendReporting:
    """The `web:` config section does not, on its own, start a server."""

    def _log(self, caplog, backend, tmp_path):
        import asyncio

        from nowspinning.artwork import ArtworkCache
        from nowspinning.cli import _async_side
        from nowspinning.config import Config
        from nowspinning.state import StateStore

        config = Config()
        config.cache_dir = tmp_path
        config.display.backend = backend
        config.web.port = 8123  # never bound: stop is already set
        stop = threading.Event()
        stop.set()  # return immediately; only the startup lines are of interest
        with caplog.at_level(logging.INFO, logger="nowspinning"):
            asyncio.run(
                _async_side(
                    config,
                    StateStore(),
                    ArtworkCache(config.cache_dir),
                    None,
                    None,
                    stop,
                    demo=True,
                )
            )
        return caplog.text

    def test_it_says_when_the_web_display_is_off(self, caplog, tmp_path):
        # The section configures a server that pygame-only never starts, so it
        # reads as though it enables one. Silence there costs an afternoon.
        text = self._log(caplog, "pygame", tmp_path)
        assert "web display off" in text
        assert "'web' or 'both'" in text

    def test_it_says_where_the_web_display_is_when_on(self, caplog, tmp_path):
        assert "web display on http://" in self._log(caplog, "web", tmp_path)
