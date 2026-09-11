"""Live timing straight from F1's own feed, for the forked f1-live plugin.

Why this exists
---------------
Tried against Spanish GP FP1 on 2026-09-11, the first live session since the
fork shipped. Every source the plugin used, or could have used, turned an
anonymous client away while the session was running:

    OpenF1              /v1/sessions?session_key=latest   401
    F1 static archive   <session>/Index.json              403
                        <session>/TimingData.jsonStream   403
    F1 SignalR classic  /signalr/negotiate                401
    F1 SignalR Core     /signalrcore/negotiate            200   <- this module

OpenF1's 401 said global access, past sessions included, is restricted to
authenticated users until the session ends. So `f1_live.LiveRaceFeed` would
have shown nothing at all during the Grand Prix. Only the static archive's
"what is on now" files stay public (StreamingStatus.json, SessionInfo.json),
and this module uses them to decide when to connect.

The Core hub accepted an anonymous Subscribe and returned the whole FP1 order
(RUS 1:34.077, ANT +0.286, HAM +0.543 ...) followed by 507 TimingData updates
in 90 seconds, 2-7 s behind the timing screens. OpenF1 is itself built on this
feed, so going direct removes the middleman that started charging.

`LiveRaceFeed` stays for replay and fixtures, which OpenF1 still serves
between sessions, and as `live.source: "openf1"` for anyone with a key.

Honest limitations
------------------
* Unofficial and undocumented. F1 has already closed the classic endpoint and
  could close this one without notice. Every failure degrades to "not live",
  and the plugin keeps showing its normal cards.
* 2-7 s behind the timing screens, measured. Not TV-instant.
* A card is a frozen bitmap in the scroll strip, so what a viewer sees is as
  old as the last strip rebuild. That bounds freshness, not this module.

Transport
---------
Long-polling over plain HTTP, through `requests`. SignalR Core also offers
WebSockets and server-sent events, but the Pi's system Python -- which is what
ledmatrix.service runs, as root -- has no websocket library, and about one
request a second is nothing to a CDN. The protocol, as verified against FP1:

    POST /signalrcore/negotiate?negotiateVersion=1     -> connectionToken
    POST /signalrcore?id=TOKEN  {"protocol":"json","version":1}<RS>
    GET  /signalrcore?id=TOKEN  first poll returns empty, the next {}<RS>
    POST /signalrcore?id=TOKEN  {"type":1,"invocationId":"0",
                                 "target":"Subscribe","arguments":[[topics]]}<RS>
    GET  ...                    type 3: the full state, one key per topic
    GET  ...                    type 1: arguments = [topic, delta, utc]

Deltas deep-merge into the held state. Where the state holds a list, a delta
addresses it by string index: race control's {"Messages": {"54": {...}}}
appends message 54 to the list the initial state delivered.

Between sessions this costs two tiny public GETs a minute. The thread does not
connect unless StreamingStatus.json says a session is streaming and
SessionInfo.json says it is a type the plugin displays.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests

LIVETIMING = "https://livetiming.formula1.com"
HUB = LIVETIMING + "/signalrcore"
STATIC = LIVETIMING + "/static/"
RS = "\x1e"

# What FastF1 and F1's own timing player send. Plain python-requests headers
# are not what this endpoint is used to seeing, so do not stand out.
_HEADERS = {"User-Agent": "BestHTTP", "Accept-Encoding": "gzip, identity"}

TOPICS = ["SessionInfo", "SessionStatus", "TrackStatus", "LapCount",
          "TimingData", "TimingAppData", "DriverList", "RaceControlMessages",
          "ExtrapolatedClock", "Heartbeat"]

_IDLE_CHECK_S = 60       # between StreamingStatus checks when nothing is on
_RECONNECT_S = 15        # after a connection ends, before trying another
_POLL_GAP_S = 1.0        # between long polls; batches a second of updates
_POLL_TIMEOUT_S = 120    # the server holds an idle poll open for ~90 s
_HTTP_TIMEOUT_S = 10

# TrackStatus.Status. Only race-wide states get a chip, matching the OpenF1
# path: "2" (a yellow somewhere) is ignored the way sector yellows are there,
# and "7" (VSC ending) clears the chip the way "VSC ENDING" does there.
_TRACK_FLAG = {"4": "SC", "5": "RED", "6": "VSC"}

# SessionStatus carries two fields: "Status", the moment-to-moment state, and
# "Started", the session's phase. Seen live: FP1 ran as Started/Started and
# ended Finished/Finished. After a red flag in FP2 it read Status "Inactive",
# Started "Started" with 18 minutes still to run -- and the board went dark,
# because only Status was read. So a session is running while either field
# says so, and over when either says it is. "Aborted" is a red-flag
# suspension: still this session, and exactly when the wall wants the board.
_LIVE_STATUSES = {"started", "aborted"}
_DONE_STATUSES = {"finished", "finalised", "ends"}


def _parse_utc(ts: Any) -> Optional[datetime]:
    """F1 stamps carry seven fractional digits and a Z; fromisoformat takes six."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        s = "%s.%s%s" % (head, rest[:i][:6], rest[i:])
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _value(v: Any) -> Any:
    """F1 wraps many figures as {"Value": ...}."""
    return v.get("Value") if isinstance(v, dict) else v


