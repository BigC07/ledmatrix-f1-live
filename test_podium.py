#!/usr/bin/env python3
"""After a race: the winner pop-up and the podium (2026-09-13).

Asked for as the Spanish GP ended: "a checkered flag with the winner's name in
it for a few passes", then "a list of the top three ... until it goes back into
the results mode". This builds the plugin without __init__ (as
test_vegas_alert.py does), feeds _poll_podium() finished races, and checks:

  - a finished Grand Prix is captured, from the live board or from the feed's
    final order; a sprint, qualifying, practice or a race still running is
    not; nothing happens before Jolpica has been read once;
  - the winner card goes out as an alert four times, about a minute apart;
  - a last-lap change updates the podium without restarting the passes;
  - it is persisted: a restart neither loses nor repeats the passes;
  - the PODIUM header and three rows lead the F1 block, behind a live board;
  - it goes when Jolpica has the race, a new session goes live, or after 36 h,
    and the race the feed still holds is not captured again, restart or not;
  - the chequered winner card itself;
  - WINNER in the alert test file shows the winner card.

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_podium.py
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

CORE = "/home/admin/LEDMatrix"
PLUGIN = os.environ.get("F1_LIVE_PLUGIN", CORE + "/plugin-repos/f1-live")
sys.path.insert(0, PLUGIN)
sys.path.insert(0, CORE)
if os.path.isdir(CORE):
    os.chdir(CORE)
logging.disable(logging.CRITICAL)

import numpy as np  # noqa: E402
import manager as M  # noqa: E402
from f1_renderer import F1Renderer  # noqa: E402

PLUGIN_CLS = next(v for v in vars(M).values()
                  if isinstance(v, type) and hasattr(v, "get_vegas_alert"))
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


cfg = json.load(open(CORE + "/config/config.json", encoding="utf-8"))["f1-live"]


class Cache:
    def __init__(self):
        self.d = {}

    def get(self, key, max_age=None):
        return copy.deepcopy(self.d.get(key))

    def set(self, key, value, ttl=None):
        self.d[key] = copy.deepcopy(value)

    def delete(self, key):
        self.d.pop(key, None)


class Feed:
    def __init__(self, final=None):
        self.final = final

    def state(self):
        return None

    def final_snapshot(self):
        return self.final


DRIVERS = {"ANT": ("Antonelli", "Mercedes", 2), "VER": ("Verstappen", "Red Bull Racing", 3),
           "NOR": ("Norris", "McLaren", 1), "LEC": ("Leclerc", "Ferrari", 4)}


def race(key=11375, name="Race", stype="Race", finished=True,
         order=("ANT", "VER", "NOR", "LEC")):
    gaps = (None, 3.21, 7.9, 12.4)
    return {"session_key": key, "session_type": stype, "session_name": name,
            "meeting_name": "Spanish Grand Prix", "finished": finished, "lap": 57, "flag": None,
            "entries": [{"position": i + 1, "code": c, "last_name": DRIVERS[c][0],
                         "constructor_id": DRIVERS[c][1], "gap_to_leader": gaps[i],
                         "interval": None, "lap": 57, "grid": DRIVERS[c][2]}
                        for i, c in enumerate(order)]}


def plugin(cache=None, final=None):
    p = PLUGIN_CLS.__new__(PLUGIN_CLS)
    p.logger = logging.getLogger("test")
    p.config = dict(cfg)
    p.cache_manager = cache if cache is not None else Cache()
    p._scroll_renderer = F1Renderer(128, 32, dict(cfg, timezone="America/New_York"))
    p._live_feed = Feed(final)
    p._last_update = 1.0
    p._vegas_alert = None
    p._vegas_live_race_cards = []
    p._vegas_last_session_cards = []
    p._last_session = None
    p._recent_races = [{"race_name": "Italian Grand Prix", "date": "2026-09-06"}]
    p._podium = p._restore_podium()
    p._podium_cards = []
    p._podium_sig = None
    p._stale_sections = lambda now=None: set()
    p._vegas_sections = lambda: []
    return p


def alert_id(p):
    return p._vegas_alert[0] if p._vegas_alert else None


def codes(p):
    return [e["code"] for e in (p._podium or {}).get("entries") or []]


def gone(p):
    return not p._podium_cards and bool((p._podium or {}).get("dropped"))


# --- capture and the passes -----------------------------------------------------
p = plugin()
p._poll_podium(race())
check("a finished race is captured: the top three", codes(p) == ["ANT", "VER", "NOR"], codes(p))
check("the first winner pass goes out at once",
      (alert_id(p) or "").startswith("winner:11375:") and alert_id(p).endswith(":4"), alert_id(p))
check("the podium cards: a header and three rows", len(p._podium_cards) == 4, len(p._podium_cards))
first = alert_id(p)
p._poll_podium(None)
check("no second pass inside the gap", alert_id(p) == first, alert_id(p))
ids = []
for _ in range(4):
    p._podium["winner_next"] = 0.0
    p._poll_podium(None)
    ids.append(alert_id(p))
check("three more passes, then no more",
      [i.rsplit(":", 1)[1] for i in ids] == ["3", "2", "1", "1"], ids)
check("the podium stays after the passes", len(p._podium_cards) == 4)

v = plugin()
v._poll_podium(race())
left = v._podium["winner_left"]
v._poll_podium(race(order=("ANT", "NOR", "VER", "LEC")))
check("a last-lap change updates the podium", codes(v) == ["ANT", "NOR", "VER"], codes(v))
check("without restarting the passes", v._podium["winner_left"] == left, v._podium["winner_left"])

# --- where it comes from --------------------------------------------------------
f = plugin(final=race())
f._poll_podium(None)
check("after the live board has gone, the feed's final order is enough",
      codes(f) == ["ANT", "VER", "NOR"] and alert_id(f), alert_id(f))
g = plugin(final=race())
g._last_update = 0
g._poll_podium(None)
check("nothing before Jolpica has been read once", g._podium is None and g._vegas_alert is None)
for name, stype in (("Qualifying", "Qualifying"), ("Practice 3", "Practice"), ("Sprint", "Race")):
    s = plugin()
    s._poll_podium(race(name=name, stype=stype))
    check("a finished %s is not a podium" % name.lower(), s._podium is None, s._podium)
u = plugin()
u._poll_podium(race(finished=False))
check("a race still running is not a podium", u._podium is None)

# --- persistence ----------------------------------------------------------------
q = plugin(cache=p.cache_manager)
q._poll_podium(None)
check("a restart restores the podium", len(q._podium_cards) == 4, len(q._podium_cards))
check("and does not repeat the passes", q._vegas_alert is None, alert_id(q))
mid = plugin()
mid._poll_podium(race())
mid._podium["winner_left"], mid._podium["winner_next"] = 2, 0.0
mid._save_podium()
r = plugin(cache=mid.cache_manager)
r._poll_podium(None)
check("a restart part-way through carries on with the passes left",
      (alert_id(r) or "").endswith(":2"), alert_id(r))

# --- the F1 block ---------------------------------------------------------------
images = p.get_vegas_content()
check("the podium leads the F1 block", images and images[:4] == p._podium_cards,
      len(images or []))
p._vegas_last_session_cards = ["session"]
check("ahead of a stored session result", p.get_vegas_content()[:4] == p._podium_cards)
p._vegas_last_session_cards = []
p._vegas_live_race_cards = ["live"]
check("but a live board comes first", p.get_vegas_content()[0] == "live")
p._vegas_live_race_cards = []

# --- when it goes ---------------------------------------------------------------
p._podium["captured_at"] = datetime(2026, 9, 13, 14, 45, tzinfo=timezone.utc).timestamp()
p._recent_races = [{"race_name": "Spanish Grand Prix", "date": "2026-09-13"}]
p._poll_podium(None)
check("Jolpica has the race: the podium goes", gone(p), p._podium)
check("and stays gone in the cache",
      bool((p.cache_manager.get(p._PODIUM_KEY) or {}).get("dropped")))
check("with nothing left in the F1 block", not p.get_vegas_content())
p._live_feed.final = race()
p._vegas_alert = None
p._poll_podium(None)
check("the race the feed still holds is not captured again",
      gone(p) and p._vegas_alert is None, alert_id(p))
w = plugin(cache=p.cache_manager, final=race())
w._poll_podium(None)
check("nor after a restart", gone(w) and w._vegas_alert is None, alert_id(w))
n = plugin()
n._poll_podium(race())
n._poll_podium({"session_key": 11400, "session_name": "Practice 1", "session_type": "Practice",
                "finished": False, "entries": []})
check("a new session going live drops it", gone(n), n._podium)
o = plugin()
o._poll_podium(race())
o._podium["captured_at"] -= o._PODIUM_TTL + 60
o._poll_podium(None)
check("and after 36 hours it expires", gone(o), o._podium)
nxt = plugin(cache=n.cache_manager)
nxt._poll_podium(race(key=11420))
check("the next race is a new podium, whatever was dropped before",
      codes(nxt) == ["ANT", "VER", "NOR"] and len(nxt._podium_cards) == 4)

# --- the card -------------------------------------------------------------------
rend = p._scroll_renderer
card = np.asarray(rend.render_winner_card("Antonelli", "Mercedes",
                                          "Spanish Grand Prix").convert("RGB"))
check("the winner card is 128x32", card.shape[:2] == (32, 128), card.shape)
white = (card == rend.CHECK_WHITE).all(axis=2)
check("chequered at the left end", white[0, 0] and not white[0, 4] and not white[4, 0]
      and white[4, 4])
check("and the right", white[:, 112:].any() and white[:, :16].sum() == white[:, 112:].sum())
check("and plain in the middle", not white[:, 18:110].any())
gold = (card[:12, 18:110] == rend.WINNER_GOLD).all(axis=2)
check("SPANISH GP WINNER in gold along the top", gold.sum() > 40, gold.sum())
lit = card[12:31, 18:110].max(axis=2) > 60
check("the name big across the middle",
      lit.sum() > 80 and np.ptp(np.nonzero(lit.any(axis=0))[0]) > 60, lit.sum())
for last, team, gp in (("Verstappen", "Red Bull Racing", "Mexico City Grand Prix"),
                       ("Hulkenberg", "Audi", "Australian Grand Prix")):
    c2 = np.asarray(rend.render_winner_card(last, team, gp).convert("RGB"))
    bands = np.concatenate([c2[:, :16], c2[:, 112:]], axis=1)
    check("%s at the %s stays between the flags" % (last, gp),
          ((bands == rend.CHECK_WHITE).all(axis=2) | (bands == 0).all(axis=2)).all())

# --- the test trigger -----------------------------------------------------------
t = plugin()
t._poll_podium(race())
t._vegas_alert = None
t._ALERT_TEST_FILE = os.path.join(tempfile.mkdtemp(), "f1-live-alert-test")
with open(t._ALERT_TEST_FILE, "w") as fh:
    fh.write("WINNER")
t._maybe_test_alert()
check("WINNER in the test file shows the winner card",
      (alert_id(t) or "").startswith("winner-test:") and not os.path.exists(t._ALERT_TEST_FILE),
      alert_id(t))
t2 = plugin()
t2._ALERT_TEST_FILE = os.path.join(tempfile.mkdtemp(), "f1-live-alert-test")
with open(t2._ALERT_TEST_FILE, "w") as fh:
    fh.write("winner\n")
t2._maybe_test_alert()
check("and a sample one when there is no podium",
      (alert_id(t2) or "").startswith("winner-test:"), alert_id(t2))

print()
print("%d failure(s)" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
