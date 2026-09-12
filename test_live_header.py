#!/usr/bin/env python3
"""Offline checks for the flag states of the F1 live header.

A red flag is design B, chosen by the user on 2026-09-12: the card keeps its
usual colours and RED FLAG sits in a solid red badge at the right end of the
second line. The first version recoloured a card that is red already, and it
barely showed. Safety car and VSC still recolour the card.

Drives the real F1Renderer with the Pi's config, so run it on the Pi:

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_live_header.py
"""
from __future__ import annotations

import json
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

import f1_renderer as FR  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


cfg = json.load(open(CORE + "/config/config.json", encoding="utf-8"))["f1-live"]
R = FR.F1Renderer(128, 32, cfg)
W, H = 128, 32
BADGE = FR._RED_FLAG_BADGE
BAR = (40, 0, 0)          # the live header's usual card colour
WHITE = (255, 255, 255)


def pixels(img):
    return img.convert("RGB").load()


def badge_box(img):
    """Bounding box of the badge colour, or None."""
    p = pixels(img)
    hits = [(x, y) for x in range(W) for y in range(H) if p[x, y] == BADGE]
    if not hits:
        return None
    xs, ys = [x for x, _ in hits], [y for _, y in hits]
    return min(xs), min(ys), max(xs), max(ys)


def test_badge_colour_is_its_own():
    check("the badge colour is not F1 red, so it can be told apart from the title",
          tuple(FR.F1_RED) != tuple(BADGE), (FR.F1_RED, BADGE))
    check("and a green card has none of it",
          badge_box(R.render_live_header("SPANISH GP", None, session_label="Practice 3")) is None)


def test_red_flag_is_the_green_card_plus_a_badge():
    for label, kw in (("practice", dict(session_label="Practice 3")),
                      ("qualifying", dict(session_label="Qualifying")),
                      ("race", dict(lap=23))):
        green = R.render_live_header("SPANISH GP", None, **kw)
        red = R.render_live_header("SPANISH GP", "RED", **kw)
        box = badge_box(red)
        check(label + ": a red badge is drawn", box is not None)
        if not box:
            continue
        x0, y0, x1, y1 = box
        check(label + ": on the second line", y0 >= 14, box)
        check(label + ": at the right end", x0 >= 64 and x1 >= W - 6, box)
        pr, pg = pixels(red), pixels(green)
        changed = [(x, y) for x in range(W) for y in range(H)
                   if not (x0 <= x <= x1 and y0 <= y <= y1) and pr[x, y] != pg[x, y]]
        check(label + ": everything outside it is the green card, pixel for pixel",
              not changed, changed[:6])
        whites = sum(1 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                     if pr[x, y] == WHITE)
        check(label + ": white lettering in the badge", whites > 20, whites)


def test_a_long_session_name_stops_short_of_the_badge():
    red = R.render_live_header("SPANISH GP", "RED", session_label="Sprint Qualifying")
    box = badge_box(red)
    check("long name: badge drawn", box is not None)
    if box:
        x0, y0, _, y1 = box
        p = pixels(red)
        touching = [(x, y) for x in range(x0 - 2, x0) for y in range(y0, y1 + 1)
                    if p[x, y] != BAR]
        check("long name: a clear gap of card before the badge", not touching, touching[:6])


def test_badge_without_a_second_line():
    box = badge_box(R.render_live_header("SPANISH GP", "RED"))
    check("no session name or lap: the badge still gets its line", bool(box) and box[1] >= 14, box)


def test_safety_car_and_vsc_unchanged():
    for flag, colour in (("SC", (255, 220, 0)), ("VSC", (255, 160, 0))):
        img = R.render_live_header("SPANISH GP", flag, lap=23)
        p = pixels(img)
        check(flag + ": no red badge", badge_box(img) is None)
        check(flag + ": the name is recoloured",
              any(p[x, y] == colour for x in range(W) for y in range(16)))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("-- " + name)
            fn()
    print("\n%d failed" % len(failures) if failures else "\nALL PASS")
    sys.exit(1 if failures else 0)
