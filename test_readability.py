#!/usr/bin/env python3
"""The F1 cards' times at a size the wall can be read at (2026-09-12).

Asked for with a photo of the upcoming card: "make the race time easier to
read", and "make the F1 times on the cards a little bigger". On a 128x32
renderer this checks:

  - the second line of every driver card (qualifying/practice, race result,
    live race) is in 5x8 rather than 4x6, and still ends above the 2px
    team-colour line along the bottom;
  - each full stop is drawn as the colon's lower dot: 5x8's own "." is a small
    plus sign that read on the panel as a comma, and "+0.011" as "+0+011";
  - the upcoming card draws "RACE 9:00A" whole beside a seven-character
    countdown, and the countdown gives up no more than one tier (9x15) for it;
  - tall panels keep their own tier.

    cd <fork> && PYTHONPATH=/home/admin/LEDMatrix python3 test_readability.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CORE = "/home/admin/LEDMatrix"
if os.path.isdir(CORE):
    sys.path.insert(1, CORE)
    os.chdir(CORE)
import logging  # noqa: E402
logging.disable(logging.CRITICAL)
import numpy as np  # noqa: E402
from f1_renderer import F1Renderer  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


def base(font):
    return os.path.basename(getattr(font, "path", "") or "")


CFG = {"timezone": "America/New_York"}
r = F1Renderer(128, 32, CFG)
tf = r._row_time_font()
check("the times on a 128x32 card are 5x8", base(tf) == "5x8.bdf" and getattr(tf, "size", 0) == 8,
      (base(tf), getattr(tf, "size", None)))
tall = F1Renderer(128, 64, CFG)
check("a tall panel keeps its own tier for them", tall._row_time_font() is tall.fonts["detail"])

dot = r._period_dot(tf)
check("the full stop is the colon's lower dot: a 2x2 block", len(dot) == 4
      and len({x for x, _ in dot}) == 2 and len({y for _, y in dot}) == 2, dot)
check("and only in the 5x8 time font", r._period_dot(r.fonts["detail"]) == [])


def line2(card, left=5, right=100):
    """(first row, last row, lit pixels) of the ink on line 2 of a driver card:
    rows 16-29, between the accent bar and the logo, above the team line."""
    a = np.asarray(card.convert("RGB")).max(axis=2) > 60
    box = a[16:30, left:right]
    rows = [16 + y for y in range(box.shape[0]) if box[y].any()]
    return (rows[0], rows[-1], int(box.sum())) if rows else (None, None, 0)


def period_cell(card, x0, y0):
    """Lit pixels in the 5x8 cell of the full stop that starts at x0."""
    a = np.asarray(card.convert("RGB")).max(axis=2) > 60
    return int(a[y0:y0 + 9, x0:x0 + 5].sum())


cards = {
    "practice/qualifying row": r.render_practice_entry({
        "position": 2, "last_name": "Antonelli", "code": "ANT", "constructor_id": "mercedes",
        "best_lap": "1:31.835", "gap": "+0.011"}),
    "race result row": r.render_race_row({
        "position": 2, "last_name": "Russell", "code": "RUS", "constructor_id": "mercedes",
        "status": "Finished", "time": "+3.857", "grid": 2}),
    "live race row": r.render_race_row({
        "position": 2, "last_name": "Norris", "code": "NOR", "constructor_id": "mclaren",
        "status": "Finished", "time": "+1.204", "grid": 0}, live=True),
}
for name, card in cards.items():
    top, bottom, lit = line2(card)
    check("%s: line 2 is 6 rows of 5x8 digits (was 5 of 4x6)" % name,
          top is not None and bottom - top + 1 == 6, (top, bottom))
    check("%s: and ends above the team line (row %s, line at 30)" % (name, bottom),
          bottom is not None and bottom <= 27)

# Where the "." of "1:31.835" sits: x + 4 characters of 5px, drawn at line 2's top.
card = cards["practice/qualifying row"]
top = line2(card)[0]
x0 = r.accent_bar_width + 2 + 4 * 5
check("the full stop in 1:31.835 lights exactly its 2x2 dot",
      period_cell(card, x0, top - 1) == 4, period_cell(card, x0, top - 1))

# --- the upcoming card ------------------------------------------------------------
race_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=13, minute=0, second=0, microsecond=0)         # 9:00 AM Eastern (EDT)


def upcoming(when, countdown):
    return {"country": "Spain", "city": "Madrid", "name": "Spanish Grand Prix",
            "short_name": "Spanish GP", "circuit_name": "", "next_session_type": "Race",
            "countdown_seconds": countdown,
            "sessions": [{"type_abbr": "Race", "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "status_state": "pre"}]}


def render_spied(renderer, race):
    truncs, fits = [], []
    real_trunc, real_fit = renderer._truncate, renderer._fit_font

    def trunc(draw, text, font, max_w):
        out = real_trunc(draw, text, font, max_w)
        truncs.append((text, out, font))
        return out

    def fit(draw, text, avail_w, tiers):
        out = real_fit(draw, text, avail_w, tiers)
        fits.append((text, out))
        return out

    renderer._truncate, renderer._fit_font = trunc, fit
    try:
        img = renderer.render_upcoming_race(race)
    finally:
        renderer._truncate, renderer._fit_font = real_trunc, real_fit
    return img, truncs, fits


u = F1Renderer(128, 32, CFG)
if u.show_circuit_map:          # this layout is the no-map one; Madrid has no map
    u.show_circuit_map = False
race = upcoming(race_at, 12 * 3600 + 41 * 60)
check("fixture: the minimum next-session line is 'RACE 9:00A'",
      u._next_session_min_line(race) == "RACE 9:00A", u._next_session_min_line(race))
img, truncs, fits = render_spied(u, race)
drawn = [out for text, out, font in truncs if text.startswith("RACE")]
check("the upcoming card draws 'RACE 9:00A' whole beside '12H 41M'",
      drawn == ["RACE 9:00A"], drawn)
check("in 5x8", [base(f) for t, o, f in truncs if t.startswith("RACE")] == ["5x8.bdf"])
cd = [out for text, out in fits if text == "12H 41M"]
check("the countdown gives up one tier at most: 9x15, not smaller",
      [base(f) for f in cd] == ["9x15.bdf"], [base(f) for f in cd])
a = np.asarray(img.convert("RGB")).max(axis=2) > 60
check("and the card's text still ends inside the card", not a[31:, 4:60].any())

img, truncs, fits = render_spied(u, upcoming(race_at, 30 * 3600))
check("a six-character countdown ('1D 6H' family) keeps 10x20",
      [base(f) for t, f in fits] == ["10x20.bdf"], [(t, base(f)) for t, f in fits])
check("and the line is whole there too",
      [o for t, o, f in truncs if t.startswith("RACE")] == ["RACE 9:00A"])

late = race_at.replace(hour=16, minute=30)                # 12:30 PM Eastern
img, truncs, fits = render_spied(u, upcoming(late, 12 * 3600 + 41 * 60))
check("a 12:30 race keeps 'RACE 12:30P' whole",
      [o for t, o, f in truncs if t.startswith("RACE")] == ["RACE 12:30P"],
      [o for t, o, f in truncs if t.startswith("RACE")])

print("\n%d failed" % len(failures) if failures else "\nALL PASS")
sys.exit(1 if failures else 0)
