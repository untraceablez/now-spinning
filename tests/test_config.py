"""Config has to be forgiving about what is absent and strict about what is wrong."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nowspinning.config import Config, load_config


def test_defaults_are_usable_with_no_file():
    config = Config()
    assert config.audio.sample_rate == 16000
    assert config.display.backend == "pygame"
    assert config.recognizer.provider == "shazam"
    assert config.detect.silence_threshold_dbfs <= config.detect.start_threshold_dbfs


def test_partial_file_only_overrides_named_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("display:\n  backend: web\n  rpm: 45.0\n")
    config = load_config(path)
    assert config.display.backend == "web"
    assert config.display.rpm == 45.0
    assert config.display.fullscreen is True  # untouched default
    assert config.audio.sample_rate == 16000


def test_empty_file_yields_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert load_config(path).display.backend == "pygame"


def test_missing_explicit_path_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_no_file_anywhere_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr("nowspinning.config.CONFIG_SEARCH_PATH", (tmp_path / "absent.yaml",))
    assert load_config() == Config()


def test_non_mapping_file_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- one\n- two\n")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(path)


def test_unknown_keys_are_rejected(tmp_path):
    """A silently ignored typo in a threshold would be very hard to debug in a room."""
    path = tmp_path / "config.yaml"
    path.write_text("detect:\n  start_treshold_dbfs: -30\n")
    with pytest.raises(ValidationError):
        load_config(path)


def test_hysteresis_must_be_the_right_way_round():
    with pytest.raises(ValidationError, match="hysteresis"):
        Config.model_validate(
            {"detect": {"start_threshold_dbfs": -60, "silence_threshold_dbfs": -40}}
        )


def test_clip_cannot_be_longer_than_the_buffer():
    with pytest.raises(ValidationError, match="clip_seconds"):
        Config.model_validate({"audio": {"buffer_seconds": 5.0, "clip_seconds": 12.0}})


def test_example_config_is_valid_and_matches_defaults():
    """config.example.yaml is documentation; if it drifts, the docs are wrong."""
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.yaml"
    loaded = load_config(example)
    assert loaded.model_dump(exclude={"cache_dir"}) == Config().model_dump(exclude={"cache_dir"})
