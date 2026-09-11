"""Live race timing from OpenF1, for the forked f1-live plugin.

Why this exists
---------------
The upstream plugin's `detect_live_session()` is a clock guess: it decides a
session is live when the current time falls inside its scheduled window (a
Grand Prix being 120 minutes from the published start). Nothing is fetched. So
during a race the ticker keeps showing the *previous* race, and the only thing
"live" changes is the update interval and a badge on cards we do not display.

OpenF1 (https://openf1.org) carries the actual session feed. The upstream
plugin already talks to it for practice data, so the dependency is not new --
see OPENF1_BASE in f1_data.py.

**No longer the live source, as of 2026-09-11.** Tried in Spanish GP FP1,
OpenF1 answered every unauthenticated request with 401 while the session ran
-- global access, past sessions included, is restricted to authenticated
users until the session ends -- so this feed would have shown nothing during a
race. Live timing now comes from F1's own feed (f1_signalr.py). This module
stays for replay and fixtures, which OpenF1 still serves between sessions, and
as `live.source: "openf1"` for anyone with a key.

What "live" means here
----------------------
Not the clock. A session counts as live when its newest datum is *recent*:

    newest position/interval row is younger than `freshness_s` (default 300 s)

That is evidence rather than a schedule, so a delayed start, a red flag that
stretches a race past its window, or a cancelled session all behave correctly,
and a finished session stops being "live" on its own without a special case.

Honest limitations
------------------
* OpenF1 lags the television broadcast, typically by a few seconds but
  sometimes by up to a minute. This is not TV-instant timing and should not be
  sold as such.
* It is a free community API with no uptime guarantee. Every call here is
  wrapped; any failure degrades to "not live" and the plugin falls back to its
  normal cards rather than showing a broken one.
* **It rate-limits.** Pulling the full `/intervals` history for a race is
  ~22,000 rows, and doing that a few times in quick succession earns a 429.
  Hence the response cache below, and hence live polling uses a `date>` cursor
  so each poll fetches only what has appeared since the last one. A 429 parks
  the feed for a cool-off rather than hammering harder.
* A card is a frozen bitmap spliced into the scroll strip, so what a viewer
  sees was true when the strip was last built, not at the instant it passes
  their eyes. Rebuild cadence bounds that, not this module.

Testing without a race
----------------------
`replay_session_key` + `replay_at` pin the feed to a finished session and a
timestamp inside it, so the whole live path can be exercised against a real
race. Verified against Monza 2026 (session_key 11361): at 14:00Z the order is
RUS, VER, ANT, PIA, HAM, GAS; at the flag it is ANT, RUS, VER, NOR, PIA, which
matches the published classification.

`fixture_dir` reads those same endpoints from JSON files instead of calling
OpenF1, so iteration does not spend the rate limit. Pull once:

    ~/f1fixtures/{sessions,drivers,position,intervals,laps,race_control,stints}.json
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

OPENF1_BASE = "https://api.openf1.org/v1"

_DEFAULT_TIMEOUT = 8

# Per-endpoint cache lifetime, seconds. The running order and gaps are the only
# things that move quickly; identities never change within a session, and the
# session list barely changes at all. These bound how hard a race weekend can
# hit a free API.
_TTL = {
    "sessions": 60,
    "drivers": 3600,
    "position": 10,
    "intervals": 10,
    "laps": 20,
    "race_control": 20,
}
_REPLAY_TTL = 3600          # replayed data is historical and immutable
_RATE_LIMIT_COOLOFF = 120   # seconds parked after a 429

# Endpoints that publish a row per driver per few seconds. A whole race of
# /intervals is ~22,000 rows and pulling that repeatedly is what earns a 429,
# so these are always asked for with a bounded window: a cursor when live, a
# lookback from the replay instant otherwise. Only the newest row per driver
# is ever used, so a window costs nothing.
_HIGH_VOLUME = ("intervals",)

# Which field carries a row's timestamp. /laps stamps date_start, everything
# else uses date. The incremental cursor needs this to advance per endpoint.
_DATE_KEY = {"laps": "date_start"}
_REPLAY_LOOKBACK = timedelta(minutes=10)

# Do not hammer OpenF1 looking for a race that cannot be running. We still
# poll through scheduled end + this window, because a red flag can stretch a
# Grand Prix well past its published date_end.
_POST_SESSION_GRACE = timedelta(hours=2)
_PRE_SESSION_LEAD = timedelta(minutes=10)

# A car this many laps behind the leader is out, not merely lapped. LEC at
# Monza 2026 sat on lap 2 with the field on 15 and OpenF1 still labelled the
# gap "+1 LAP"; treating a large deficit as RETIRED is what stops that
# meaningless figure reaching the card.
_RETIRED_LAP_DEFICIT = 5


def _parse(ts: str) -> Optional[datetime]:
    """OpenF1 timestamps are ISO with an offset; tolerate a missing one."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_gap(gap: Any, position: Optional[int],
               driver_lap: Optional[int] = None,
               leader_lap: Optional[int] = None,
               retired: bool = False) -> Tuple[str, str]:
    """(time_str, status) for render_race_row().

    Leader -> LEADER. Lapped -> LAPPED. Retired -> RETIRED. Everyone else
    a +seconds gap. Never a gap figure for a car that is no longer racing.
    """
    # F1's own feed says so outright. The lap-deficit test below is the
    # inference OpenF1 forced, and stays for that path.
    if retired:
        return "RETIRED", "Retired"
    if (leader_lap is not None and driver_lap is not None
            and (leader_lap - driver_lap) >= _RETIRED_LAP_DEFICIT):
        return "RETIRED", "Retired"
    if isinstance(gap, str) and "LAP" in gap.upper():
        return "LAPPED", "Lapped"
    if position == 1:
        return "LEADER", "Finished"
    if isinstance(gap, (int, float)):
        return "+%.3f" % float(gap), "Finished"
    return "", "Finished"


