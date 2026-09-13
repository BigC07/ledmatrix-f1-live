#!/usr/bin/env python3
"""The fastest lap on the live board, and the Audi rings (2026-09-13).

Both picked from LED-dot mock-ups during the Spanish GP: a purple stopwatch
after the gap on the holder's row (option 1), with the full lap time beside it
in its own colour; and for the 2026 Audi team, red rings over AUDI (option C,
tools/f1_audi_logo.py) in place of the Sauber K.

    cd <fork> && PYTHONPATH=/home/admin/LEDMatrix python3 test_fastest_lap.py
"""
import os
import sys

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
from f1_live import live_row_from_entry  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


r = F1Renderer(128, 32, {"timezone": "America/New_York"})
PURPLE, TIME, WHITE = np.array(r.FL_PURPLE), np.array(r.FL_TIME), np.array((210, 210, 210))


def card(entry=None, live=True, **row_extra):
    e = {"position": 4, "last_name": "Norris", "code": "NOR", "constructor_id": "McLaren",
         "gap_to_leader": 8.9, "interval": 2.345, "lap": 40, "grid": 2}
    e.update(entry or {})
    row = live_row_from_entry(e, 40)
    row.update(row_extra)
    return np.asarray(r.render_race_row(row, live=live).convert("RGB"))


def ink(a, colour, rows=slice(0, 30)):
    m = (a[rows] == colour).all(axis=2)
    ys, xs = np.nonzero(m)
    return xs, ys


plain = card()
fl = card(fastest_lap=True, fastest_lap_time="1:31.234")
check("no purple on a row without the fastest lap", ink(plain, PURPLE)[0].size == 0)
px, py = ink(fl, PURPLE)
check("the holder's row has the purple stopwatch, five columns wide",
      px.size > 0 and px.max() - px.min() == 4, (px.min() if px.size else None, px.max() if px.size else None))
check("on line 2", py.size > 0 and py.min() >= 16, py.min() if py.size else None)
wx, _ = ink(fl, WHITE, slice(16, 30))
check("after the white gap", wx.size > 0 and px.min() > wx.max(), (wx.max() if wx.size else None, px.min()))
tx, ty = ink(fl, TIME)
check("the lap time follows it in its own colour", tx.size > 0 and tx.min() > px.max(),
      (tx.min() if tx.size else None, px.max()))
dx, _ = ink(fl, np.array((220, 50, 50)), slice(16, 30))     # the -2 places figure, red
check("and ends before the places-gained figure", dx.size > 0 and tx.max() < dx.min(),
      (tx.max(), dx.min() if dx.size else None))
full_w = tx.max() - tx.min()
check("the whole lap time fits beside a short gap", full_w >= 30, full_w)

tight = card(entry={"interval": 12.345, "grid": 16}, fastest_lap=True,
             fastest_lap_time="1:31.234")
tx2, _ = ink(tight, TIME)
dx2, _ = ink(tight, np.array((0, 210, 80)), slice(16, 30))  # +12, green
check("beside a long gap and a two-digit figure the time is shortened, not run into it",
      tx2.size > 0 and dx2.size > 0 and tx2.max() < dx2.min()
      and (tx2.max() - tx2.min()) < full_w, (tx2.max() if tx2.size else None,
                                             dx2.min() if dx2.size else None))
check("no stopwatch without a lap time is still a stopwatch",
      ink(card(fastest_lap=True, fastest_lap_time=""), PURPLE)[0].size > 0)

r.show_fl_dot = True
dot = tuple(r.fl_dot_color)[:3]
live_dot = card(fastest_lap=True, fastest_lap_time="1:31.234")
check("a live row does not also get the upstream corner dot",
      not (live_dot[2:5, 124:127] == dot).all(axis=2).any())
done = card(live=False, fastest_lap=True)
check("a finished race row still gets it", (done[2:5, 124:127] == dot).all(axis=2).any())
r.show_fl_dot = False

logo = r.logo_loader.get_team_logo("Audi", max_height=20, max_width=20)
a = np.asarray(logo.convert("RGBA"))
red = ((a[..., 0] > 200) & (a[..., 1] < 60) & (a[..., 2] < 80) & (a[..., 3] > 0)).sum()
white = ((a[..., :3] > 230).all(axis=2) & (a[..., 3] > 0)).sum()
green = ((a[..., 1] > 120) & (a[..., 0] < 120) & (a[..., 2] < 120) & (a[..., 3] > 0)).sum()
check("Audi draws the red rings", red >= 40, red)
check("over a white AUDI", white >= 20, white)
check("and not the Sauber K", green == 0, green)
check("at 20x20, untouched", logo.size == (20, 20), logo.size)

print()
print("%d failure(s)" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
