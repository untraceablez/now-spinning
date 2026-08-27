"""Parsing recorded responses -- the layer most likely to drift if upstream changes.

Album is genuinely optional in Shazam responses and artwork sometimes only comes
in the low-resolution size, so both have to survive being absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nowspinning.recognize.shazam import parse_response

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_full_match_is_parsed():
    track = parse_response(load("shazam_match.json"))
    assert track is not None
    assert track.title == "Blue In Green"
    assert track.artist == "Miles Davis"
    assert track.album == "Kind of Blue"
    assert track.artwork_url == "https://images.example.invalid/800x800cc.jpg"
    assert track.provider_id == "40333609"
    assert track.provider == "shazam"


def test_no_match_returns_none():
    assert parse_response(load("shazam_no_match.json")) is None


def test_sparse_match_survives_missing_album_and_artist():
    track = parse_response(load("shazam_sparse_match.json"))
    assert track is not None
    assert track.title == "Untitled Acetate"
    assert track.artist == "Unknown artist"
    assert track.album is None
    assert track.artwork_url == "https://images.example.invalid/400x400cc.jpg"


def test_album_lookup_is_case_insensitive():
    data = load("shazam_match.json")
    data["track"]["sections"][0]["metadata"][0]["title"] = "ALBUM"
    assert parse_response(data).album == "Kind of Blue"


def test_blank_album_text_is_treated_as_missing():
    data = load("shazam_match.json")
    data["track"]["sections"][0]["metadata"][0]["text"] = "   "
    assert parse_response(data).album is None


def test_high_resolution_artwork_is_preferred():
    data = load("shazam_match.json")
    assert parse_response(data).artwork_url.endswith("800x800cc.jpg")
    del data["track"]["images"]["coverarthq"]
    assert parse_response(data).artwork_url.endswith("400x400cc.jpg")


def test_non_http_artwork_is_ignored():
    data = load("shazam_match.json")
    data["track"]["images"] = {"coverarthq": "data:image/png;base64,AAAA"}
    assert parse_response(data).artwork_url is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        "nope",
        {"track": None},
        {"track": {}},
        {"track": {"title": "   ", "subtitle": "Somebody"}},
    ],
)
def test_malformed_responses_are_no_match(data):
    assert parse_response(data) is None


def test_odd_section_shapes_do_not_raise():
    data = load("shazam_match.json")
    data["track"]["sections"] = ["nonsense", {"metadata": "not a list"}, {"metadata": [None, 3]}]
    track = parse_response(data)
    assert track is not None and track.album is None
