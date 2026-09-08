#!/usr/bin/env python3
"""Offline checks for f1-live against ~/f1fixtures. Must not call OpenF1."""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone

PLUGIN = "/home/admin/LEDMatrix/plugin-repos/f1-live"
FIXTURES = os.path.expanduser("~/f1fixtures")
sys.path.insert(0, PLUGIN)

from f1_live import (  # noqa: E402
    LiveRaceFeed, format_gap, live_header_title, live_row_from_entry,
)


class BoomSession:
    def get(self, *args, **kwargs):
        raise RuntimeError("HTTP should not be called with fixture_dir: %s %s" %
                           (args, kwargs))


def feed(at, **extra):
    return LiveRaceFeed(
        session=BoomSession(),
        replay_session_key=11361,
        replay_at=at,
        fixture_dir=FIXTURES,
        **extra,
    )


def codes(snap, n=8):
    return [e["code"] for e in snap["entries"][:n]]


failures = []


def check(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        failures.append(name)


print("=== 14:00Z running order, gaps, lap, grid, flag ===")
s = feed("2026-09-06T14:00:00+00:00").state()
check("state is not None", s is not None)
check("order RUS VER ANT PIA HAM GAS NOR LIN",
      codes(s) == ["RUS", "VER", "ANT", "PIA", "HAM", "GAS", "NOR", "LIN"],
      codes(s))
check("lap is 15", s.get("lap") == 15, s.get("lap"))
check("flag is None at 14:00 (green)", s.get("flag") is None, s.get("flag"))
p2 = s["entries"][1]
check("VER gap_to_leader ~0.392",
      abs(float(p2["gap_to_leader"]) - 0.392) < 1e-6, p2.get("gap_to_leader"))
check("VER interval ~0.392",
      abs(float(p2["interval"]) - 0.392) < 1e-6, p2.get("interval"))
check("RUS gap 0.0", s["entries"][0]["gap_to_leader"] == 0.0,
      s["entries"][0].get("gap_to_leader"))
check("RUS grid 2", s["entries"][0]["grid"] == 2, s["entries"][0].get("grid"))
check("VER grid 5", p2["grid"] == 5, p2.get("grid"))
check("ANT grid 19", s["entries"][2]["grid"] == 19, s["entries"][2].get("grid"))
check("GAS grid 1", next(e["grid"] for e in s["entries"] if e["code"] == "GAS") == 1)

lec = next(e for e in s["entries"] if e["code"] == "LEC")
row = live_row_from_entry(lec, s["lap"])
check("LEC is RETIRED not LAPPED", row["status"] == "Retired" and row["time"] == "RETIRED",
      row)
leader = live_row_from_entry(s["entries"][0], s["lap"])
check("P1 time LEADER", leader["time"] == "LEADER", leader)
ver_row = live_row_from_entry(p2, s["lap"])
check("P2 time +0.392", ver_row["time"] == "+0.392", ver_row)
check("P2 grid 5 so arrow +3", ver_row["grid"] == 5 and ver_row["position"] == 2)
check("header MONZA · LAP 15",
      live_header_title(s) == "MONZA · LAP 15", live_header_title(s))

print("=== flag windows ===")
sc = feed("2026-09-06T13:07:00+00:00").state()
check("13:07 SC", sc and sc.get("flag") == "SC", sc.get("flag") if sc else None)
red = feed("2026-09-06T13:10:00+00:00").state()
check("13:10 RED", red and red.get("flag") == "RED", red.get("flag") if red else None)
vsc = feed("2026-09-06T14:18:00+00:00").state()
check("14:18 VSC", vsc and vsc.get("flag") == "VSC", vsc.get("flag") if vsc else None)
after = feed("2026-09-06T14:20:00+00:00").state()
check("14:20 VSC cleared", after and after.get("flag") is None,
      after.get("flag") if after else None)
restart_sc = feed("2026-09-06T13:35:00+00:00").state()
check("13:35 restart SC (lights on)",
      restart_sc and restart_sc.get("flag") == "SC",
      restart_sc.get("flag") if restart_sc else None)

print("=== no live session without replay ===")
# fixture_dir + latest session is Monza, which ended hours ago from 'now'.
# Without replay, _in_live_window should refuse rather than fetch intervals.
dead = LiveRaceFeed(session=BoomSession(), fixture_dir=FIXTURES)
check("no replay -> None (old session)", dead.state() is None)

print("=== cache: second state() does not re-read? (functional, still no HTTP) ===")
f = feed("2026-09-06T14:00:00+00:00")
a = f.state()
b = f.state()
check("repeat state same order", codes(a) == codes(b))

print("=== format_gap edges ===")
check("lapped string", format_gap("+1 LAP", 22, 14, 15) == ("LAPPED", "Lapped"))
check("retired deficit", format_gap("+1 LAP", 22, 2, 15) == ("RETIRED", "Retired"))
check("leader", format_gap(0.0, 1, 15, 15) == ("LEADER", "Finished"))
check("none gap", format_gap(None, 4, 15, 15) == ("", "Finished"))

if failures:
    print("\n%d FAILED:" % len(failures), ", ".join(failures))
    sys.exit(1)
print("\nALL PASS")