def _seconds(v: Any) -> Optional[float]:
    """'+0.870' -> 0.87 and '+1:02.345' -> 62.345. None for blanks and labels."""
    v = _value(v)
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().lstrip("+")
    if not s:
        return None
    try:
        if ":" in s:
            minutes, secs = s.split(":", 1)
            return int(minutes) * 60 + float(secs)
        return float(s)
    except ValueError:
        return None


def _race_gap(raw: Any, position: Optional[int]) -> Any:
    """GapToLeader, reshaped for f1_live.format_gap().

    The leader's own GapToLeader is the lap counter ("LAP 23"), which
    format_gap() would read as lapped, so the leader gets 0. A lapped car reads
    "1L" or "1 L"; that becomes "+1 LAP", which format_gap() labels LAPPED
    rather than printing a figure from a different lap.
    """
    if position == 1:
        return 0.0
    v = _value(raw)
    if isinstance(v, str):
        s = v.strip().upper().replace(" ", "")
        if s.endswith("L") and s[:-1].lstrip("+").isdigit():
            return "+%s LAP" % s[:-1].lstrip("+")
        if "LAP" in s:
            return v
    return _seconds(v)


def _timed_figures(line: Dict[str, Any], part: Optional[int]):
    """(gap to fastest, gap to the car ahead, best lap) for practice and qualifying.

    Practice carries these at the top level -- seen live in FP1. Qualifying is
    documented to keep one set per segment in Stats / BestLapTimes, indexed by
    TimingData.SessionPart. That shape has NOT been seen live here yet, so it
    is read defensively: top-level fields first, then the current segment, then
    the latest segment that has a time.
    """
    gap = _seconds(line.get("TimeDiffToFastest"))
    ahead = _seconds(line.get("TimeDiffToPositionAhead"))
    best = _value(line.get("BestLapTime")) or ""

    def pick(seq, key=None):
        if isinstance(seq, dict):
            seq = [seq[k] for k in sorted(seq, key=lambda k: _int(k) or 0)]
        if not isinstance(seq, list):
            return None
        order = list(range(len(seq) - 1, -1, -1))
        if part and 0 < part <= len(seq):
            order.insert(0, part - 1)
        for i in order:
            item = seq[i]
            if isinstance(item, dict):
                v = item.get(key) if key else _value(item)
                if v not in (None, ""):
                    return v
        return None

    if gap is None and line.get("Stats") is not None:
        gap = _seconds(pick(line["Stats"], "TimeDiffToFastest"))
        ahead = _seconds(pick(line["Stats"], "TimeDiffToPositionAhead"))
    if not best and line.get("BestLapTimes") is not None:
        best = pick(line["BestLapTimes"]) or ""
    return gap, ahead, best


def _merge(dst: Any, src: Any) -> Any:
    """Fold one delta into the held state. See the module docstring."""
    if isinstance(src, dict):
        if isinstance(dst, dict):
            for k, v in src.items():
                if k != "_kf":
                    dst[k] = _merge(dst.get(k), v)
            return dst
        if isinstance(dst, list):
            for k, v in src.items():
                i = _int(k)
                if i is None or i < 0:
                    continue
                if i < len(dst):
                    dst[i] = _merge(dst[i], v)
                else:
                    dst.extend([None] * (i - len(dst)))
                    dst.append(_merge(None, v))
            return dst
        # "_kf" marks a keyframe; it is protocol, not data.
        return {k: _merge(None, v) for k, v in src.items() if k != "_kf"}
    return src


