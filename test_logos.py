#!/usr/bin/env python3
"""Offline checks for the F1 team logos as the panel draws them (f1-live).

The clean-up asked for on 2026-09-12: margins cropped, the Sauber art reduced to
its green K, the white tiles behind Ferrari and Haas removed, edges hardened, and
Cadillac left as it was. Uses the bundled PNGs, so run it on the Pi:

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_logos.py
"""
from __future__ import annotations

import logging
import os
import sys

CORE = "/home/admin/LEDMatrix"
PLUGIN = os.environ.get("F1_LIVE_PLUGIN", CORE + "/plugin-repos/f1-live")
sys.path.insert(0, PLUGIN)
sys.path.insert(0, CORE)
if os.path.isdir(CORE):
    os.chdir(CORE)
logging.disable(logging.CRITICAL)

from PIL import Image  # noqa: E402
import logo_downloader as LD  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


L = LD.F1LogoLoader(PLUGIN)
SIZE = 24          # about the result row's logo slot on a 32 px panel


def logo(cid):
    return L._load_logo(cid, SIZE, SIZE)


def plain(cid):
    """What the loader drew before 2026-09-12: the whole PNG, thumbnailed."""
    img = Image.open(L.teams_dir / (cid + ".png")).convert("RGBA")
    img.thumbnail((SIZE, SIZE), Image.Resampling.LANCZOS)
    return img


def ink(img):
    """Pixels the panel draws at full or near-full strength. (A bounding box
    would not do: the resample leaves near-invisible pixels past the mark's
    edge that stretch the old version's box.)"""
    return sum(1 for a in img.getchannel("A").getdata() if a >= 128)


def white_share(img):
    px = list(img.convert("RGBA").getdata())
    return sum(1 for r, g, b, a in px if a > 128 and min(r, g, b) > 200) / float(len(px))


def test_every_logo_fits_fills_and_is_crisp():
    for path in sorted(L.teams_dir.glob("*.png")):
        cid, img = path.stem, logo(path.stem)
        check("%s fits %dx%d" % (cid, SIZE, SIZE), img.width <= SIZE and img.height <= SIZE,
              img.size)
        if cid in LD._NO_HARDEN:
            continue
        check("%s fills its slot on its long side" % cid, max(img.size) == SIZE, img.size)
        alpha = set(img.getchannel("A").getdata())
        check("%s has no half-transparent fringe" % cid, alpha <= {0, 255}, sorted(alpha)[:6])
        if cid not in LD._TILE_SEEDS and cid not in LD._KEEP_ONLY:
            check("%s draws at least as much of its mark as before" % cid,
                  ink(img) >= ink(plain(cid)), (ink(plain(cid)), ink(img)))


def test_sauber_is_just_the_green_k():
    lit = [p for p in logo("sauber").getdata() if p[3]]
    check("sauber: something is drawn", len(lit) > 50, len(lit))
    check("sauber: every lit pixel is green -- no wordmark, no ring",
          all(g > r and g > b for r, g, b, a in lit),
          [p for p in lit if not (p[1] > p[0] and p[1] > p[2])][:3])
    audi = L.get_team_logo("Audi", SIZE, SIZE)
    check("the 2026 Audi team gets the same K", audi.tobytes() == logo("sauber").tobytes())


def test_ferrari_and_haas_lose_their_white_tile():
    # What stays white is the logo's own: the tricolour stripe on Ferrari's
    # shield, and the inside of Haas's H circle.
    for cid in ("ferrari", "haas"):
        before, after = white_share(plain(cid)), white_share(logo(cid))
        check("%s: the white tile is gone (white %.0f%% -> %.0f%%)"
              % (cid, 100 * before, 100 * after),
              before > 0.25 and after < min(0.25, before * 0.6), (before, after))


def test_wide_marks_get_bigger():
    for cid in ("aston_martin", "red_bull", "mclaren", "alpine"):
        check("%s: more of the slot is logo" % cid, ink(logo(cid)) > ink(plain(cid)),
              (ink(plain(cid)), ink(logo(cid))))


def test_cadillac_is_left_as_it_was():
    check("cadillac: unchanged", logo("cadillac").tobytes() == plain("cadillac").tobytes())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("-- " + name)
            fn()
    print("\n%d failed" % len(failures) if failures else "\nALL PASS")
    sys.exit(1 if failures else 0)
