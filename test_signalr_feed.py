#!/usr/bin/env python3
"""Offline checks for the F1 SignalR feed. Must not touch the network.

Replays a real recording -- 537 hub messages from Spanish GP FP1, captured on
2026-09-11 the first time this feed was tried -- through SignalRLiveFeed and
checks the snapshot the manager would see. The recording runs from ten seconds
before the chequered flag to eighty after it, so it covers a live board, the
finish, and the hold afterwards.

Race-only behaviour (lap counter, gaps, lapped, retired, flags, a new session
on the same connection) is exercised with small synthetic deltas, because the
only live session seen so far was a practice.

Run on the Pi:  python3 test_signalr_feed.py
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

PLUGIN = os.environ.get("F1_LIVE_PLUGIN", "/home/admin/LEDMatrix/plugin-repos/f1-live")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN if os.path.isdir(PLUGIN) else HERE)

from f1_signalr import SignalRLiveFeed, _merge, _parse_utc, _race_gap  # noqa: E402
from f1_live import format_gap, live_row_from_entry  # noqa: E402

RECORDING = "signalr_2026_spain_fp1.jsonl"
CANDIDATES = [os.path.expanduser("~/f1fixtures/" + RECORDING),
              os.path.join(HERE, "fixtures", RECORDING)]
FLAG = datetime(2026, 9, 11, 12, 30, 0, tzinfo=timezone.utc)


class BoomHTTP:
    def get(self, *a, **k):
        raise RuntimeError("network must not be touched: GET %s" % (a,))

    def post(self, *a, **k):
        raise RuntimeError("network must not be touched: POST %s" % (a,))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


def recording():
    for path in CANDIDATES:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return [json.loads(line)["msg"] for line in fh
                        if line.strip() and "msg" in json.loads(line)]
    raise SystemExit("recording not found; looked in %s" % CANDIDATES)


def replay(clock, types=("Practice",), before=None, **kw):
    """Feed the recording in, holding back deltas stamped at or after `before`."""
    feed = SignalRLiveFeed(session_types=types, http=BoomHTTP(), now_fn=clock, **kw)
    for msg in recording():
        if before is not None and msg.get("type") == 1:
            stamp = _parse_utc((msg.get("arguments") or [None] * 3)[2])
            if stamp is not None and stamp >= before:
                break
        feed.feed_message(msg)
    return feed


def feed_delta(feed, topic, delta, when):
    feed.feed_message({"type": 1, "target": "feed",
                       "arguments": [topic, delta, when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")]})


# ── pieces ─────────────────────────────────────────────────────────────
def test_merge():
    state = {"Messages": [{"a": 1}, {"b": 2}]}
    _merge(state, {"Messages": {"2": {"c": 3}}})
    check("list delta by string index appends", state["Messages"][2] == {"c": 3}, state)
    _merge(state, {"Messages": {"0": {"a": 9}}})
    check("list delta by string index updates in place", state["Messages"][0] == {"a": 9}, state)
    fresh = _merge(None, {"x": {"y": 1, "_kf": True}, "_kf": True})
    check("keyframe markers dropped", fresh == {"x": {"y": 1}}, fresh)


def test_parse_utc():
    a = _parse_utc("2026-09-11T12:27:20.4143167Z")
    check("seven fractional digits parse", a == datetime(2026, 9, 11, 12, 27, 20, 414316,
                                                          tzinfo=timezone.utc), a)
    b = _parse_utc("2026-09-11T12:26:45")
    check("race control's bare stamp is UTC", b and b.tzinfo is not None, b)
    check("garbage is None, not an exception", _parse_utc("not a time") is None)


def test_race_gap():
    check("leader's lap counter becomes 0", _race_gap("LAP 23", 1) == 0.0)
    check("seconds gap parsed", _race_gap("+1.234", 2) == 1.234)
    check("'1L' becomes a lapped label", _race_gap("1L", 3) == "+1 LAP", _race_gap("1L", 3))
    check("'2 L' too", _race_gap("2 L", 9) == "+2 LAP", _race_gap("2 L", 9))
    check("blank is None", _race_gap("", 4) is None)


def test_format_gap_unchanged_for_openf1():
    check("openf1 numeric gap", format_gap(1.5, 3) == ("+1.500", "Finished"), format_gap(1.5, 3))
    check("openf1 lapped string", format_gap("+1 LAP", 5) == ("LAPPED", "Lapped"))
    check("openf1 leader", format_gap(None, 1) == ("LEADER", "Finished"))
    check("retired flag wins", format_gap(0.4, 2, retired=True) == ("RETIRED", "Retired"))


# ── the real recording ─────────────────────────────────────────────────
def test_fp1_live_board():
    clock = Clock(FLAG - timedelta(seconds=1))
    snap = replay(clock, before=FLAG).state()
    check("FP1 before the flag is live", snap is not None)
    if not snap:
        return
    check("session named", snap["session_name"] == "Practice 1", snap["session_name"])
    check("meeting named without the sponsor", snap["meeting_name"] == "Spanish Grand Prix",
          snap["meeting_name"])
    check("practice has no lap counter", snap["lap"] is None, snap["lap"])
    check("track clear, no chip", snap["flag"] is None, snap["flag"])
    e = snap["entries"]
    check("whole field present", len(e) == 22, len(e))
    check("positions run 1..n", [x["position"] for x in e] == list(range(1, len(e) + 1)))
    p1, p2 = e[0], e[1]
    print("      top three:", ", ".join("%s %s %s" % (x["code"], x["best_lap"],
                                                    "" if x["gap_to_leader"] is None
                                                    else "+%.3f" % x["gap_to_leader"])
                                        for x in e[:3]))
    check("P1 has a lap time", bool(re.match(r"^\d:\d\d\.\d{3}$", p1["best_lap"] or "")),
          p1["best_lap"])
    check("P1 has no gap to itself", p1["gap_to_leader"] is None, p1["gap_to_leader"])
    check("P2 is some way off it", (p2["gap_to_leader"] or 0) > 0, p2["gap_to_leader"])
    r1 = live_row_from_entry(p1, snap["lap"], timed=True)
    r2 = live_row_from_entry(p2, snap["lap"], timed=True)
    check("P1 row shows the time to beat, milliseconds and all", r1["time"] == p1["best_lap"], r1)
    check("P2 row shows its gap", r2["time"].startswith("+"), r2)
    check("no LEADER/RETIRED labels in practice",
          all(live_row_from_entry(x, None, timed=True)["status"] == "Finished" for x in e))
    check("every car has a team", all(x["constructor_id"] for x in e))
    try:
        sys.path.insert(0, PLUGIN)
        from team_colors import normalize_constructor_id
        bad = [x["constructor_id"] for x in e if not normalize_constructor_id(x["constructor_id"])]
        check("every team resolves to a colour", not bad, bad)
    except ImportError:
        print("SKIP  team colours (team_colors.py is not in this checkout)")


def test_default_types_exclude_practice():
    snap = replay(Clock(FLAG - timedelta(seconds=1)), types=("Race",), before=FLAG).state()
    check("Race-only config ignores practice", snap is None)


def test_finish_and_hold():
    feed = replay(Clock(FLAG))
    finished = feed._finished_at
    check("finish seen at the flag", finished is not None and abs(
        (finished - FLAG).total_seconds()) < 2, finished)
    if finished is None:
        return
    feed._now = Clock(finished + timedelta(seconds=60))
    snap = feed.state()
    check("final order held a minute after the flag", snap is not None and snap["finished"])
    feed._now = Clock(finished + timedelta(seconds=feed.freshness_s + 1))
    check("and gone once the hold is over", feed.state() is None)


def test_stale_connection():
    feed = replay(Clock(FLAG - timedelta(seconds=1) + timedelta(seconds=400)), before=FLAG)
    check("no data for 400 s is not live", feed.state() is None)


def test_joined_after_flag():
    initial = copy.deepcopy(next(m for m in recording() if m.get("type") == 3))
    initial["result"]["SessionStatus"] = {"Status": "Finished"}
    beat = _parse_utc(initial["result"]["Heartbeat"]["Utc"])
    feed = SignalRLiveFeed(session_types=("Practice",), http=BoomHTTP(),
                           now_fn=Clock(beat + timedelta(seconds=5)))
    feed.feed_message(initial)
    check("connecting after the flag does not show a live board", feed.state() is None)


# ── race shapes, synthetic ─────────────────────────────────────────────
T0 = datetime(2026, 9, 13, 13, 40, 0, tzinfo=timezone.utc)


def race_feed():
    drivers = {"63": ("RUS", "Russell", "Mercedes"), "12": ("ANT", "Antonelli", "Mercedes"),
               "1": ("NOR", "Norris", "McLaren"), "16": ("LEC", "Leclerc", "Ferrari")}
    result = {
        "SessionInfo": {"Type": "Race", "Name": "Race", "Path": "2026/spain/race/",
                        "Meeting": {"Name": "Spanish Grand Prix", "Country": {"Name": "Spain"},
                                    "Circuit": {"ShortName": "Madring"}}},
        "SessionStatus": {"Status": "Started"},
        "TrackStatus": {"Status": "1", "Message": "AllClear"},
        "LapCount": {"CurrentLap": 23, "TotalLaps": 57},
        "Heartbeat": {"Utc": T0.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")},
        "DriverList": {n: {"Tla": t, "LastName": l, "TeamName": c} for n, (t, l, c) in drivers.items()},
        "TimingAppData": {"Lines": {"63": {"GridPos": "3"}, "12": {"GridPos": "1"},
                                    "1": {"GridPos": "2"}, "16": {"GridPos": "4"}}},
        "TimingData": {"Lines": {
            "63": {"Position": "1", "GapToLeader": "LAP 23",
                   "IntervalToPositionAhead": {"Value": "LAP 23"}, "NumberOfLaps": 23},
            "12": {"Position": "2", "GapToLeader": "+1.234",
                   "IntervalToPositionAhead": {"Value": "+1.234"}, "NumberOfLaps": 23},
            "1": {"Position": "3", "GapToLeader": "1L",
                  "IntervalToPositionAhead": {"Value": "1L"}, "NumberOfLaps": 22},
            "16": {"Position": "4", "GapToLeader": "", "NumberOfLaps": 9, "Retired": True}}},
    }
    feed = SignalRLiveFeed(session_types=("Race",), http=BoomHTTP(),
                           now_fn=Clock(T0 + timedelta(seconds=5)))
    feed.feed_message({"type": 3, "invocationId": "0", "result": result})
    return feed


def rows(snap):
    return [live_row_from_entry(e, snap["lap"]) for e in snap["entries"]]


def test_race_board():
    feed = race_feed()
    snap = feed.state()
    check("race is live", snap is not None)
    if not snap:
        return
    check("lap and scheduled distance", (snap["lap"], snap["total_laps"]) == (23, 57),
          (snap["lap"], snap["total_laps"]))
    check("order", [e["code"] for e in snap["entries"]] == ["RUS", "ANT", "NOR", "LEC"])
    r = rows(snap)
    check("leader reads LEADER, not LAPPED", r[0]["time"] == "LEADER", r[0])
    check("gap in seconds", r[1]["time"] == "+1.234", r[1])
    check("lapped car labelled", r[2]["status"] == "Lapped", r[2])
    check("retired car labelled from the feed's own flag", r[3]["status"] == "Retired", r[3])
    check("grid carried for places gained", snap["entries"][0]["grid"] == 3)

    for code, want in (("4", "SC"), ("6", "VSC"), ("5", "RED"), ("1", None)):
        feed_delta(feed, "TrackStatus", {"Status": code}, T0 + timedelta(seconds=2))
        check("track status %s -> %s" % (code, want), feed.state()["flag"] == want,
              feed.state()["flag"])

    feed_delta(feed, "SessionStatus", {"Status": "Aborted"}, T0 + timedelta(seconds=3))
    check("red-flag suspension stays live", feed.state() is not None)
    feed_delta(feed, "SessionStatus", {"Status": "Started"}, T0 + timedelta(seconds=3))

    feed_delta(feed, "TimingData", {"Lines": {
        "12": {"Position": "1", "GapToLeader": "LAP 24"},
        "63": {"Position": "2", "GapToLeader": "+0.500"}}}, T0 + timedelta(seconds=4))
    snap = feed.state()
    check("overtake applied", [e["code"] for e in snap["entries"]][:2] == ["ANT", "RUS"])
    r = rows(snap)
    check("new leader reads LEADER", r[0]["time"] == "LEADER", r[0])
    check("old leader reads its gap", r[1]["time"] == "+0.500", r[1])

    feed_delta(feed, "SessionInfo", {"Path": "2026/next/race/", "Type": "Race"},
               T0 + timedelta(seconds=4))
    check("a new session on the same connection starts clean", feed.state() is None)


def test_qualifying_segments():
    """Shape from F1's published feed, not yet seen live here. See _timed_figures."""
    result = {
        "SessionInfo": {"Type": "Qualifying", "Name": "Qualifying", "Path": "2026/spain/q/"},
        "SessionStatus": {"Status": "Started"},
        "Heartbeat": {"Utc": T0.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")},
        "DriverList": {"63": {"Tla": "RUS"}, "12": {"Tla": "ANT"}},
        "TimingData": {"SessionPart": 2, "Lines": {
            "63": {"Position": "1", "BestLapTimes": [{"Value": "1:33.9"}, {"Value": "1:33.412"}],
                   "Stats": [{"TimeDiffToFastest": ""}, {"TimeDiffToFastest": ""}]},
            "12": {"Position": "2", "BestLapTimes": [{"Value": "1:34.0"}, {"Value": "1:33.600"}],
                   "Stats": [{"TimeDiffToFastest": "+0.1"}, {"TimeDiffToFastest": "+0.188"}]}}},
    }
    feed = SignalRLiveFeed(session_types=("Qualifying",), http=BoomHTTP(),
                           now_fn=Clock(T0 + timedelta(seconds=5)))
    feed.feed_message({"type": 3, "invocationId": "0", "result": result})
    snap = feed.state()
    check("qualifying is live when asked for", snap is not None)
    if snap:
        e = snap["entries"]
        check("Q2 best lap, not Q1", e[0]["best_lap"] == "1:33.412", e[0]["best_lap"])
        check("Q2 gap, not Q1", e[1]["gap_to_leader"] == 0.188, e[1]["gap_to_leader"])


def test_qualifying_last_segment_wins():
    """The top ten are classified on their Q3 lap even when a Q2 lap was
    quicker, the rest on the segment that knocked them out. Shape as above."""
    result = {
        "SessionInfo": {"Type": "Qualifying", "Name": "Qualifying", "Path": "2026/spain/q/"},
        "SessionStatus": {"Status": "Started"},
        "Heartbeat": {"Utc": T0.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")},
        "DriverList": {"63": {"Tla": "RUS"}, "12": {"Tla": "ANT"}, "44": {"Tla": "HAM"}},
        "TimingData": {"SessionPart": 3, "Lines": {
            # Quicker in Q2 than in Q3; the top-level best lap is that Q2 lap.
            "63": {"Position": "1", "BestLapTime": {"Value": "1:32.950"},
                   "BestLapTimes": [{"Value": "1:33.900"}, {"Value": "1:32.950"},
                                    {"Value": "1:33.100"}],
                   "Stats": [{"TimeDiffToFastest": "+0.300"}, {"TimeDiffToFastest": ""},
                             {"TimeDiffToFastest": ""}]},
            # No Stats for Q3 yet: the gap comes from the top level.
            "12": {"Position": "2", "TimeDiffToFastest": "+0.150",
                   "BestLapTimes": [{"Value": "1:34.000"}, {"Value": "1:33.300"},
                                    {"Value": "1:33.250"}],
                   "Stats": [{"TimeDiffToFastest": "+0.400"}, {"TimeDiffToFastest": "+0.350"}]},
            # Out in Q2, on a lap slower than the Q1 one the top level carries.
            "44": {"Position": "11", "BestLapTime": {"Value": "1:33.600"},
                   "BestLapTimes": [{"Value": "1:33.600"}, {"Value": "1:33.700"}, {"Value": ""}],
                   "Stats": [{"TimeDiffToFastest": "+0.500"}, {"TimeDiffToFastest": "+0.750"},
                             {"TimeDiffToFastest": ""}]}}},
    }
    feed = SignalRLiveFeed(session_types=("Qualifying",), http=BoomHTTP(),
                           now_fn=Clock(T0 + timedelta(seconds=5)))
    feed.feed_message({"type": 3, "invocationId": "0", "result": result})
    snap = feed.state()
    check("qualifying with both shapes is live", snap is not None)
    if not snap:
        return
    e = {x["code"]: x for x in snap["entries"]}
    check("Q3 lap beats a quicker top-level best lap", e["RUS"]["best_lap"] == "1:33.100",
          e["RUS"]["best_lap"])
    check("pole has no gap, not an older segment's", e["RUS"]["gap_to_leader"] is None,
          e["RUS"]["gap_to_leader"])
    check("out in Q2: the Q2 lap", e["HAM"]["best_lap"] == "1:33.700", e["HAM"]["best_lap"])
    check("out in Q2: the Q2 gap", e["HAM"]["gap_to_leader"] == 0.75, e["HAM"]["gap_to_leader"])
    check("no Stats for the segment: the top-level gap", e["ANT"]["gap_to_leader"] == 0.15,
          e["ANT"]["gap_to_leader"])


# ── the final order ────────────────────────────────────────────────────
def test_final_snapshot_fp1():
    """The manager keeps the last practice or qualifying order on the ticker
    until the next session starts. final_snapshot() is where it gets it."""
    snap = replay(Clock(FLAG + timedelta(seconds=90))).final_snapshot()
    check("FP1's final order is there after the flag", snap is not None)
    if snap:
        e = snap["entries"]
        check("final order: whole field", len(e) == 22, len(e))
        check("final order: named", snap["session_name"] == "Practice 1", snap["session_name"])
        check("final order: marked finished", snap["finished"] is True, snap["finished"])
        check("final order: P1 has a lap time",
              bool(re.match(r"^\d:\d\d\.\d{3}$", e[0]["best_lap"] or "")), e[0]["best_lap"])
    running = replay(Clock(FLAG - timedelta(seconds=1)), before=FLAG)
    check("no final order while the session runs", running.final_snapshot() is None)
    race_only = replay(Clock(FLAG + timedelta(seconds=90)), types=("Race",))
    check("kept where only races get live cards", race_only.final_snapshot() is not None)


def test_final_snapshot_outlives_hold():
    feed = replay(Clock(FLAG))
    feed._now = Clock(FLAG + timedelta(hours=2))
    check("two hours on, the live board is gone", feed.state() is None)
    snap = feed.final_snapshot()
    check("but the final order is not", snap is not None and len(snap["entries"]) == 22,
          snap and len(snap["entries"]))


def test_final_snapshot_types():
    feed = race_feed()
    feed_delta(feed, "SessionStatus", {"Status": "Finished", "Started": "Finished"},
               T0 + timedelta(seconds=2))
    snap = feed.state()
    check("the race is over and held", snap is not None and snap["finished"])
    check("but a race's result is not kept by default", feed.final_snapshot() is None)
    off = replay(Clock(FLAG + timedelta(seconds=90)), result_types=())
    check("result_types=() keeps no practice order", off.final_snapshot() is None)


# ── the thread, against a fake hub ─────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, body=b"", payload=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class FakeHub:
    """A session streaming, a practice on, and a hub that accepts then says nothing."""

    def __init__(self, path="2026/next/fp2/", poll_status=200):
        self.path = path
        self.poll_status = poll_status
        self.negotiations = 0

    def get(self, url, **kw):
        if url.endswith("StreamingStatus.json"):
            return FakeResponse(body=json.dumps({"Status": "Available"}))
        if url.endswith("SessionInfo.json"):
            return FakeResponse(body=json.dumps({"Type": "Practice", "Name": "Practice 2",
                                                 "Path": self.path}))
        return FakeResponse(status=self.poll_status)   # a long poll with nothing new

    def post(self, url, **kw):
        if "negotiate" in url:
            self.negotiations += 1
            return FakeResponse(payload={"connectionToken": "tok"})
        return FakeResponse()


def run_thread_for(feed, seconds):
    import time
    feed.start()
    time.sleep(seconds)
    feed.stop()
    feed._thread.join(timeout=5)


def test_new_session_holds_one_connection():
    """FP2, 2026-09-11: FP1's finished state was still held when FP2's stream
    came up, and the thread reconnected every 0.6 s. It must hold one."""
    hub = FakeHub()
    feed = SignalRLiveFeed(session_types=("Practice",), http=hub)
    old = copy.deepcopy(next(m for m in recording() if m.get("type") == 3))
    old["result"]["SessionStatus"] = {"Status": "Finalised"}
    feed.feed_message(old, when=datetime.now(timezone.utc) - timedelta(hours=2))
    run_thread_for(feed, 2.5)
    check("a new session gets one connection, not a reconnect loop",
          hub.negotiations == 1, hub.negotiations)
    check("the old session's finish did not end the new connection",
          feed._done_path is None, feed._done_path)


def test_closed_connection_waits_before_reconnecting():
    hub = FakeHub(poll_status=204)
    feed = SignalRLiveFeed(session_types=("Practice",), http=hub)
    run_thread_for(feed, 2.5)
    check("a connection the server closes is not retried at once",
          hub.negotiations == 1, hub.negotiations)


def test_thread_connects_for_results():
    """With live cards for races only, a practice must still be joined, or
    there is no final order for final_snapshot() to hand over."""
    hub = FakeHub()
    feed = SignalRLiveFeed(session_types=("Race",), result_types=("Practice",), http=hub)
    run_thread_for(feed, 1.5)
    check("race-only cards still join a practice for its result",
          hub.negotiations == 1, hub.negotiations)
    hub = FakeHub()
    feed = SignalRLiveFeed(session_types=("Race",), result_types=(), http=hub)
    run_thread_for(feed, 1.5)
    check("and do not when no results are kept", hub.negotiations == 0, hub.negotiations)


def test_session_status_two_fields():
    """FP2, 2026-09-11: after a red flag the session carried on as Status
    "Inactive", Started "Started", and reading only Status took the board down
    with 18 minutes to run. Either field says running; either says over."""
    cases = [
        ({"Status": "Inactive", "Started": "Started"}, True, "after a red flag, still Started"),
        ({"Status": "Aborted", "Started": "Started"}, True, "red-flag suspension"),
        ({"Status": "Inactive"}, False, "before the start"),
        ({"Status": "Inactive", "Started": "Inactive"}, False, "before the start, both fields"),
    ]
    for status, live, label in cases:
        feed = race_feed()
        feed_delta(feed, "SessionStatus", status, T0 + timedelta(seconds=2))
        check("%s -> %s" % (label, "live" if live else "not live"),
              (feed.state() is not None) == live, json.dumps(status))
    feed = race_feed()
    feed_delta(feed, "SessionStatus", {"Status": "Finished", "Started": "Finished"},
               T0 + timedelta(seconds=2))
    snap = feed.state()
    check("chequered flag in both fields -> held as finished",
          snap is not None and snap["finished"])


def test_core_calls_update_often_enough():
    """FP2, 2026-09-11: the live poll lives in update(), and the core calls
    update() on the manifest's update_interval, else the config's -- 3600 s
    here. With no manifest key the feed was live and the cards waited up to
    an hour for someone to ask."""
    base = PLUGIN if os.path.isdir(PLUGIN) else HERE
    with open(os.path.join(base, "manifest.json"), encoding="utf-8") as fh:
        interval = json.load(fh).get("update_interval")
    check("manifest sets a short update_interval (<= 30 s)",
          isinstance(interval, (int, float)) and 0 < interval <= 30, interval)


def quali_feed(part=1, status=None, stype="Qualifying"):
    """A qualifying session in F1's shape, at the given segment."""
    result = {
        "SessionInfo": {"Type": stype, "Name": stype, "Path": "2026/spain/q/",
                        "Meeting": {"Name": "Spanish Grand Prix"}},
        "SessionStatus": status or {"Status": "Started", "Started": "Started"},
        "Heartbeat": {"Utc": T0.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")},
        "DriverList": {"1": {"Tla": "NOR"}, "12": {"Tla": "ANT"}},
        "TimingData": {"SessionPart": part, "NoEntries": [22, 16, 10], "Lines": {
            "1": {"Position": "1", "BestLapTimes": [{"Value": "1:32.100"}],
                  "Stats": [{"TimeDiffToFastest": ""}]},
            "12": {"Position": "2", "BestLapTimes": [{"Value": "1:32.300"}],
                   "Stats": [{"TimeDiffToFastest": "+0.200"}]}}},
    }
    clock = Clock(T0 + timedelta(seconds=5))
    feed = SignalRLiveFeed(session_types=("Qualifying",), http=BoomHTTP(), now_fn=clock)
    feed.feed_message({"type": 3, "invocationId": "0", "result": result})
    return feed, clock


def test_qualifying_segment_ends_are_not_the_end():
    """Qualifying, 2026-09-12: F1 ended Q1 with Finished, exactly as it ends the
    session, and the feed stored Q1's order as the result and went dark for Q2
    and Q3. The gaps below are that session's (SessionData.StatusSeries)."""
    feed, clock = quali_feed(part=1)
    at = [T0 + timedelta(seconds=10)]

    def step(topic, delta, seconds):
        at[0] += timedelta(seconds=seconds)
        clock.t = at[0] + timedelta(seconds=2)
        feed_delta(feed, topic, delta, at[0])

    def finished(snap):
        return bool(snap and snap.get("finished"))

    check("Q1 running -> live", feed.state() is not None)
    step("SessionStatus", {"Status": "Finished", "Started": "Finished"}, 1070)   # 14:18
    snap = feed.state()
    check("the Q1 flag, both fields Finished -> the board stays up, not finished",
          snap is not None and not finished(snap), snap and snap.get("finished"))
    check("and no result is stored off Q1", feed.final_snapshot() is None)
    step("Heartbeat", {"Utc": "x"}, 330)            # past the old five-minute hold
    check("5.5 min into the break -> still live", feed.state() is not None)
    check("and the thread is not done for good", not feed._finished_for_good())
    step("TimingData", {"SessionPart": 2}, 29)      # 14:23:59
    step("SessionStatus", {"Status": "Inactive"}, 9)
    check("Inactive before Q2 -> still live", feed.state() is not None)
    step("SessionStatus", {"Status": "Started", "Started": "Started"}, 52)
    check("Q2 running -> live", feed.state() is not None)
    step("SessionStatus", {"Status": "Finished", "Started": "Finished"}, 900)    # 14:40
    check("the Q2 flag -> still live, still no result",
          feed.state() is not None and not finished(feed.state())
          and feed.final_snapshot() is None)
    step("TimingData", {"SessionPart": 3}, 360)     # 14:46:00
    step("SessionStatus", {"Status": "Inactive"}, 17)
    step("SessionStatus", {"Status": "Started", "Started": "Started"}, 43)
    check("Q3 running -> live", feed.state() is not None and not finished(feed.state()))
    step("SessionStatus", {"Status": "Finished", "Started": "Finished"}, 780)    # 15:00
    check("the Q3 flag -> held as finished", finished(feed.state()))
    final = feed.final_snapshot()
    check("and the final order is kept as the result",
          bool(final) and [e["code"] for e in final["entries"]] == ["NOR", "ANT"], final)
    step("SessionStatus", {"Status": "Finalised"}, 255)
    check("Finalised -> still over", feed.final_snapshot() is not None)


def test_qualifying_edges():
    feed, _ = quali_feed(part=1, status={"Status": "Inactive", "Started": "Inactive"})
    check("before Q1 (segment 1, Inactive) -> not live", feed.state() is None)
    feed, _ = quali_feed(part=1, status={"Status": "Finished", "Started": "Finished"})
    snap = feed.state()
    check("joined in the Q1-Q2 break -> live, not finished",
          snap is not None and not snap.get("finished"), snap and snap.get("finished"))
    check("and no result off Q1", feed.final_snapshot() is None)
    feed, _ = quali_feed(part=2, status={"Status": "Inactive"})
    check("joined just before Q2 (segment 2, Inactive) -> live", feed.state() is not None)
    feed, _ = quali_feed(part=3, status={"Status": "Finished", "Started": "Finished"})
    check("joined after Q3 -> the result is there", feed.final_snapshot() is not None)
    feed, _ = quali_feed(part=1, status={"Status": "Ends", "Started": "Finished"})
    check("Ends is the end whatever the segment", feed.final_snapshot() is not None)
    feed, _ = quali_feed(part=1, status={"Status": "Finished", "Started": "Finished"},
                         stype="Practice")
    check("practice has no segments: Finished is the end", feed.final_snapshot() is not None)


def test_race_gap_interval():
    """Race rows: the time to the car ahead by default, as on TV (2026-09-13)."""
    p1 = {"position": 1, "code": "LEC", "gap_to_leader": 0.0, "interval": None, "lap": 30}
    p2 = {"position": 2, "code": "ANT", "gap_to_leader": 2.0, "interval": 2.0, "lap": 30}
    p3 = {"position": 3, "code": "VER", "gap_to_leader": 6.0, "interval": 4.0, "lap": 30}
    lapped = {"position": 15, "code": "STR", "gap_to_leader": "+1 LAP", "interval": None,
              "lap": 29}
    out = {"position": 18, "code": "ALB", "gap_to_leader": None, "interval": 3.1, "lap": 9,
           "retired": True}
    check("P1 still reads LEADER", live_row_from_entry(p1, 30)["time"] == "LEADER")
    check("P2: interval and leader gap agree", live_row_from_entry(p2, 30)["time"] == "+2.000")
    r3 = live_row_from_entry(p3, 30)
    check("P3 shows its time to P2 (+4.000), as the TV does", r3["time"] == "+4.000", r3)
    r3l = live_row_from_entry(p3, 30, race_gap="leader")
    check("race_gap leader: P3 shows its gap to the leader (+6.000)", r3l["time"] == "+6.000", r3l)
    rl = live_row_from_entry(lapped, 30)
    check("no numeric interval: the lapped label stays", rl["status"] == "Lapped", rl)
    ro = live_row_from_entry(out, 30)
    check("a retired car stays RETIRED whatever its interval", ro["time"] == "RETIRED", ro)
    timed = {"position": 3, "gap_to_leader": 0.5, "interval": 0.2, "best_lap": "1:31.000"}
    rt = live_row_from_entry(timed, None, timed=True)
    check("practice and qualifying unchanged: the gap to the fastest", rt["time"] == "+0.500", rt)
    snap = race_feed().state()
    times = [r["time"] for r in (live_row_from_entry(e, snap["lap"]) for e in snap["entries"])]
    check("the synthetic race board reads the same under intervals",
          times[:2] == ["LEADER", "+1.234"], times)


def test_fastest_lap_code():
    """The race's fastest lap so far, and whose it is (2026-09-13)."""
    from f1_live import fastest_lap_code, lap_seconds
    check("a lap time is parsed", abs(lap_seconds("1:31.234") - 91.234) < 1e-9,
          lap_seconds("1:31.234"))
    check("bare seconds are parsed", lap_seconds("91.5") == 91.5)
    check("blank and missing are None", lap_seconds("") is None and lap_seconds(None) is None)
    check("a label is None", lap_seconds("LAP 23") is None)
    e = [{"code": "LEC", "best_lap": "1:32.100"}, {"code": "ANT", "best_lap": "1:31.900"},
         {"code": "NOR", "best_lap": "1:31.234"}, {"code": "VER", "best_lap": ""}]
    check("the holder has the lowest best lap", fastest_lap_code(e) == "NOR", fastest_lap_code(e))
    check("no lap times yet: no holder",
          fastest_lap_code([{"code": "LEC", "best_lap": ""}]) is None)
    tie = [{"code": "LEC", "best_lap": "1:31.234"}, {"code": "NOR", "best_lap": "1:31.234"}]
    check("a tie goes to the car ahead", fastest_lap_code(tie) == "LEC", fastest_lap_code(tie))
    snap = race_feed().state()
    check("race entries carry best_lap", all("best_lap" in x for x in snap["entries"]))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("--", name)
            fn()
    print()
    print("%d failure(s)" % len(failures) if failures else "all passed")
    sys.exit(1 if failures else 0)
