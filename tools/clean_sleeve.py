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


def clean(source: pygame.Surface) -> pygame.Surface:
    width, height = source.get_size()
    out = source.copy()
    inner = CLEAN_X - CX
    for x in range(PAPER_FROM, CLEAN_X):
        for y in range(height):
            radius = math.hypot(x - CX, y - CY)
            if radius > R:
                out.set_at((x, y), (0, 0, 0, 0))  # past the record's edge
                continue
            sx = min(width - 1, round(CX + max(radius, inner)))
            out.set_at((x, y), source.get_at((sx, int(CY))))
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