def live_header_title(state: Dict[str, Any], race_name: str = "") -> str:
    """Title for the live header card: `ITALIAN GP · LAP 15` when we have a
    name, otherwise `MONZA · LAP 15`."""
    if race_name:
        short = race_name.replace("Grand Prix", "GP").strip().upper()
    else:
        short = (state.get("circuit") or state.get("country") or "RACE").upper()
    lap = state.get("lap")
    if lap:
        return "%s · LAP %d" % (short, lap)
    return short


def live_row_from_entry(entry: Dict[str, Any],
                        leader_lap: Optional[int] = None,
                        timed: bool = False) -> Dict[str, Any]:
    """Shape a snapshot entry for F1Renderer.render_race_row().

    `timed` is practice and qualifying, which run on best laps rather than
    on track position: P1 shows the time to beat, everyone else how far off
    it they are. No LEADER, LAPPED or RETIRED there -- a car in the garage
    for twenty minutes of FP1 is not out of anything, and lap counts differ
    by design, so the lap-deficit test would mark half the field retired.
    """
    if timed:
        gap = entry.get("gap_to_leader")
        if entry.get("position") == 1 or gap in (None, ""):
            time_str = entry.get("best_lap") or ""
        elif isinstance(gap, (int, float)):
            time_str = "+%.3f" % float(gap)
        else:
            time_str = str(gap)
        status = "Finished"
    else:
        time_str, status = format_gap(
            entry.get("gap_to_leader"),
            entry.get("position"),
            entry.get("lap"),
            leader_lap,
            retired=bool(entry.get("retired")))
    grid = entry.get("grid") or 0
    try:
        grid = int(grid)
    except (TypeError, ValueError):
        grid = 0
    return {
        "position": entry.get("position"),
        "last_name": entry.get("last_name") or "",
        "code": entry.get("code") or "",
        "constructor_id": entry.get("constructor_id") or "",
        "time": time_str,
        "status": status,
        "grid": grid,
    }


