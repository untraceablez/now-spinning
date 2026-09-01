"""Where things sit inside the sleeve artwork.

Every value is a fraction of ``sleeve.png``, so it holds at any size. They live
here rather than in the pygame renderer because the web page lays out the same
composition and has to agree with it exactly -- and because importing the pygame
renderer to read a number would drag SDL into a web-only install.

All of it is measured from the asset rather than eyeballed; the comments say how.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).with_name("assets")

#: The cover's window inside sleeve.png: x, y, width, height. From the original
#: theme's stylesheet -- a 355x355 window at (27, 14) in a 453x387 sheet, which
#: is why the artwork reads as square despite the sleeve being taller than wide.
ART_WINDOW = (27 / 453, 14 / 387, 355 / 453, 355 / 387)

#: The record, fitted from the artwork's opaque pixels: centre (274.2, 194.0)
#: and radius 172.6, which reproduces every measured column to within a pixel.
DISC_CENTRE = (274.2 / 453, 194.0 / 387)
DISC_RADIUS = 172.6 / 453

#: Where the jacket ends and the record begins. Everything left of it is jacket,
#: everything right is record, which is what lets the two be drawn -- or not --
#: independently. It is also the left edge of the visible crescent of record.
SLEEVE_RIGHT = 377 / 453

#: The composition to centre and scale by: the cover, plus the record when it is
#: shown. Deliberately not the image's alpha bounds -- the jacket's shadow used
#: to reach further left than right, which pulled the cover off centre.
COVER_LEFT = ART_WINDOW[0]
COVER_TOP = ART_WINDOW[1]
COVER_RIGHT = ART_WINDOW[0] + ART_WINDOW[2]
COVER_BOTTOM = ART_WINDOW[1] + ART_WINDOW[3]
DISC_EDGE = DISC_CENTRE[0] + DISC_RADIUS

#: Breathing room left around the artwork when it has the panel to itself, as a
#: fraction of the shorter side. Enough to not look cropped.
ARTWORK_ONLY_MARGIN = 0.04


#: The artwork's own pixel size. The browser needs it because the fractions above
#: are normalised against different axes -- DISC_RADIUS against the width, the
#: disc's centre y against the height -- and mixing the two silently misplaces
#: things. Working in these pixels and converting once is unambiguous.
IMAGE_SIZE = (453, 387)


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
