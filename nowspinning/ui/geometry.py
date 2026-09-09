"""Where things sit in the record display composition.

The display uses separate component assets (case, tab, vinyl) rather than a
composite, and lays them out as layers. The geometry here positions them
relative to the composition's origin.

All positioning is in pixels or as fractions of the composition size, measured
from the assets rather than eyeballed.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).with_name("assets")

#: The cover's window inside the case: x, y, width, height (in pixels).
#: Artwork fills most of the case with minimal padding.
CASE_SIZE = (600, 600)
CASE_ART_WINDOW = (30, 30, 540, 540)  # x, y, width, height in pixels

#: The vinyl record dimensions (in pixels).
VINYL_SIZE = (578, 578)

#: The composition bounds: just the case size (no tab).
COMPOSITION_SIZE = (600, 600)

#: Position of each component within the composition (in pixels).
CASE_POS = (0, 0)  # Top-left of the case
#: Vinyl offset to the right with slight top offset
VINYL_POS = (50, 11)  # Offset ~50px right for positioning

#: Scale factor applied to all components (10% reduction for padding).
COMPONENT_SCALE = 0.9

#: Convert to fractions for web layout and other uses
CASE_LEFT = CASE_POS[0] / COMPOSITION_SIZE[0]
CASE_TOP = CASE_POS[1] / COMPOSITION_SIZE[1]
CASE_RIGHT = (CASE_POS[0] + CASE_SIZE[0]) / COMPOSITION_SIZE[0]
CASE_BOTTOM = (CASE_POS[1] + CASE_SIZE[1]) / COMPOSITION_SIZE[1]

VINYL_LEFT = VINYL_POS[0] / COMPOSITION_SIZE[0]
VINYL_TOP = VINYL_POS[1] / COMPOSITION_SIZE[1]
VINYL_RIGHT = (VINYL_POS[0] + VINYL_SIZE[0]) / COMPOSITION_SIZE[0]
VINYL_BOTTOM = (VINYL_POS[1] + VINYL_SIZE[1]) / COMPOSITION_SIZE[1]

#: Cover window bounds (derived from case window, in fractions of composition).
ART_WINDOW_LEFT = CASE_ART_WINDOW[0] / COMPOSITION_SIZE[0]
ART_WINDOW_TOP = CASE_ART_WINDOW[1] / COMPOSITION_SIZE[1]
ART_WINDOW_WIDTH = CASE_ART_WINDOW[2] / COMPOSITION_SIZE[0]
ART_WINDOW_HEIGHT = CASE_ART_WINDOW[3] / COMPOSITION_SIZE[1]
ART_WINDOW = (ART_WINDOW_LEFT, ART_WINDOW_TOP, ART_WINDOW_WIDTH, ART_WINDOW_HEIGHT)

#: Vinyl disc center and radius (in fractions of composition).
#: The vinyl is a 578x578 circle centered at (289, 289) in its own space.
VINYL_CENTER_X = (VINYL_POS[0] + VINYL_SIZE[0] / 2) / COMPOSITION_SIZE[0]
VINYL_CENTER_Y = (VINYL_POS[1] + VINYL_SIZE[1] / 2) / COMPOSITION_SIZE[1]
VINYL_RADIUS = (VINYL_SIZE[0] / 2) / COMPOSITION_SIZE[0]
DISC_CENTRE = (VINYL_CENTER_X, VINYL_CENTER_Y)
DISC_RADIUS = VINYL_RADIUS

#: For compatibility with web layout.
COVER_LEFT = ART_WINDOW_LEFT
COVER_TOP = ART_WINDOW_TOP
COVER_RIGHT = ART_WINDOW_LEFT + ART_WINDOW_WIDTH
COVER_BOTTOM = ART_WINDOW_TOP + ART_WINDOW_HEIGHT

#: The point where the case ends.
SLEEVE_RIGHT = CASE_RIGHT

#: The rightmost edge of the composition (no tab).
DISC_EDGE = CASE_RIGHT

#: Breathing room around artwork when displayed alone.
ARTWORK_ONLY_MARGIN = 0.04

#: The composition's pixel size (used for scaling).
IMAGE_SIZE = COMPOSITION_SIZE


def as_dict() -> dict[str, object]:
    """The geometry as JSON, for the browser to lay out the same composition."""
    return {
        "image_size": list(IMAGE_SIZE),
        "art_window": list(ART_WINDOW),
        "disc_centre": list(DISC_CENTRE),
        "disc_radius": DISC_RADIUS,
        "sleeve_right": SLEEVE_RIGHT,
        "cover_left": COVER_LEFT,
        "cover_top": COVER_TOP,
        "cover_right": COVER_RIGHT,
        "cover_bottom": COVER_BOTTOM,
        "disc_edge": DISC_EDGE,
        "artwork_only_margin": ARTWORK_ONLY_MARGIN,
    }
