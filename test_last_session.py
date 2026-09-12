#!/usr/bin/env python3
"""Offline checks for the last-session result on the F1 ticker.

After a practice or qualifying ends, its final order stays on the marquee --
drawn like the qualifying cards -- until the next session goes live. Asked for
on 2026-09-11 after FP2. These drive the manager's own methods on an instance
built without __init__, with the feed, cache and card builder stubbed, so they
need the core on sys.path (run on the Pi) but never touch the network.

Run on the Pi:  cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_last_session.py
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
                  if isinstance(v, type) and hasattr(v, "_poll_last_session"))

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


class FakeCache:
    def __init__(self, preload=None):
        self.store = dict(preload or {})
        self.sets = 0
        self.deletes = 0

    def get(self, key, max_age=None, **kw):
        return self.store.get(key)

    def set(self, key, data, ttl=None):
        self.sets += 1
        self.store[key] = data

    def delete(self, key):
        self.deletes += 1
        self.store.pop(key, None)


class FakeFeed:
    def __init__(self, final=None):
        self.final = final

    def final_snapshot(self):
        return self.final


def snap(key=11, name="Practice 2", stype="Practice", codes=("ANT", "RUS", "LEC")):
    return {
        "session_key": key, "session_name": name, "session_type": stype,
        "meeting_name": "Spanish Grand Prix", "finished": True,
        "entries": [{"position": i + 1, "code": c, "best_lap": "1:33.%03d" % (900 + i),
                     "gap_to_leader": None if i == 0 else 0.1 * i}
                    for i, c in enumerate(codes)],
    }


def plugin(final=None, result_sessions=("Practice", "Qualifying"), cache=None):
    p = PLUGIN_CLS.__new__(PLUGIN_CLS)
    p.logger = logging.getLogger("test")
    p.config = {"live": {"result_sessions": list(result_sessions)},
                "vegas": {"sections": ["upcoming", "qualifying", "last_race"]}}
    p.cache_manager = cache or FakeCache()
    p._live_feed = FakeFeed(final)
    p._last_session = None
    p._last_session_sig = None
    p._last_session_dropped = None
    p._vegas_last_session_cards = []
    p._vegas_live_race_cards = []
    p._qualifying = None
    p._upcoming_race = None
    p._recent_races = []
    p._schedule_events = None
    # One marker per card, so the marquee order can be read back.
    p._build_session_result_cards = lambda s: ["result:%s" % e["code"] for e in s["entries"]]
    p._vegas_section_images = lambda section: ["section:%s" % section]
    return p


def test_stores_final_and_leads_the_marquee():
    p = plugin(final=snap())
    p._poll_last_session(None)
    check("finished practice stored", (p._last_session or {}).get("session_name") == "Practice 2")
    check("persisted for a restart", M.F1ScoreboardPlugin._LAST_SESSION_KEY in p.cache_manager.store
          if hasattr(M, "F1ScoreboardPlugin") else bool(p.cache_manager.store))
    images = p.get_vegas_content()
    check("result cards lead the marquee", images[:3] == ["result:ANT", "result:RUS", "result:LEC"],
          images)
    check("a practice result leaves the qualifying section alone", "section:qualifying" in images,
          images)


def test_qualifying_result_hides_the_qualifying_section():
    p = plugin(final=snap(key=12, name="Qualifying", stype="Qualifying"))
    p._poll_last_session(None)
    images = p.get_vegas_content()
    check("qualifying result shown", images[0] == "result:ANT", images)
    check("and the Jolpica qualifying section hidden, not shown twice",
          "section:qualifying" not in images, images)
    check("last race still shown", "section:last_race" in images, images)


def test_live_cards_take_precedence():
    p = plugin(final=snap())
    p._poll_last_session(None)
    p._vegas_live_race_cards = ["live:header"]
    images = p.get_vegas_content()
    check("live cards lead", images[0] == "live:header", images)
    check("and the stored result waits", not any(i.startswith("result:") for i in images), images)


def test_same_session_live_keeps_it():
    p = plugin(final=snap(key=11))
    p._poll_last_session(None)
    p._poll_last_session({"session_key": 11, "session_name": "Practice 2"})
    check("the hold after the flag does not drop its own result", p._last_session is not None)


def test_next_session_drops_it_for_good():
    p = plugin(final=snap(key=11))
    p._poll_last_session(None)
    # FP3 goes live while the feed still holds FP2's finished state.
    p._poll_last_session({"session_key": 13, "session_name": "Practice 3"})
    check("next session live drops the result", p._last_session is None)
    check("and its cards", p._vegas_last_session_cards == [])
    check("and the persisted copy", not p.cache_manager.store)
    p._poll_last_session(None)
    check("a dropped session does not come straight back", p._last_session is None)


def test_cache_written_only_on_change():
    p = plugin(final=snap())
    for _ in range(5):
        p._poll_last_session(None)
    check("five identical polls, one cache write", p.cache_manager.sets == 1, p.cache_manager.sets)
    p._live_feed.final = snap(codes=("RUS", "ANT", "LEC"))
    p._poll_last_session(None)
    check("a changed order is written", p.cache_manager.sets == 2, p.cache_manager.sets)


def test_expires_when_the_next_result_never_came():
    p = plugin(final=snap(key=11))
    p._poll_last_session(None)
    p._last_session["captured_at"] = time.time() - p._LAST_SESSION_TTL - 10
    p._poll_last_session(None)
    check("stale result expires", p._last_session is None)


def test_restore_after_restart():
    stored = dict(snap(), captured_at=time.time())
    p = plugin(cache=FakeCache({PLUGIN_CLS._LAST_SESSION_KEY: stored}))
    check("restored from the cache", (p._restore_last_session() or {}).get("session_key") == 11)
    p2 = plugin(cache=FakeCache({PLUGIN_CLS._LAST_SESSION_KEY: {"entries": []}}))
    check("an empty stored result is ignored", p2._restore_last_session() is None)


def test_empty_list_turns_it_off():
    p = plugin(final=snap(), result_sessions=())
    p._last_session = dict(snap(), captured_at=time.time())
    p._poll_last_session(None)
    check("result_sessions [] clears and stores nothing", p._last_session is None)


def test_openf1_feed_without_final_snapshot():
    p = plugin()
    p._live_feed = object()
    p._poll_last_session(None)
    check("a feed without final_snapshot is not an error", p._last_session is None)


def test_real_cards_match_the_q3_cards_pixel_for_pixel():
    """The real builder and renderer: one header and ten rows, and a result row
    byte-identical to a Q3 row for the same driver -- the look the result was
    asked to match (2026-09-11)."""
    import json as _json
    from f1_renderer import F1Renderer
    cfg = _json.load(open(CORE + "/config/config.json", encoding="utf-8"))["f1-live"]
    p = plugin()
    del p._build_session_result_cards          # use the real method
    p._scroll_renderer = F1Renderer(128, 32, cfg)
    p._live_race_name = lambda s: ""
    codes = ("ANT", "LEC", "HAM", "LIN", "RUS", "VER", "PIA", "TSU", "OCO", "LAW", "ALB", "SAI")
    s = snap(codes=codes)
    for e in s["entries"]:
        e["last_name"] = "Driver" + e["code"]
        e["constructor_id"] = "Mercedes"
    cards = p._build_session_result_cards(s)
    check("one header and ten rows", len(cards) == 11, len(cards))
    check("every card is a 128x32 image",
          all(getattr(c, "size", None) == (128, 32) for c in cards))
    e = s["entries"][1]
    q3 = p._scroll_renderer.render_qualifying_entry({
        "position": e["position"], "last_name": e["last_name"], "code": e["code"],
        "constructor_id": e["constructor_id"], "q3": e["best_lap"],
        "q3_gap": "+%.3f" % e["gap_to_leader"], "eliminated_in": ""}, "Q3")
    check("a result row is pixel-identical to a Q3 row", cards[2].tobytes() == q3.tobytes())
    qs = snap(key=12, name="Qualifying", stype="Qualifying", codes=codes)
    for e in qs["entries"]:
        e["last_name"] = "Driver" + e["code"]
        e["constructor_id"] = "Mercedes"
    check("a qualifying result builds too", len(p._build_session_result_cards(qs)) == 11)


def weekend(first_utc):
    fmt = "%Y-%m-%dT%H:%MZ"
    return {"name": "Next Grand Prix",
            "sessions": [{"type_abbr": "FP1", "date": first_utc.strftime(fmt), "status_state": "post"},
                         {"type_abbr": "Race", "date": (first_utc + timedelta(days=2)).strftime(fmt),
                          "status_state": "pre"}]}


def test_last_weekends_qualifying_goes_when_practice_starts():
    """Asked for 2026-09-11: once a new Grand Prix weekend is under way, the
    previous weekend's qualifying is off the ticker. Monza's Q3 cards were still
    scrolling on the Friday of the Spanish GP."""
    now = datetime.now(timezone.utc)
    p = plugin()
    p._qualifying = {"race_name": "Italian Grand Prix",
                     "date": (now - timedelta(days=5)).strftime("%Y-%m-%d")}
    p._upcoming_race = weekend(now + timedelta(hours=3))
    check("before the next weekend starts, last weekend's qualifying stays",
          "section:qualifying" in p.get_vegas_content())
    p._upcoming_race = weekend(now - timedelta(hours=3))
    images = p.get_vegas_content()
    check("once its first session has started, it goes", "section:qualifying" not in images, images)
    check("and the upcoming race card stays", "section:upcoming" in images, images)
    p._qualifying = {"race_name": "Spanish Grand Prix",
                     "date": (now + timedelta(days=2)).strftime("%Y-%m-%d")}
    check("this weekend's qualifying, once published, is shown",
          "section:qualifying" in p.get_vegas_content())
    p._upcoming_race, p._qualifying = None, {"date": "2026-09-06"}
    check("with no schedule, nothing is hidden", "section:qualifying" in p.get_vegas_content())


def test_last_weekends_race_goes_too_and_does_not_come_back():
    """Asked for 2026-09-11, after qualifying: no results from last weekend once
    the new weekend has started -- the race result as well. And after the new
    race it must not return while Jolpica is still publishing the new one: the
    newest weekend that has started decides, not the upcoming race."""
    now = datetime.now(timezone.utc)
    monza = {"race_name": "Italian Grand Prix",
             "date": (now - timedelta(days=5)).strftime("%Y-%m-%d")}
    p = plugin()
    p._recent_races = [monza]
    p._upcoming_race = weekend(now + timedelta(hours=3))
    check("before the next weekend starts, the last race stays",
          "section:last_race" in p.get_vegas_content())
    p._upcoming_race = weekend(now - timedelta(hours=3))
    images = p.get_vegas_content()
    check("once its first session has started, the last race goes too",
          "section:last_race" not in images, images)
    # Sunday evening: the new race is over, the upcoming race is the one after,
    # and Jolpica still has Monza.
    spain = weekend(now - timedelta(days=2, hours=6))        # its race ended ~6 h ago
    nxt = weekend(now + timedelta(days=12))
    p._schedule_events = [weekend(now - timedelta(days=9)), spain, nxt]
    p._upcoming_race = nxt
    images = p.get_vegas_content()
    check("after the new race, last weekend does not come back while Jolpica catches up",
          "section:last_race" not in images, images)
    p._recent_races = [{"race_name": "Spanish Grand Prix",
                        "date": (now - timedelta(hours=6)).strftime("%Y-%m-%d")}]
    check("and the new race result shows once it is published",
          "section:last_race" in p.get_vegas_content())


def test_a_cancelled_weekend_is_not_a_newer_weekend():
    """ESPN keeps cancelled Grands Prix in the schedule, dates and all -- in 2026
    Bahrain and Saudi Arabia, every session "Canceled". A weekend that never
    ran must not hide the last real one's results."""
    now = datetime.now(timezone.utc)
    japan = {"race_name": "Japanese Grand Prix",
             "date": (now - timedelta(days=12)).strftime("%Y-%m-%d")}
    called_off = weekend(now - timedelta(days=2))
    for s in called_off["sessions"]:
        s.update(status_state="post", status_detail="Canceled", status_short="Canceled")
    p = plugin()
    p._qualifying, p._recent_races = dict(japan), [dict(japan)]
    p._schedule_events = [weekend(now - timedelta(days=14)), called_off,
                          weekend(now + timedelta(days=12))]
    images = p.get_vegas_content()
    check("a cancelled weekend does not hide the last real race", "section:last_race" in images,
          images)
    check("nor its qualifying", "section:qualifying" in images, images)
    called_off["sessions"][0].update(status_detail="Final", status_short="Final")
    check("a weekend whose first session ran still counts",
          "section:last_race" not in p.get_vegas_content())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("--", name)
            fn()
    print()
    print("%d failure(s)" % len(failures) if failures else "all passed")
    sys.exit(1 if failures else 0)
