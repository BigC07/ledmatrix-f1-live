#!/usr/bin/env python3
"""Qualifying up to its race's start, never after (2026-09-13).

Asked for as the wall went back to its normal rotation after the Spanish GP:
show qualifying "all the way up to race starts ... once the race starts, don't
show any more qualifying results". Two things carry qualifying: Jolpica's
qualifying section (_stale_sections) and the finished qualifying kept from
F1's feed (_poll_last_session). Checks:

  - Jolpica's qualifying shows up to the race start and goes at it, and stays
    gone the next week;
  - a race the two sources date a day apart (one starting after midnight UTC)
    is matched to its start, and its result is not taken for last weekend's;
  - with no schedule, qualifying goes once its race day is over;
  - the F1 block drops the section;
  - the feed's qualifying result goes when the race starts, even one never
    seen live, and does not come back;
  - the feed's finished race is not a session result (the podium has it).

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_quali_cutoff.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

CORE = "/home/admin/LEDMatrix"
PLUGIN = os.environ.get("F1_LIVE_PLUGIN", CORE + "/plugin-repos/f1-live")
sys.path.insert(0, PLUGIN)
sys.path.insert(0, CORE)
if os.path.isdir(CORE):
    os.chdir(CORE)
logging.disable(logging.CRITICAL)

import manager as M  # noqa: E402

PLUGIN_CLS = next(v for v in vars(M).values()
                  if isinstance(v, type) and hasattr(v, "get_vegas_alert"))
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, max_age=None):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class FakeFeed:
    def __init__(self, final=None):
        self.final = final

    def final_snapshot(self):
        return self.final


def weekend(race_utc, name="Spanish Grand Prix"):
    fmt = "%Y-%m-%dT%H:%MZ"
    return {"name": name, "sessions": [
        {"type_abbr": "FP1", "date": (race_utc - timedelta(days=2, hours=1)).strftime(fmt)},
        {"type_abbr": "Qual", "date": (race_utc - timedelta(hours=23)).strftime(fmt)},
        {"type_abbr": "Race", "date": race_utc.strftime(fmt)}]}


def snap(key, name, stype, codes=("ANT", "RUS", "LEC")):
    return {"session_key": key, "session_name": name, "session_type": stype,
            "meeting_name": "Spanish Grand Prix", "finished": True,
            "entries": [{"position": i + 1, "code": c, "best_lap": "1:33.%03d" % (900 + i),
                         "gap_to_leader": None if i == 0 else 0.1 * i}
                        for i, c in enumerate(codes)]}


def plugin(final=None):
    p = PLUGIN_CLS.__new__(PLUGIN_CLS)
    p.logger = logging.getLogger("test")
    p.config = {"live": {"result_sessions": ["Practice", "Qualifying"]},
                "vegas": {"sections": ["upcoming", "qualifying", "last_race"]}}
    p.cache_manager = FakeCache()
    p._live_feed = FakeFeed(final)
    p._last_session = None
    p._last_session_sig = None
    p._last_session_dropped = None
    p._vegas_last_session_cards = []
    p._vegas_live_race_cards = []
    p._podium_cards = []
    p._qualifying = None
    p._upcoming_race = None
    p._recent_races = []
    p._schedule_events = None
    p._build_session_result_cards = lambda s: ["result:%s" % e["code"] for e in s["entries"]]
    p._vegas_section_images = lambda section: ["section:%s" % section]
    return p


# --- Jolpica's qualifying section -------------------------------------------------
start = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)      # the Spanish GP
p = plugin()
p._schedule_events = [weekend(start)]
p._qualifying = {"race_name": "Spanish Grand Prix", "date": "2026-09-13"}
p._recent_races = [{"race_name": "Italian Grand Prix", "date": "2026-09-06"}]


def hidden(now):
    return p._stale_sections(now)


check("after qualifying, the evening before: shown",
      "qualifying" not in hidden(start - timedelta(hours=20)))
check("a minute before the start: shown", "qualifying" not in hidden(start - timedelta(minutes=1)))
check("at the start: gone", "qualifying" in hidden(start))
check("after the race: still gone", "qualifying" in hidden(start + timedelta(hours=2)))
check("and the next week, before the next weekend starts",
      "qualifying" in hidden(start + timedelta(days=4)))
check("last weekend's race result still goes as before",
      "last_race" in hidden(start - timedelta(hours=20)))

vegas = datetime(2026, 11, 22, 4, 0, tzinfo=timezone.utc)     # Saturday 20:00 in Las Vegas
lv = plugin()
lv._schedule_events = [weekend(vegas, "Las Vegas Grand Prix")]
lv._qualifying = {"race_name": "Las Vegas Grand Prix", "date": "2026-11-21"}
lv._recent_races = [{"race_name": "Las Vegas Grand Prix", "date": "2026-11-21"}]
check("dates a day apart: qualifying shown up to the start",
      "qualifying" not in lv._stale_sections(vegas - timedelta(minutes=1)))
check("and gone at the start", "qualifying" in lv._stale_sections(vegas))
check("and its race result is not taken for last weekend's",
      "last_race" not in lv._stale_sections(vegas + timedelta(hours=3)))

ns = plugin()
ns._qualifying = {"race_name": "Spanish Grand Prix", "date": "2026-09-13"}
check("no schedule: shown on race day",
      "qualifying" not in ns._stale_sections(datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)))
check("no schedule: gone the day after",
      "qualifying" in ns._stale_sections(datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc)))

now = datetime.now(timezone.utc)
b = plugin()
b._schedule_events = [weekend(now - timedelta(minutes=5))]
b._qualifying = {"race_name": "Spanish Grand Prix",
                 "date": (now - timedelta(minutes=5)).strftime("%Y-%m-%d")}
images = b.get_vegas_content()
check("race started five minutes ago: no qualifying in the F1 block",
      "section:qualifying" not in images, images)
check("the upcoming card stays", "section:upcoming" in images, images)
b._schedule_events = [weekend(now + timedelta(hours=2))]
b._qualifying["date"] = (now + timedelta(hours=2)).strftime("%Y-%m-%d")
check("race two hours off: qualifying in the F1 block",
      "section:qualifying" in b.get_vegas_content())

# --- the qualifying kept from F1's feed -----------------------------------------
f = plugin(final=snap(12, "Qualifying", "Qualifying"))
f._schedule_events = [weekend(now + timedelta(hours=20))]
f._poll_last_session(None)
check("a finished qualifying is kept before the race",
      (f._last_session or {}).get("session_name") == "Qualifying", f._last_session)
check("and leads the F1 block", f.get_vegas_content()[0] == "result:ANT")
# 21 hours on: the race started an hour ago, and was never seen live here.
f._last_session["captured_at"] -= 21 * 3600
f._schedule_events = [weekend(now - timedelta(hours=1))]
f._qualifying = {"race_name": "Spanish Grand Prix",
                 "date": (now - timedelta(hours=1)).strftime("%Y-%m-%d")}
f._poll_last_session(None)
check("once the race has started it goes", f._last_session is None, f._last_session)
check("and its cards", f._vegas_last_session_cards == [])
f._poll_last_session(None)
check("and it does not come back from the feed", f._last_session is None)
check("nothing of it in the F1 block",
      not any(i.startswith("result:") or i == "section:qualifying"
              for i in f.get_vegas_content() or []))

pr = plugin(final=snap(11, "Practice 2", "Practice"))
pr._schedule_events = [weekend(now - timedelta(hours=1))]
pr._poll_last_session(None)
check("a practice result is not affected by a race start", pr._last_session is not None)
rc = plugin(final=snap(30, "Race", "Race"))
rc._poll_last_session(None)
check("the feed's finished race is not a session result", rc._last_session is None)

print()
print("%d failure(s)" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
