#!/usr/bin/env python3
"""The manifest and the example config, as the plugin store's review checks them (2026-09-13).

From ChuckBuilds' SUBMISSION.md and VERIFICATION.md: the version is semver and
matches the newest versions[] entry, last_updated is that entry's release date,
the display modes collide with no other plugin's (F1 Scoreboard's are f1_*, so
these are f1_live_*, and manager.py must handle exactly those), there is a
LICENSE, example_config.json is accepted by the config schema, and the code
reaches no fixed path outside the plugin's directory.

Needs neither LEDMatrix nor the network; jsonschema only for the schema check.

    python3 test_manifest.py
"""
import json
import os
import re
import sys

PLUGIN = os.environ.get("F1_LIVE_PLUGIN", os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_MODES = {"f1_driver_standings", "f1_constructor_standings", "f1_recent_races",
                  "f1_upcoming", "f1_qualifying", "f1_practice", "f1_sprint", "f1_calendar"}
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(detail)))
    if not cond:
        failures.append(name)


def load(name):
    with open(os.path.join(PLUGIN, name), encoding="utf-8") as fh:
        return json.load(fh)


m = load("manifest.json")
newest = (m.get("versions") or [{}])[0]
check("version is semver", re.fullmatch(r"\d+\.\d+\.\d+", m.get("version", "")), m.get("version"))
check("version matches the newest versions[] entry", newest.get("version") == m.get("version"),
      (newest.get("version"), m.get("version")))
check("last_updated is the newest entry's release date",
      m.get("last_updated") == newest.get("released"), (m.get("last_updated"), newest.get("released")))
check("the newest entry names a minimum LEDMatrix", bool(newest.get("ledmatrix_min_version")))
check("every versions[] entry uses ledmatrix_min_version, not the old ledmatrix_min",
      all("ledmatrix_min" not in v for v in m.get("versions") or []))
for key in ("id", "name", "version", "author", "class_name", "entry_point", "display_modes",
            "compatible_versions"):
    check("manifest has %s" % key, key in m)

modes = list(m.get("display_modes") or [])
check("display modes are all f1_live_*", modes and all(d.startswith("f1_live_") for d in modes), modes)
check("none of them is one of F1 Scoreboard's", not set(modes) & UPSTREAM_MODES,
      sorted(set(modes) & UPSTREAM_MODES))
with open(os.path.join(PLUGIN, m.get("entry_point") or "manager.py"), encoding="utf-8") as fh:
    src = fh.read()
bases = {d[len("f1_"):] for d in UPSTREAM_MODES}
# manager.py's other f1_live_* strings are cache keys (f1_live_last_session, f1_live_podium).
used = {s for s in re.findall(r'"(f1_live_[a-z_]+)"', src) if s[len("f1_live_"):] in bases}
check("manager.py uses exactly the declared modes", used == set(modes), sorted(used ^ set(modes)))
check("no old mode name is left in manager.py",
      not re.search(r'"f1_(%s)"' % "|".join(d[3:] for d in UPSTREAM_MODES), src))
check("class_name is a class in the entry point",
      re.search(r"^class %s\b" % re.escape(m.get("class_name", "")), src, re.M) is not None)

check("a LICENSE ships with the plugin", os.path.isfile(os.path.join(PLUGIN, "LICENSE")))
ex = load("example_config.json")
check("example_config.json holds a block for the plugin's id", isinstance(ex.get(m.get("id")), dict))
try:
    import jsonschema
except ImportError:
    jsonschema = None
    print("SKIP  the schema check: jsonschema is not installed")
if jsonschema is not None and isinstance(ex.get(m.get("id")), dict):
    schema = load(m.get("config_schema") or "config_schema.json")
    errors = list(jsonschema.validators.validator_for(schema)(schema).iter_errors(ex[m["id"]]))
    check("the config schema accepts the example", not errors,
          ["%s: %s" % (list(e.path), e.message[:120]) for e in errors[:3]])

code = {f: open(os.path.join(PLUGIN, f), encoding="utf-8").read()
        for f in os.listdir(PLUGIN) if f.endswith(".py") and not f.startswith("test_")}
fixed = sorted(f for f, text in code.items() if re.search(r"[\"'](/var/cache|/home/)", text))
check("no fixed /var/cache or /home path in the plugin's code", not fixed, fixed)
check("the alert test file is in the plugin's own directory",
      '_ALERT_TEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert-test")'
      in src)

print()
print("%d failure(s)" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
