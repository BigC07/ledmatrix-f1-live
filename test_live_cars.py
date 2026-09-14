#!/usr/bin/env python3
"""How many cars the live board shows: live.cars (2026-09-13).

Asked for before the plugin store submission: "can we make it an option to
show all twenty-two cars?" Builds the live board for a 22-car race through the
plugin's own _build_live_race_cards() and counts the cards (a header, then one
per car):

  - 10 by default; live.cars 22 shows the whole field, 15 the top fifteen;
  - more than the field shows the field; a value that is no number gives 10;
  - the favourite driver is still added when outside the cars shown, never twice;
  - recent_races.top_finishers, which set it before, no longer does;
  - the config schema takes 1 to 30, and the web UI lists the setting.

    cd /home/admin/LEDMatrix && python3 plugin-repos/f1-live/test_live_cars.py
"""
import json
import logging
import os
import sys
import types

CORE = "/home/admin/LEDMatrix"
PLUGIN = os.environ.get("F1_LIVE_PLUGIN", CORE + "/plugin-repos/f1-live")
sys.path.insert(0, PLUGIN)
sys.path.insert(0, CORE)
if os.path.isdir(CORE):
    os.chdir(CORE)
logging.disable(logging.CRITICAL)

import manager as M  # noqa: E402
from f1_renderer import F1Renderer  # noqa: E402

PLUGIN_CLS = next(v for v in vars(M).values()
                  if isinstance(v, type) and hasattr(v, "_build_live_race_cards"))
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


GRID = [("NOR", "Norris", "McLaren"), ("PIA", "Piastri", "McLaren"),
        ("VER", "Verstappen", "Red Bull Racing"), ("HAD", "Hadjar", "Red Bull Racing"),
        ("LEC", "Leclerc", "Ferrari"), ("HAM", "Hamilton", "Ferrari"),
        ("RUS", "Russell", "Mercedes"), ("ANT", "Antonelli", "Mercedes"),
        ("ALO", "Alonso", "Aston Martin"), ("STR", "Stroll", "Aston Martin"),
        ("GAS", "Gasly", "Alpine"), ("COL", "Colapinto", "Alpine"),
        ("ALB", "Albon", "Williams"), ("SAI", "Sainz", "Williams"),
        ("LAW", "Lawson", "Racing Bulls"), ("LIN", "Lindblad", "Racing Bulls"),
        ("OCO", "Ocon", "Haas F1 Team"), ("BEA", "Bearman", "Haas F1 Team"),
        ("HUL", "Hulkenberg", "Audi"), ("BOR", "Bortoleto", "Audi"),
        ("PER", "Perez", "Cadillac"), ("BOT", "Bottas", "Cadillac")]


def race():
    return {"session_key": 1, "session_type": "Race", "session_name": "Race",
            "meeting_name": "Spanish Grand Prix", "lap": 30, "flag": None, "finished": False,
            "entries": [{"position": i + 1, "code": c, "last_name": n, "constructor_id": t,
                         "gap_to_leader": None if i == 0 else 1.5 * i,
                         "interval": None if i == 0 else 1.5, "lap": 30, "grid": i + 1,
                         "best_lap": "1:3%d.%03d" % (5 + i % 3, 100 + i)}
                        for i, (c, n, t) in enumerate(GRID)]}


cfg = json.load(open(CORE + "/config/config.json", encoding="utf-8"))["f1-live"]
RENDERER = F1Renderer(128, 32, dict(cfg, timezone="America/New_York"))


def board(cars=None, fav="", top_finishers=None):
    c = dict(cfg)
    c["live"] = {k: v for k, v in (cfg.get("live") or {}).items() if k != "cars"}
    if cars is not None:
        c["live"]["cars"] = cars
    if top_finishers is not None:
        c["recent_races"] = dict(cfg.get("recent_races") or {}, top_finishers=top_finishers)
    stub = types.SimpleNamespace(_scroll_renderer=RENDERER, config=c, favorite_driver=fav,
                                 _live_race_name=lambda s: "")
    return PLUGIN_CLS._build_live_race_cards(stub, race())


n = len(board())
check("by default, a header and ten cars", n == 11, n)
n = len(board(22))
check("live.cars 22: the whole field", n == 23, n)
n = len(board(15))
check("live.cars 15: the top fifteen", n == 16, n)
n = len(board(30))
check("more than the field: the field", n == 23, n)
n = len(board("lots"))
check("a value that is no number: ten", n == 11, n)
n = len(board(fav="BOT"))
check("the favourite outside the ten is still added", n == 12, n)
n = len(board(22, fav="BOT"))
check("and not twice when inside", n == 23, n)
n = len(board(top_finishers=5))
check("recent_races.top_finishers no longer sets it", n == 11, n)
check("every card is 128x32", all(c.size == (128, 32) for c in board(22)))

schema = json.load(open(os.path.join(PLUGIN, "config_schema.json"), encoding="utf-8"))
live = schema["properties"]["live"]
cars = live["properties"].get("cars") or {}
check("the schema has live.cars: an integer, 10 by default, 1 to 30",
      cars.get("type") == "integer" and cars.get("default") == 10
      and cars.get("minimum") == 1 and cars.get("maximum") == 30, cars)
check("and the web UI lists it", "cars" in live.get("x-propertyOrder", []))
try:
    import jsonschema
except ImportError:
    jsonschema = None
    print("SKIP  the schema checks: jsonschema is not installed")
if jsonschema is not None:
    v = jsonschema.validators.validator_for(live)(live)
    check("the schema takes 22", not list(v.iter_errors({"cars": 22})))
    check("and refuses 31 and 0", bool(list(v.iter_errors({"cars": 31})))
          and bool(list(v.iter_errors({"cars": 0}))))

print()
print("%d failure(s)" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