class LiveRaceFeed:
    """Reads the current session from OpenF1 and reduces it to one snapshot."""

    def __init__(self, session: Optional[requests.Session] = None,
                 logger: Optional[logging.Logger] = None,
                 freshness_s: int = 300,
                 replay_session_key: Optional[int] = None,
                 replay_at: Optional[str] = None,
                 fixture_dir: Optional[str] = None):
        self.http = session or requests.Session()
        self.log = logger or logging.getLogger("f1_live")
        self.freshness_s = freshness_s
        self.replay_session_key = replay_session_key
        self.replay_at = _parse(replay_at) if replay_at else None
        # f1-live: fixture_dir -- JSON dumps of OpenF1 endpoints, so tests
        # never spend the rate limit. Live polling still uses _get() HTTP.
        self.fixture_dir = fixture_dir
        # Folded state for the single-value endpoints, carried across polls so
        # they can be fetched incrementally like the per-driver ones.
        self._max_lap = None
        self._laps_per_driver = {}
        self._flag_state_cache = (None, None)

        self._drivers: Dict[int, Dict[str, Any]] = {}
        self._drivers_for_session: Optional[int] = None
        self._last_error_log = 0.0
        self._cache: Dict[tuple, tuple] = {}
        self._blocked_until = 0.0
        # Rolling "latest row per driver", merged across polls. Live polling
        # asks only for rows newer than what we already hold, so a poll during
        # a race moves a handful of rows rather than the ~22,000 that a full
        # /intervals history costs.
        self._merged: Dict[str, Dict[int, Dict]] = {"position": {}, "intervals": {}}
        self._cursor: Dict[str, str] = {}
        self._merged_session: Optional[int] = None
        # Starting grid: earliest position per driver, captured on the first
        # full position fetch. Empty if we joined too late to have seen it --
        # the renderer must not draw a places-gained arrow from a guess.
        self._grid: Dict[int, int] = {}
        self._grid_when: Dict[int, datetime] = {}

    # ── HTTP / fixtures ─────────────────────────────────────────────────
    def _load_fixture(self, path: str) -> Optional[List[Dict]]:
        """Read `{fixture_dir}/{path}.json`. None on any failure -- never raises."""
        fp = os.path.join(self.fixture_dir, "%s.json" % path)
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            if time.time() - self._last_error_log > 60:
                self._last_error_log = time.time()
                self.log.warning("OpenF1 fixture missing: %s", fp)
            return None
        except Exception as exc:  # noqa: BLE001
            if time.time() - self._last_error_log > 60:
                self._last_error_log = time.time()
                self.log.warning("OpenF1 fixture unreadable %s: %s", fp, exc)
            return None
        if not isinstance(data, list):
            return None
        return data

    def _get(self, path: str, **params) -> Optional[List[Dict]]:
        """One OpenF1 call, cached. Returns None on any failure -- never raises.

        Failures are logged at most once a minute: a race weekend polls this
        every few seconds and a flapping API would otherwise bury the journal,
        which is how the BDF font fallback stayed hidden for a day.

        Fixtures short-circuit HTTP entirely. Cache still applies so a 22,000
        row intervals file is parsed once, not once per poll.
        """
        now = time.time()

        if self.fixture_dir:
            fkey = ("fixture", path)
            hit = self._cache.get(fkey)
            if hit and now < hit[0]:
                return hit[1]
            data = self._load_fixture(path)
            if data is not None:
                self._cache[fkey] = (now + _REPLAY_TTL, data)
            return data

        key = (path, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and now < hit[0]:
            return hit[1]

        # Parked by a 429: hand back whatever we last saw rather than nothing.
        # Checking this before the cache (the first version of this) meant a
        # rate-limit blanked the display for two minutes even though a perfectly
        # good snapshot was sitting in memory.
        if now < self._blocked_until:
            return hit[1] if hit else None

        try:
            r = self.http.get("%s/%s" % (OPENF1_BASE, path),
                              params=params, timeout=_DEFAULT_TIMEOUT)
            if r.status_code == 429:
                # Park rather than retry. Serving the previous snapshot for a
                # couple of minutes is far better than being cut off entirely.
                self._blocked_until = now + _RATE_LIMIT_COOLOFF
                if now - self._last_error_log > 60:
                    self._last_error_log = now
                    self.log.warning(
                        "OpenF1 rate-limited this client; backing off %ds",
                        _RATE_LIMIT_COOLOFF)
                return hit[1] if hit else None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return None
            ttl = _REPLAY_TTL if self.replay_at else _TTL.get(path, 15)
            self._cache[key] = (now + ttl, data)
            return data
        except Exception as exc:  # noqa: BLE001 - any failure means "no data"
            if now - self._last_error_log > 60:
                self._last_error_log = now
                self.log.warning("OpenF1 %s unavailable: %s", path, exc)
            # Stale beats blank while a race is running.
            return hit[1] if hit else None

    # ── session ─────────────────────────────────────────────────────────
    def current_session(self) -> Optional[Dict]:
        """Session metadata for the session in progress, or the replay target."""
        if self.replay_session_key:
            rows = self._get("sessions", session_key=self.replay_session_key)
        else:
            rows = self._get("sessions", session_key="latest")
        return rows[0] if rows else None

    def _now(self) -> datetime:
        return self.replay_at or datetime.now(timezone.utc)

    def _in_live_window(self, session: Dict) -> bool:
        """Cheap schedule gate so a finished race does not cost a position fetch.

        Liveness itself is still decided by data freshness. This only skips
        the heavy endpoints when the session cannot possibly be running:
        more than 10 minutes before lights out, or more than two hours after
        the published end (red-flag overruns of that length are the reason
        the grace exists at all).
        """
        if self.replay_at:
            return True
        now = datetime.now(timezone.utc)
        start = _parse(session.get("date_start") or "")
        end = _parse(session.get("date_end") or "")
        if start and now < start - _PRE_SESSION_LEAD:
            return False
        if end and now > end + _POST_SESSION_GRACE:
            return False
        return True

    def _drivers_map(self, session_key: int) -> Dict[int, Dict[str, Any]]:
        """driver_number -> identity. Fetched once per session, then cached."""
        if self._drivers_for_session == session_key and self._drivers:
            return self._drivers
        rows = self._get("drivers", session_key=session_key)
        if not rows:
            return self._drivers if self._drivers_for_session == session_key else {}
        self._drivers = {
            int(d["driver_number"]): {
                "code": d.get("name_acronym") or "",
                "first_name": d.get("first_name") or "",
                "last_name": d.get("last_name") or "",
                # normalize_constructor_id() in team_colors.py already maps
                # OpenF1 team names ("Red Bull Racing", "Audi") onto the
                # plugin's own ids, so the team colour and logo lookups that
                # the race-row renderer uses work with no extra table here.
                "constructor_id": d.get("team_name") or "",
                "team_name": d.get("team_name") or "",
            }
            for d in rows if d.get("driver_number") is not None
        }
        self._drivers_for_session = session_key
        return self._drivers

    # ── reduction helpers ───────────────────────────────────────────────
    def _reset_session(self, session_key: int) -> None:
        self._merged = {"position": {}, "intervals": {}}
        self._cursor = {}
        self._merged_session = session_key
        self._grid = {}
        self._grid_when = {}
        self._max_lap = None
        self._laps_per_driver = {}
        self._flag_state_cache = (None, None)

    def _note_grid(self, num: int, when: datetime, position: Any) -> None:
        """Keep the earliest position we have seen for this driver."""
        if position is None:
            return
        prev = self._grid_when.get(num)
        if prev is None or when < prev:
            try:
                self._grid[num] = int(position)
            except (TypeError, ValueError):
                return
            self._grid_when[num] = when

    def _grid_for_session(self, session: Dict) -> Dict[int, int]:
        """Starting grid, or {} if we never saw a start-of-session snapshot.

        The places-gained arrow is meaningless without a real grid. If the
        earliest position row is well after lights out we joined late and
        must not invent one.
        """
        if not self._grid:
            return {}
        start = _parse(session.get("date_start") or "")
        earliest = min(self._grid_when.values())
        if start and earliest > start + timedelta(minutes=2):
            return {}
        return dict(self._grid)

    def _incremental(self, path: str, session_key: int) -> List[Dict]:
        """Rows for `path` that have appeared since the last poll.

        Running order and gaps fold per driver in _rolling_latest(). Lap count
        and flag state fold into single values instead, but they need the same
        cursor. Without it they were almost the entire cost of a race: /laps is
        ~500 KB and /race_control ~40 KB for one Grand Prix, so re-pulling both
        every 20 s for two hours is ~190 MB across 720 full-history requests --
        98% of the traffic, and the exact shape of request that earned a 429
        during development.

        Replay and fixtures still read the session whole; both are immutable.
        """
        if self._merged_session != session_key:
            self._reset_session(session_key)
        params = {"session_key": session_key}
        cursor = self._cursor.get(path)
        if cursor and not self.replay_at and not self.fixture_dir:
            params["date>"] = cursor
        rows = self._get(path, **params)
        if not rows:
            return []
        date_key = _DATE_KEY.get(path, "date")
        newest = cursor
        for row in rows:
            iso = row.get(date_key)
            if iso and (newest is None or iso > newest):
                newest = iso
        if newest and not self.replay_at and not self.fixture_dir:
            self._cursor[path] = newest
        return rows

    def _rolling_latest(self, path: str, session_key: int) -> Dict[int, Dict]:
        """Newest row per driver for `path`, maintained incrementally.

        Replay fetches the whole session once (it is immutable, and the cache
        keeps it). Live fetches only rows newer than the cursor and merges them
        over what we already hold, which is what keeps a race weekend inside
        OpenF1's rate limit.
        """
        if self._merged_session != session_key:
            self._reset_session(session_key)

        params = {"session_key": session_key}
        cursor = self._cursor.get(path)
        if cursor and not self.replay_at and not self.fixture_dir:
            params["date>"] = cursor
        elif self.replay_at and path in _HIGH_VOLUME and not self.fixture_dir:
            params["date>"] = (self.replay_at - _REPLAY_LOOKBACK).isoformat()

        rows = self._get(path, **params)
        if rows is None:
            return self._merged.get(path, {})

        held = self._merged.setdefault(path, {})
        newest_seen = cursor
        for row in rows:
            num = row.get("driver_number")
            when = _parse(row.get("date", ""))
            if num is None or when is None:
                continue
            if path == "position":
                self._note_grid(num, when, row.get("position"))
            if self.replay_at and when > self.replay_at:
                continue
            prev = held.get(num)
            if prev is None or when > prev["_when"]:
                stored = dict(row)
                stored["_when"] = when
                held[num] = stored
            iso = row.get("date")
            if iso and (newest_seen is None or iso > newest_seen):
                newest_seen = iso
        if newest_seen and not self.replay_at:
            self._cursor[path] = newest_seen
        return held

    @staticmethod
    def _latest_per_driver(rows: List[Dict], cutoff: Optional[datetime]) -> Dict[int, Dict]:
        """Keep the newest row per driver, ignoring anything after `cutoff`."""
        latest: Dict[int, Dict] = {}
        for row in rows:
            num = row.get("driver_number")
            when = _parse(row.get("date", ""))
            if num is None or when is None:
                continue
            if cutoff and when > cutoff:
                continue
            prev = latest.get(num)
            if prev is None or when > prev["_when"]:
                stored = dict(row)
                stored["_when"] = when
                latest[num] = stored
        return latest

    # ── the snapshot ────────────────────────────────────────────────────
    def state(self) -> Optional[Dict[str, Any]]:
        """Current race state, or None when nothing is live.

        None is the normal answer most of the time and is not an error; the
        caller simply keeps showing its usual cards.
        """
        session = self.current_session()
        if not session:
            return None

        # Only races. Practice and qualifying already have their own sections
        # and their running order means something different.
        if (session.get("session_type") or "").lower() != "race":
            return None

        session_key = session.get("session_key")
        if session_key is None:
            return None

        if not self._in_live_window(session):
            return None

        cutoff = self.replay_at
        latest_pos = self._rolling_latest("position", session_key)
        if not latest_pos:
            return None

        # Liveness is decided by the data, not the clock: is the newest datum
        # recent? In replay this is trivially true by construction.
        newest = max(r["_when"] for r in latest_pos.values())
        if not self.replay_at:
            age = (datetime.now(timezone.utc) - newest).total_seconds()
            if age > self.freshness_s:
                return None

        drivers = self._drivers_map(session_key)
        intervals = self._rolling_latest("intervals", session_key)
        grid = self._grid_for_session(session)
        lap, total_laps, laps_by_driver = self._lap_state(session_key, cutoff)

        entries = []
        for num, row in latest_pos.items():
            ident = drivers.get(num, {})
            iv = intervals.get(num, {})
            entries.append({
                "position": row.get("position"),
                "driver_number": num,
                "code": ident.get("code") or str(num),
                "last_name": ident.get("last_name") or "",
                "first_name": ident.get("first_name") or "",
                "constructor_id": ident.get("constructor_id") or "",
                "team_name": ident.get("team_name") or "",
                "gap_to_leader": iv.get("gap_to_leader"),
                "interval": iv.get("interval"),
                "lap": laps_by_driver.get(num),
                "grid": grid.get(num, 0),
            })
        entries = [e for e in entries if e["position"] is not None]
        entries.sort(key=lambda e: e["position"])

        flag, flag_msg = self._flag_state(session_key, cutoff)

        return {
            "session_key": session_key,
            "session_name": session.get("session_name") or "Race",
            "country": session.get("country_name") or "",
            "circuit": session.get("circuit_short_name") or "",
            "lap": lap,
            "total_laps": total_laps,
            "flag": flag,
            "flag_message": flag_msg,
            "data_age_s": 0 if self.replay_at else
                          int((datetime.now(timezone.utc) - newest).total_seconds()),
            "entries": entries,
        }

    def _lap_state(self, session_key: int, cutoff: Optional[datetime]):
        """(current lap, scheduled total, per-driver laps).

        OpenF1 publishes no scheduled lap count, so `total_laps` stays None
        unless a caller supplies one. Showing "LAP 32" alone is honest;
        inventing a denominator is not.
        """
        rows = self._incremental("laps", session_key)
        best = self._max_lap or 0
        per: Dict[int, int] = self._laps_per_driver
        for row in rows:
            when = _parse(row.get("date_start", ""))
            if cutoff and when and when > cutoff:
                continue
            n = row.get("lap_number") or 0
            best = max(best, n)
            num = row.get("driver_number")
            if num is not None:
                per[num] = max(per.get(num, 0), n)
        # Lap counts only climb, so folding the new rows into the running maxima
        # lands in the same place as re-reading the whole session.
        self._max_lap = best or self._max_lap
        return self._max_lap, None, dict(per)

    def _flag_state(self, session_key: int, cutoff: Optional[datetime]):
        """Race-wide flag: SC, VSC, RED. Sector yellows are ignored.

        Walks chronologically so a later RED replaces an SC, VSC ENDING
        clears a VSC, and SESSION STARTED after a stoppage does not leave
        an SC chip up for the rest of the afternoon. Monza 2026 messages
        are `SAFETY CAR DEPLOYED`, `RED FLAG - RACE SUSPENDED`,
        `VSC DEPLOYED`, `VSC ENDING` -- not the longer phrases the first
        draft matched.
        """
        rows = self._incremental("race_control", session_key)
        if not rows:
            # No new messages means the track status has not changed.
            return self._flag_state_cache
        events = []
        for row in rows:
            when = _parse(row.get("date", ""))
            if when is None or (cutoff and when > cutoff):
                continue
            events.append((when, row))
        if not events:
            return self._flag_state_cache
        events.sort(key=lambda x: x[0])

        # Start from the last known status and apply only what is new. The walk
        # below is chronological, so replaying just the new messages on top of
        # the carried state lands where replaying all of them would.
        flag, flag_msg = self._flag_state_cache
        for _, row in events:
            msg = (row.get("message") or "")
            up = msg.upper()
            cat = row.get("category") or ""
            raw_flag = (row.get("flag") or "").upper()

            if "RED FLAG" in up or raw_flag == "RED":
                flag, flag_msg = "RED", msg
                continue
            if cat == "SafetyCar" or "SAFETY CAR DEPLOYED" in up:
                if "VSC" in up or "VIRTUAL" in up:
                    if "ENDING" in up:
                        flag, flag_msg = None, None
                    else:
                        flag, flag_msg = "VSC", msg
                elif "DEPLOYED" in up or "IN THIS LAP" in up:
                    flag, flag_msg = "SC", msg
                elif "ENDING" in up:
                    flag, flag_msg = None, None
                continue
            if "SAFETY CAR LIGHTS ON" in up:
                flag, flag_msg = "SC", msg
                continue
            if cat == "SessionStatus" and "SESSION STARTED" in up:
                # Lights-out, or the restart after a red flag. Either way the
                # previous SC/RED chip would otherwise stick until the next
                # SafetyCar message -- at Monza that was 38 minutes of a
                # false SC. Drop it.
                flag, flag_msg = None, None
                continue
            if raw_flag == "CHEQUERED" or "SESSION FINISHED" in up:
                flag, flag_msg = None, None
        self._flag_state_cache = (flag, flag_msg)
        return flag, flag_msg