class SignalRLiveFeed:
    """F1's live timing, reduced to the snapshot LiveRaceFeed.state() returns.

    The manager reads `replay_at` / `replay_session_key` off whichever feed it
    holds to pick a poll cadence. This one never replays, so both are None.
    """

    replay_at = None
    replay_session_key = None

    def __init__(self, logger: Optional[logging.Logger] = None,
                 session_types=("Race",), freshness_s: int = 300,
                 http: Optional[requests.Session] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.log = logger or logging.getLogger("f1_signalr")
        self.session_types = {str(t).lower() for t in (session_types or ("Race",))}
        self.freshness_s = freshness_s
        self.http = http or requests.Session()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}
        self._last_msg: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._done_path: Optional[str] = None
        # True once this connection has delivered its own full state. Until
        # then, whatever is held is left over from an earlier session.
        self._have_initial = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error_log = 0.0

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="f1-live-timing",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish. It exits at its next wait or poll."""
        self._stop.set()

    # ── feeding: the thread uses these, and tests replay a recording ────
    def feed_message(self, msg: Dict[str, Any],
                     when: Optional[datetime] = None) -> None:
        """Apply one hub message: type 3 carries the full state, type 1 a delta."""
        kind = msg.get("type")
        if kind == 3:
            result = msg.get("result")
            if isinstance(result, dict):
                beat = _parse_utc((result.get("Heartbeat") or {}).get("Utc"))
                self._load_initial(result, when or beat or self._now())
        elif kind == 1:
            args = msg.get("arguments") or []
            if len(args) >= 2 and isinstance(args[0], str):
                stamp = _parse_utc(args[2]) if len(args) > 2 else None
                self._apply(args[0], args[1], when or stamp or self._now())

    def _load_initial(self, result: Dict[str, Any], when: datetime) -> None:
        with self._lock:
            self._state = {k: _merge(None, v) for k, v in result.items()}
            self._have_initial = True
            self._last_msg = when
            # Joined after the flag: the finish was not seen, so do not dress
            # the final order up as live for another five minutes.
            self._finished_at = (when - timedelta(seconds=self.freshness_s + 1)
                                 if self._done_locked() else None)

    def _apply(self, topic: str, delta: Any, when: datetime) -> None:
        with self._lock:
            if topic == "SessionInfo" and isinstance(delta, dict):
                new_path = delta.get("Path")
                old_path = (self._state.get("SessionInfo") or {}).get("Path")
                if new_path and old_path and new_path != old_path:
                    # A new session on the same connection. Nothing held
                    # about the old one is true of this one.
                    self._state = {}
                    self._finished_at = None
            self._state[topic] = _merge(self._state.get(topic), delta)
            if self._last_msg is None or when > self._last_msg:
                self._last_msg = when
            if topic == "SessionStatus":
                if self._done_locked():
                    if self._finished_at is None:
                        self._finished_at = when
                else:
                    self._finished_at = None

    def _status_locked(self) -> str:
        return str((self._state.get("SessionStatus") or {}).get("Status") or "").lower()

    def _done_locked(self) -> bool:
        """Is the session over? Either SessionStatus field can say so."""
        ss = self._state.get("SessionStatus") or {}
        return any(str(ss.get(k) or "").lower() in _DONE_STATUSES
                   for k in ("Status", "Started"))

    def _running_locked(self) -> bool:
        """Is the session under way, suspensions included? See _LIVE_STATUSES."""
        if self._done_locked():
            return False
        ss = self._state.get("SessionStatus") or {}
        return (str(ss.get("Status") or "").lower() in _LIVE_STATUSES
                or str(ss.get("Started") or "").lower() == "started")

    # ── the snapshot ────────────────────────────────────────────────────
    def state(self) -> Optional[Dict[str, Any]]:
        """Current snapshot, or None when nothing the plugin displays is live.

        None is the normal answer most of the week and is not an error.
        """
        now = self._now()
        with self._lock:
            info = self._state.get("SessionInfo") or {}
            stype = str(info.get("Type") or "")
            if not info or stype.lower() not in self.session_types:
                return None
            if (self._last_msg is None
                    or (now - self._last_msg).total_seconds() > self.freshness_s):
                return None     # the connection has gone quiet
            if self._running_locked():
                pass
            elif (self._done_locked() and self._finished_at is not None
                  and (now - self._finished_at).total_seconds() <= self.freshness_s):
                pass            # the flag has fallen; hold the final order briefly
            else:
                return None
            entries = self._entries_locked(timed=stype.lower() != "race")
            if not entries:
                return None
            laps = self._state.get("LapCount") or {}
            track = self._state.get("TrackStatus") or {}
            meeting = info.get("Meeting") or {}
            flag = _TRACK_FLAG.get(str(track.get("Status") or ""))
            return {
                "source": "f1",
                "session_key": info.get("Key") or info.get("Path"),
                "session_name": info.get("Name") or "",
                "session_type": stype,
                "meeting_name": meeting.get("Name") or "",
                "country": (meeting.get("Country") or {}).get("Name") or "",
                "circuit": (meeting.get("Circuit") or {}).get("ShortName") or "",
                "lap": _int(laps.get("CurrentLap")),
                "total_laps": _int(laps.get("TotalLaps")),
                "flag": flag,
                "flag_message": track.get("Message") if flag else None,
                "finished": self._done_locked(),
                "data_age_s": max(0, int((now - self._last_msg).total_seconds())),
                "entries": entries,
            }

    def _entries_locked(self, timed: bool) -> List[Dict[str, Any]]:
        timing = self._state.get("TimingData") or {}
        lines = timing.get("Lines") or {}
        drivers = self._state.get("DriverList") or {}
        app = (self._state.get("TimingAppData") or {}).get("Lines") or {}
        part = _int(timing.get("SessionPart"))
        out = []
        for num, line in (lines.items() if isinstance(lines, dict) else ()):
            if not isinstance(line, dict):
                continue
            pos = _int(line.get("Position"))
            if pos is None:
                continue
            ident = drivers.get(num) if isinstance(drivers.get(num), dict) else {}
            stint = app.get(num) if isinstance(app, dict) and isinstance(app.get(num), dict) else {}
            if timed:
                gap, interval, best = _timed_figures(line, part)
            else:
                gap = _race_gap(line.get("GapToLeader"), pos)
                interval = _seconds(line.get("IntervalToPositionAhead"))
                best = _value(line.get("BestLapTime")) or ""
            team = ident.get("TeamName") or ""
            out.append({
                "position": pos,
                "driver_number": _int(num),
                "code": ident.get("Tla") or str(num),
                "last_name": ident.get("LastName") or "",
                "first_name": ident.get("FirstName") or "",
                # normalize_constructor_id() already maps these ("Red Bull
                # Racing", "Haas F1 Team", "Audi" -> sauber). OpenF1 copies its
                # team names from this feed, and all eleven 2026 names were
                # checked against team_colors on 2026-09-11.
                "constructor_id": team,
                "team_name": team,
                "gap_to_leader": gap,
                "interval": interval,
                "lap": _int(line.get("NumberOfLaps")),
                "grid": _int(stint.get("GridPos")) or 0,
                "best_lap": best,
                # The feed says so outright, where OpenF1 made us infer it
                # from a lap deficit.
                "retired": bool(line.get("Retired")),
                "stopped": bool(line.get("Stopped")),
                "in_pit": bool(line.get("InPit")),
            })
        out.sort(key=lambda e: e["position"])
        return out

    # ── the thread ──────────────────────────────────────────────────────
    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                info = self._wanted_session()
                if info is None:
                    self._stop.wait(_IDLE_CHECK_S)
                    continue
                self.log.info("F1 live timing: connecting for %s, %s",
                              info.get("Name"), (info.get("Meeting") or {}).get("Name"))
                self._session_loop()
                backoff = 5
                # Never straight back in. A connection that ends at once --
                # a 204, or the bug described in _begin_connection() -- must
                # not become a reconnect loop against F1's servers.
                self._stop.wait(_RECONNECT_S)
            except Exception as exc:  # noqa: BLE001 - any failure means "not live"
                self._log_error("F1 live timing unavailable: %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def _wanted_session(self) -> Optional[Dict[str, Any]]:
        """SessionInfo for a streaming session we display, else None."""
        status = self._static("StreamingStatus.json") or {}
        if status.get("Status") != "Available":
            return None
        info = self._static("SessionInfo.json")
        if not info or str(info.get("Type") or "").lower() not in self.session_types:
            return None
        if info.get("Path") and info.get("Path") == self._done_path:
            return None     # still streaming, but we already saw it finish
        return info

    def _static(self, name: str) -> Optional[Dict[str, Any]]:
        r = self.http.get(STATIC + name, headers=_HEADERS, timeout=_HTTP_TIMEOUT_S)
        r.raise_for_status()
        return json.loads(r.content.decode("utf-8-sig"))

    def _send(self, url: str, obj: Dict[str, Any]) -> None:
        headers = dict(_HEADERS)
        headers["Content-Type"] = "text/plain;charset=UTF-8"
        r = self.http.post(url, headers=headers, timeout=_HTTP_TIMEOUT_S,
                           data=(json.dumps(obj) + RS).encode("utf-8"))
        r.raise_for_status()

    def _begin_connection(self) -> None:
        """Mark the held state as not yet this connection's.

        2026-09-11, FP2: FP1's finished state was still held when FP2's stream
        came up 15 minutes before the session. The first poll of a new
        connection returns before any data, _finished_for_good() read FP1's
        two-hour-old finish, and the thread disconnected and reconnected every
        0.6 s -- about five requests a second -- until the service restarted.
        The state itself is kept, so a reconnect mid-race does not blank the
        board; it just cannot end a connection until the new one reports.
        """
        with self._lock:
            self._have_initial = False

    def _session_loop(self) -> None:
        self._begin_connection()
        r = self.http.post(HUB + "/negotiate?negotiateVersion=1", headers=_HEADERS,
                           data=b"", timeout=_HTTP_TIMEOUT_S)
        r.raise_for_status()
        url = HUB + "?id=" + quote(r.json()["connectionToken"], safe="")
        self._send(url, {"protocol": "json", "version": 1})
        subscribed = False
        while not self._stop.is_set():
            r = self.http.get(url, headers=_HEADERS, timeout=_POLL_TIMEOUT_S)
            if r.status_code == 204:
                return                              # the server closed it
            r.raise_for_status()
            # Not r.text: with no charset in the reply, requests decodes as
            # Latin-1 and every accented surname on the grid comes out mangled.
            for raw in r.content.decode("utf-8", "replace").split(RS):
                if not raw.strip():
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind is None:                    # the handshake reply
                    if msg.get("error"):
                        raise RuntimeError("handshake refused: %s" % msg["error"])
                    if not subscribed:
                        self._send(url, {"type": 1, "invocationId": "0",
                                         "target": "Subscribe", "arguments": [TOPICS]})
                        subscribed = True
                        self.log.info("F1 live timing: subscribed")
                    continue
                if kind == 7:
                    raise RuntimeError("server closed: %s" % (msg.get("error") or "no reason"))
                if kind == 3 and msg.get("error"):
                    raise RuntimeError("subscribe refused: %s" % msg["error"])
                if kind in (1, 3):
                    self.feed_message(msg)
            if self._finished_for_good():
                with self._lock:
                    self._done_path = (self._state.get("SessionInfo") or {}).get("Path")
                self.log.info("F1 live timing: session over, disconnecting")
                return
            self._stop.wait(_POLL_GAP_S)

    def _finished_for_good(self) -> bool:
        """The flag fell long enough ago that the board will not change again."""
        with self._lock:
            if not self._have_initial:
                return False    # nothing from this connection yet
            if self._finished_at is None or not self._done_locked():
                return False
            return (self._now() - self._finished_at).total_seconds() > self.freshness_s + 60

    def _log_error(self, fmt: str, *args) -> None:
        """At most once a minute. A flapping endpoint would otherwise bury the
        journal, which is how the BDF font fallback stayed hidden for a day."""
        now = time.time()
        if now - self._last_error_log > 60:
            self._last_error_log = now
            self.log.warning(fmt, *args)
