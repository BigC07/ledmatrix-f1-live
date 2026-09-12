#!/usr/bin/env python3
"""Offline checks for the F1 plugin's Vegas alert (get_vegas_alert).

The plugin half of the alert card asked for on 2026-09-12: when a red flag,
safety car or VSC comes out, the plugin offers the new live header card, and the
core scroll (patches/patch_core_vegas_alert.py) splices it into the strip just
ahead of the screen. This builds the plugin without __init__ and drives
_poll_live_race with a stub feed; the snapshot is FP2's real final order and the
cards come from the real renderer, so run it on the Pi:

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_vegas_alert.py
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
import time

CORE = "/home/admin/LEDMatrix"
PLUGIN = os.environ.get("F1_LIVE_PLUGIN", CORE + "/plugin-repos/f1-live")
sys.path.insert(0, PLUGIN)
sys.path.insert(0, CORE)
if os.path.isdir(CORE):
    os.chdir(CORE)
logging.disable(logging.CRITICAL)

import manager as M  # noqa: E402
from f1_renderer import F1Renderer  # noqa: E402
from f1_signalr import SignalRLiveFeed  # noqa: E402

PLUGIN_CLS = next(v for v in vars(M).values()
                  if isinstance(v, type) and hasattr(v, "get_vegas_alert"))
FIXTURE = "signalr_2026_spain_fp2_final.jsonl"
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


cfg = json.load(open(CORE + "/config/config.json", encoding="utf-8"))["f1-live"]


def base_snapshot():
    for d in (os.path.join(PLUGIN, "fixtures"), os.path.expanduser("~/f1fixtures")):
        path = os.path.join(d, FIXTURE)
        if os.path.exists(path):
            break
    feed = SignalRLiveFeed()
    for line in open(path, encoding="utf-8"):
        msg = json.loads(line).get("msg")
        if msg:
            feed.feed_message(msg)
    snap = feed.final_snapshot()
    snap["finished"] = False
    return snap


BASE = base_snapshot()


class Feed:
    def __init__(self):
        self.snap = None

    def state(self):
        return self.snap

    def final_snapshot(self):
        return None


def snap(flag=None, key=21, swap=False):
    s = copy.deepcopy(BASE)
    s.update(session_key=key, session_name="Qualifying", session_type="Qualifying", flag=flag)
    if swap:                       # an order change under the same flag
        e = s["entries"]
        e[0], e[1] = e[1], e[0]
    return s


def plugin():
    p = PLUGIN_CLS.__new__(PLUGIN_CLS)
    p.logger = logging.getLogger("test")
    p.config = cfg
    p.favorite_driver = ""
    p._scroll_renderer = F1Renderer(128, 32, cfg)
    p._live_race_name = lambda s: ""
    p._live_feed = Feed()
    p._live_snapshot = None
    p._live_cards_sig = None
    p._vegas_live_race_cards = []
    p._vegas_alert = None
    p._alert_flag = None
    p._poll_last_session = lambda s: None      # the session-result side is tested elsewhere
    return p


def poll(p, s):
    p._live_feed.snap = s
    p._poll_live_race()


def alert_id(p):
    a = p.get_vegas_alert()
    return a[0] if a else None


def test_nothing_until_a_flag():
    p = plugin()
    check("nothing offered at first", p.get_vegas_alert() is None)
    poll(p, snap())
    check("a green session offers nothing", p.get_vegas_alert() is None)


def test_red_flag_offers_the_header_once():
    p = plugin()
    poll(p, snap())
    poll(p, snap(flag="RED"))
    a = p.get_vegas_alert()
    check("a red flag offers an alert", a is not None)
    if not a:
        return
    aid, card = a
    check("its id names the flag", ":RED:" in aid, aid)
    check("the card is 128x32 RGB", card.size == (128, 32) and card.mode == "RGB",
          (card.size, card.mode))
    check("and is the header the F1 block shows",
          card.tobytes() == p._vegas_live_race_cards[0].convert("RGB").tobytes())
    px = card.load()
    check("with the red flag badge",
          any(px[x, y] == (230, 0, 0) for x in range(64, 128) for y in range(14, 32)))
    poll(p, snap(flag="RED", swap=True))
    check("a rebuild under the same flag offers nothing new", alert_id(p) == aid,
          (alert_id(p), aid))


def test_each_new_flag_is_a_new_alert():
    p = plugin()
    poll(p, snap(flag="SC"))
    first = alert_id(p) or ""
    check("a safety car offers an alert", ":SC:" in first, first)
    poll(p, snap(flag=None))
    check("going green offers nothing new", alert_id(p) == first)
    poll(p, snap(flag="VSC"))
    second = alert_id(p) or ""
    check("a VSC after it is a new alert", ":VSC:" in second and second != first,
          (first, second))
    poll(p, snap(flag=None))
    time.sleep(0.01)
    poll(p, snap(flag="VSC", swap=True))
    third = alert_id(p) or ""
    check("the same flag coming back is a new alert too", third not in ("", second),
          (second, third))


def test_alert_expires():
    p = plugin()
    poll(p, snap(flag="RED"))
    aid, card, created = p._vegas_alert
    p._vegas_alert = (aid, card, created - p._ALERT_TTL_S - 1)
    check("an alert older than its TTL is withdrawn", p.get_vegas_alert() is None)


def test_session_end_resets():
    p = plugin()
    poll(p, snap(flag="RED"))
    first = alert_id(p)
    poll(p, None)
    check("session over: the flag memory is cleared", p._alert_flag is None)
    poll(p, snap(flag="RED", key=22))
    second = alert_id(p) or ""
    check("the next session's red flag is a new alert", second != first and ":RED:" in second,
          (first, second))


def test_test_file_makes_one_alert():
    p = plugin()
    fd, path = tempfile.mkstemp(prefix="f1-alert-test-")
    os.close(fd)
    p._ALERT_TEST_FILE = path
    p._maybe_test_alert()
    aid = alert_id(p) or ""
    check("the test file offers one alert", aid.startswith("test:"), aid)
    check("and the file is removed", not os.path.exists(path))
    a = p.get_vegas_alert()
    check("the test card is 128x32 with the badge", bool(a) and a[1].size == (128, 32) and any(
        a[1].load()[x, y] == (230, 0, 0) for x in range(64, 128) for y in range(14, 32)))
    p._vegas_alert = None
    p._maybe_test_alert()
    check("no file, no alert", p.get_vegas_alert() is None)


def test_get_vegas_alert_is_cheap():
    p = plugin()
    poll(p, snap(flag="RED"))
    t = time.perf_counter()
    for _ in range(1000):
        p.get_vegas_alert()
    check("1000 calls take well under 50 ms (it runs on the render thread)",
          time.perf_counter() - t < 0.05)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("-- " + name)
            fn()
    print("\n%d failed" % len(failures) if failures else "\nALL PASS")
    sys.exit(1 if failures else 0)
