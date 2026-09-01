#!/usr/bin/env python3
"""Rebuild the record across the paper inner sleeve in the artwork.

The "Massive Vinyl 2" jacket art draws a paper inner sleeve between the cover
and the record. At the size this project renders (480x320 and similar panels) it
stops reading as paper and becomes a grey block sitting on the record.

The record is drawn as concentric rings, so a pixel at radius r can be rebuilt
from any clean pixel at the same radius. The only angle with no paper over it is
straight out to the right, which covers every radius from CLEAN_X - CX outwards;
inside that the record is hidden by the jacket in the original too, so it is
flat-filled with the innermost clean ring.

Run from the repo root; rewrites nowspinning/ui/assets/sleeve.png in place.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

# Fitted from the asset's opaque pixels; reproduces every measured column to
# within a pixel. See DISC_CENTRE / DISC_RADIUS in ui/pygame_display.py.
CX, CY, R = 274.2, 194.0, 172.6
PAPER_FROM, CLEAN_X = 377, 406


def radial_profile(source: pygame.Surface) -> dict[int, tuple[int, int, int, int]]:
    """Median colour of the record at each whole-pixel radius.

    Sampling a single line through the centre is not good enough: that line
    crosses a bright highlight, so every rebuilt radius inherits it and the
    repair shows up as a vertical band. Taking the median across the whole clean
    arc at each radius averages the highlight out.
    """
    width, height = source.get_size()
    buckets: dict[int, list[tuple[int, int, int, int]]] = {}
    for x in range(CLEAN_X, width):
        for y in range(height):
            radius = math.hypot(x - CX, y - CY)
            if radius > R:
                continue
            buckets.setdefault(int(radius), []).append(tuple(source.get_at((x, y))))
    profile = {}
    for radius, pixels in buckets.items():
        channels = tuple(sorted(p[c] for p in pixels)[len(pixels) // 2] for c in range(4))
        profile[radius] = channels  # type: ignore[assignment]
    return profile


def clean(source: pygame.Surface) -> pygame.Surface:
    _, height = source.get_size()
    out = source.copy()
    profile = radial_profile(source)
    if not profile:
        raise RuntimeError("no clean record pixels found; check CLEAN_X and the geometry")
    inner, outer = min(profile), max(profile)
    for x in range(PAPER_FROM, CLEAN_X):
        for y in range(height):
            radius = math.hypot(x - CX, y - CY)
            if radius > R:
                out.set_at((x, y), (0, 0, 0, 0))  # past the record's edge
                continue
            # Radii inside `inner` are hidden by the jacket in the original too,
            # so there is nothing to copy: hold the innermost known ring.
            out.set_at((x, y), profile[min(max(int(radius), inner), outer)])
    return out


def main() -> int:
    target = Path("nowspinning/ui/assets/sleeve.png")
    if not target.is_file():
        print(f"{target} not found; run from the repository root", file=sys.stderr)
        return 1
    pygame.init()
    pygame.display.set_mode((64, 64))
    pygame.image.save(clean(pygame.image.load(str(target)).convert_alpha()), str(target))
    print(f"rewrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
