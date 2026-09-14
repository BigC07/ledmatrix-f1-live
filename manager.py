"""
F1 Live

Fork of ChuckBuilds f1-scoreboard. Adds live race timing from OpenF1.
Displays driver standings, constructor standings, race results, qualifying,
practice, sprint results, upcoming races, and race calendar.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from PIL import Image

from src.plugin_system.base_plugin import BasePlugin, VegasDisplayMode

from f1_data import F1DataSource
from f1_live import (LiveRaceFeed, fastest_lap_code, live_header_title,
                     live_row_from_entry)
from f1_signalr import SignalRLiveFeed
from f1_renderer import F1Renderer
from logo_downloader import F1LogoLoader
from scroll_display import ScrollDisplayManager
from team_colors import normalize_constructor_id

from f1_timezone import resolve_timezone_name

logger = logging.getLogger(__name__)


class F1ScoreboardPlugin(BasePlugin):
    """
    F1 Live. Class name stays F1ScoreboardPlugin so the manifest
    entry_point still matches; the display name is in manifest.json.

    Displays F1 standings, race results, qualifying breakdowns, practice
    standings, sprint results, upcoming races, live race timing, and race
    calendar. Supports favorite driver/team highlighting and Vegas scroll.
    """

    def __init__(self, plugin_id, config, display_manager,
                 cache_manager, plugin_manager):
        super().__init__(plugin_id, config, display_manager,
                        cache_manager, plugin_manager)

        # Display dimensions
        if hasattr(display_manager, "matrix") and display_manager.matrix:
            self.display_width = display_manager.matrix.width
            self.display_height = display_manager.matrix.height
        else:
            self.display_width = getattr(display_manager, "width", 128)
            self.display_height = getattr(display_manager, "height", 32)

        # Favorites
        self.favorite_driver = config.get("favorite_driver", "").upper()
        self.favorite_team = normalize_constructor_id(
            config.get("favorite_team", ""))

        # Display duration
        self.display_duration = config.get("display_duration", 30)

        # Scroll card width: use a fixed card width for scroll mode so cards are
        # properly sized regardless of the full chain width (multi-panel setups)
        scroll_cfg = config.get("scroll", {}) if isinstance(config.get("scroll"), dict) else {}
        self._card_width = scroll_cfg.get("game_card_width", 128)

        # Resolve timezone: plugin config → global config → UTC.
        # Re-resolved on every config change so global timezone updates take
        # effect immediately. Kept in a shallow copy (never written back into
        # `config`) so the resolved value never gets persisted as a stale
        # plugin-level override that would shadow future global changes.
        self.timezone = self._resolve_timezone(config, cache_manager, plugin_manager)
        render_config = {**config, "timezone": self.timezone}

        # Initialize components
        self.logo_loader = F1LogoLoader()
        self.data_source = F1DataSource(cache_manager, render_config)
        # Full-width renderer for static single-card display
        self.renderer = F1Renderer(
            self.display_width, self.display_height,
            render_config, self.logo_loader, self.logger)
        # Card-width renderer for scroll/Vegas mode
        self._scroll_renderer = F1Renderer(
            self._card_width, self.display_height,
            render_config, self.logo_loader, self.logger)
        self._scroll_manager = ScrollDisplayManager(
            display_manager, config, self.logger,
            global_config=getattr(self, 'global_config', {}) or {})
        self.enable_scrolling = self._scroll_manager is not None

        # Data state
        self._driver_standings: List[Dict] = []
        self._constructor_standings: List[Dict] = []
        # Pre-filter P1/P2 used for battle cards (unaffected by top_n setting)
        self._driver_battle_p1: Optional[Dict] = None
        self._driver_battle_p2: Optional[Dict] = None
        self._constructor_battle_p1: Optional[Dict] = None
        self._constructor_battle_p2: Optional[Dict] = None
        self._recent_races: List[Dict] = []
        self._upcoming_race: Optional[Dict] = None
        self._qualifying: Optional[Dict] = None
        self._practice_results: Dict[str, Dict] = {}  # FP1/FP2/FP3
        self._sprint: Optional[Dict] = None
        self._calendar: List[Dict] = []
        # Fingerprint of the data the scroll images were last built from, so a
        # refresh that returned identical data does not re-render them.
        self._scroll_content_sig: Optional[str] = None
        self._pole_positions: Dict[str, int] = {}

        # Cards for the most recent race only. The marquee's "last_race"
        # section shows just that race, while the recent_races scroll mode
        # shows several, so the two need separate lists.
        self._vegas_last_race_cards: List[Image.Image] = []
        # f1-live: live_race -- pre-rendered on the update tick, never on
        # the render path. HTTP in get_vegas_content has frozen the panel.
        self._vegas_live_race_cards: List[Image.Image] = []
        self._live_snapshot: Optional[Dict] = None
        self._live_cards_sig: Optional[str] = None
        # f1-live: Vegas alert -- (alert_id, card, created), offered when a red
        # flag, SC or VSC comes out; see get_vegas_alert().
        self._vegas_alert: Optional[tuple] = None
        self._alert_flag: Optional[str] = None
        # f1-live: last_session -- the final order of the last finished practice
        # or qualifying, from F1's feed, kept until the next session goes live.
        # Persisted, so a restart between sessions does not lose it.
        self._last_session: Optional[Dict] = self._restore_last_session()
        self._last_session_sig: Optional[str] = None
        self._last_session_dropped = None
        self._vegas_last_session_cards: List[Image.Image] = []
        # f1-live: after a race (asked for on 2026-09-13) -- a chequered winner
        # card offered as an alert a few times, then the top three at the front
        # of the F1 block until Jolpica publishes the race. Persisted, so a
        # restart neither loses nor repeats it; see _poll_podium().
        self._podium: Optional[Dict] = self._restore_podium()
        self._podium_cards: List[Image.Image] = []
        self._podium_sig: Optional[str] = None
        # f1-live: the season schedule, for hiding results from an earlier
        # Grand Prix once a newer weekend has started.
        self._schedule_events: Optional[List[Dict]] = None

        # Live session state
        self._is_live: bool = False
        self._live_session: str = ""
        self._is_race_weekend: bool = False

        # Timing
        self._last_update = 0
        self._last_live_check = 0
        self._live_check_interval = 120  # check live status every 2 min
        self._update_interval = config.get("update_interval", 3600)
        self._base_update_interval = self._update_interval
        self._last_live_feed = 0.0
        self._live_feed = self._make_live_feed(config)
        self._live_feed_interval = self._live_poll_interval(config)

        # Display state tracking (for dynamic duration)
        self._current_display_mode: Optional[str] = None

        # Build enabled modes
        self.modes = self._build_enabled_modes()

        # Preload logos
        self.logo_loader.preload_all_teams(
            self.renderer.logo_max,
            self.renderer.logo_max)

        self.logger.info("F1 Live initialized with %d modes: %s",
                        len(self.modes), ", ".join(self.modes))

    def _live_poll_interval(self, config: Optional[Dict] = None) -> int:
        cfg = (config or self.config or {}).get("live") or {}
        try:
            return max(10, min(120, int(cfg.get("poll_interval") or 20)))
        except (TypeError, ValueError):
            return 20

    def _make_live_feed(self, config: Optional[Dict] = None):
        """f1-live: live_race -- F1's own timing feed; OpenF1 for replay.

        OpenF1 turned every unauthenticated client away during live sessions
        (found in Spanish GP FP1, 2026-09-11), so live timing now comes from
        f1_signalr. Replay and fixtures are OpenF1-shaped and stay on the old
        feed, as does `live.source: "openf1"` for anyone with a key.
        """
        cfg = (config or self.config or {}).get("live") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        replay_key = cfg.get("replay_session_key") or None
        replay_at = cfg.get("replay_at") or None
        fixture_dir = cfg.get("fixture_dir") or None
        if fixture_dir and not replay_key:
            # Fixtures freeze the board on a finished race. Only honour them
            # when replay is explicitly pinned.
            fixture_dir = None
        try:
            replay_key = int(replay_key) if replay_key else None
        except (TypeError, ValueError):
            replay_key = None
        source = str(cfg.get("source") or "f1").lower()
        if replay_key or source == "openf1":
            return LiveRaceFeed(
                logger=self.logger,
                replay_session_key=replay_key,
                replay_at=replay_at or None,
                fixture_dir=fixture_dir or None,
            )
        types = cfg.get("session_types") or ["Race"]
        if not isinstance(types, (list, tuple)):
            types = ["Race"]
        # f1-live: the race's final order too, for the podium (_poll_podium);
        # _poll_last_session() takes only practice and qualifying from it.
        feed = SignalRLiveFeed(logger=self.logger, session_types=types,
                               result_types=self._result_session_types(cfg) + ["Race"])
        feed.start()
        return feed

    def _stop_live_feed(self) -> None:
        """The F1 feed owns a thread; replacing it without this leaks one
        per config save, each polling F1 for the rest of the weekend."""
        stop = getattr(getattr(self, "_live_feed", None), "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    # ─── Last session result (F1 feed) ─────────────────────────────────
    # f1-live: last_session. Asked for on 2026-09-11 after FP2: keep the final
    # order of the last finished practice or qualifying on the ticker, drawn
    # like the qualifying cards, until the next session goes live. From F1's
    # feed, not OpenF1, which locks everyone out while any session runs.
    _LAST_SESSION_KEY = "f1_live_last_session"
    # FP2 to FP3 is about 18 h and qualifying to the race about 22 h, so this
    # only bites when the next session's result was never captured.
    _LAST_SESSION_TTL = 36 * 3600

    @staticmethod
    def _result_session_types(live_cfg: Optional[Dict]) -> List[str]:
        raw = (live_cfg or {}).get("result_sessions", ["Practice", "Qualifying"])
        if not isinstance(raw, (list, tuple)):
            return ["Practice", "Qualifying"]
        return [str(t) for t in raw if str(t).lower() in ("practice", "qualifying")]

    def _restore_last_session(self) -> Optional[Dict]:
        cm = getattr(self, "cache_manager", None)
        try:
            data = cm.get(self._LAST_SESSION_KEY, max_age=self._LAST_SESSION_TTL) if cm else None
        except Exception:
            return None
        return data if isinstance(data, dict) and data.get("entries") else None

    @staticmethod
    def _last_session_signature(snap: Optional[Dict]) -> str:
        if not snap:
            return "none"
        payload = [snap.get("session_key"), snap.get("session_name"),
                   [(e.get("position"), e.get("code"), e.get("best_lap"),
                     e.get("gap_to_leader")) for e in (snap.get("entries") or [])]]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _set_last_session(self, snap: Optional[Dict]) -> None:
        """Store or drop the result, touching the cache only when it changed."""
        if self._last_session_signature(snap) == self._last_session_signature(self._last_session):
            return
        if snap:
            snap = dict(snap, captured_at=time.time())
        self._last_session = snap
        cm = getattr(self, "cache_manager", None)
        try:
            if snap:
                cm.set(self._LAST_SESSION_KEY, snap, ttl=self._LAST_SESSION_TTL)
            else:
                cm.delete(self._LAST_SESSION_KEY)
        except Exception as e:
            self.logger.debug("Session result not persisted: %s", e)

    def _poll_last_session(self, live: Optional[Dict]) -> None:
        """Called from _poll_live_race() on the update tick, never on the render path."""
        stored = self._last_session
        wanted = self._result_session_types(self.config.get("live") or {})
        if not wanted:
            self._set_last_session(None)
        elif stored:
            # Another session going live makes the stored one history.
            if live and live.get("session_key") != stored.get("session_key"):
                self.logger.info("%s is live; dropping the %s result",
                                 live.get("session_name"), stored.get("session_name"))
                self._last_session_dropped = stored.get("session_key")
                self._set_last_session(None)
            elif time.time() - (stored.get("captured_at") or 0) > self._LAST_SESSION_TTL:
                self._last_session_dropped = stored.get("session_key")
                self._set_last_session(None)
            # f1-live: a qualifying result goes when the race starts, even one
            # never seen live here (asked for on 2026-09-13).
            elif (str(stored.get("session_type") or "").lower() == "qualifying"
                  and self._race_started_since(stored.get("captured_at") or 0)):
                self.logger.info("The race has started; dropping the %s result",
                                 stored.get("session_name"))
                self._last_session_dropped = stored.get("session_key")
                self._set_last_session(None)
        if wanted:
            final_fn = getattr(self._live_feed, "final_snapshot", None)
            final = final_fn() if callable(final_fn) else None
            # The feed holds a finished session until a new one replaces it, so
            # one already dropped must not come straight back. It keeps a race's
            # final order too (for _poll_podium): only the wanted practice and
            # qualifying types are a session result.
            if (final and final.get("entries")
                    and str(final.get("session_type") or "").lower()
                    in {t.lower() for t in wanted}
                    and final.get("session_key") != self._last_session_dropped):
                self._set_last_session(final)
        sig = self._last_session_signature(self._last_session)
        if sig != self._last_session_sig:
            self._last_session_sig = sig
            self._vegas_last_session_cards = (
                self._build_session_result_cards(self._last_session)
                if self._last_session else [])
            if self._last_session:
                self.logger.info(
                    "Session result cards: %s %s n=%d",
                    self._last_session.get("session_name"),
                    ",".join(e.get("code", "?") for e in self._last_session["entries"][:8]),
                    len(self._vegas_last_session_cards))

    def _build_session_result_cards(self, snap: Dict) -> List[Image.Image]:
        # f1-live: last_session. Header plus one row per driver, drawn with the
        # qualifying section card and row because the result was asked to look
        # exactly like the Q3 qualifying results. render_practice_entry is the
        # same _render_driver_row a Q3 row uses -- time left, gap right -- and
        # was byte-identical to the live Q3 cards on the Pi.
        r = self._scroll_renderer
        race = snap.get("meeting_name") or self._live_race_name(snap) or ""
        name = str(snap.get("session_name") or "").strip()
        if name.lower() == "qualifying":
            title = "QUALIFYING - Q3"    # the top ten are the Q3 runners
        else:
            title = (name or "SESSION").upper()
        cards = [r.render_session_result_header(title, race)]
        for e in (snap.get("entries") or [])[:10]:
            gap = e.get("gap_to_leader")
            # A fresh dict with no grid key: show_position_delta is on, and a
            # grid would draw a places-gained figure no Q3 row carries.
            cards.append(r.render_practice_entry({
                "position": e.get("position"),
                "last_name": e.get("last_name") or "",
                "code": e.get("code") or "",
                "constructor_id": e.get("constructor_id") or "",
                "best_lap": e.get("best_lap") or "",
                "gap": ("+%.3f" % gap) if isinstance(gap, (int, float)) and gap > 0 else "",
            }))
        return cards

    # ─── After the race: winner and podium (F1 feed) ───────────────────
    # f1-live, asked for on 2026-09-13 as the Spanish GP ended: "a message pop-up
    # saying the winner", "a checkered flag with the winner's name in it for a
    # few passes", then "a list of the top three ... until it goes back into the
    # results mode". The final order comes from F1's feed: the live snapshot
    # once the flag has fallen (the board holds it a few minutes), or the feed's
    # final snapshot, which a restart still gets while F1 streams the session.
    # Jolpica's race result takes over once it has the race.
    _PODIUM_KEY = "f1_live_podium"
    _PODIUM_TTL = 36 * 3600
    _WINNER_PASSES = 4
    _WINNER_GAP_S = 50      # polls are 20-60 s apart, so about a minute

    def _restore_podium(self) -> Optional[Dict]:
        cm = getattr(self, "cache_manager", None)
        try:
            data = cm.get(self._PODIUM_KEY, max_age=self._PODIUM_TTL) if cm else None
        except Exception:
            return None
        if isinstance(data, dict) and (data.get("entries") or data.get("dropped")):
            return data
        return None

    def _save_podium(self) -> None:
        cm = getattr(self, "cache_manager", None)
        try:
            if self._podium:
                cm.set(self._PODIUM_KEY, self._podium, ttl=self._PODIUM_TTL)
            else:
                cm.delete(self._PODIUM_KEY)
        except Exception as e:
            self.logger.debug("Podium not persisted: %s", e)

    def _drop_podium(self, why: str) -> None:
        """Off the ticker for good. A marker stays in its place, so the race
        the feed still holds is not captured again, restart or not."""
        podium = self._podium or {}
        if podium.get("entries"):
            self.logger.info("Podium for %s dropped: %s",
                             podium.get("meeting_name") or "the race", why)
        self._podium = {"session_key": podium.get("session_key"), "dropped": True,
                        "captured_at": podium.get("captured_at") or time.time()}
        self._podium_cards = []
        self._podium_sig = None
        self._save_podium()

    def _podium_published(self, podium: Dict) -> bool:
        """Jolpica's newest race result is dated on or after the day the podium
        was captured, less a day for a race that ends after midnight UTC."""
        latest = (getattr(self, "_recent_races", None) or [None])[0]
        if not isinstance(latest, dict):
            return False
        try:
            day = datetime.strptime(str(latest.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return False
        captured = datetime.fromtimestamp(podium.get("captured_at") or 0, tz=timezone.utc).date()
        return day >= captured - timedelta(days=1)

    @staticmethod
    def _is_race_result(snap: Optional[Dict]) -> bool:
        """A finished Grand Prix. Not a sprint: the feed types both "Race"."""
        return bool(snap and snap.get("finished") and snap.get("entries")
                    and str(snap.get("session_type") or "").lower() == "race"
                    and "sprint" not in str(snap.get("session_name") or "").lower())

    @staticmethod
    def _podium_rows(entries) -> List:
        return [(e.get("position"), e.get("code"), e.get("gap_to_leader"))
                for e in entries or []]

    def _winner_card(self, podium: Dict) -> Image.Image:
        top = podium["entries"][0]
        return self._scroll_renderer.render_winner_card(
            top.get("last_name") or top.get("code") or "",
            top.get("constructor_id") or "", podium.get("meeting_name") or "")

    def _poll_podium(self, live: Optional[Dict]) -> None:
        """On the update tick, after _poll_last_session(): capture a finished
        race, offer the winner card a few times about a minute apart, and keep
        the top three until Jolpica has the race."""
        if not getattr(self, "_last_update", 0):
            return      # Jolpica not read yet: no telling whether it has the race
        podium = self._podium
        final = live if self._is_race_result(live) else None
        if final is None:
            final_fn = getattr(self._live_feed, "final_snapshot", None)
            final = final_fn() if callable(final_fn) else None
            if not self._is_race_result(final):
                final = None
        if final:
            top = [dict(e) for e in final["entries"][:3]]
            if not podium or podium.get("session_key") != final.get("session_key"):
                podium = self._podium = {
                    "session_key": final.get("session_key"),
                    "meeting_name": final.get("meeting_name") or self._live_race_name(final) or "",
                    "entries": top,
                    "lap": final.get("lap"),
                    "captured_at": time.time(),
                    "winner_left": self._WINNER_PASSES,
                    "winner_next": 0.0,
                }
                self._save_podium()
                self.logger.info("Race over: %s wins; podium %s", top[0].get("code"),
                                 ",".join(e.get("code") or "?" for e in top))
            elif (not podium.get("dropped")
                  and self._podium_rows(top) != self._podium_rows(podium.get("entries"))):
                # The field finishes its last lap after the winner does.
                podium["entries"] = top
                self._save_podium()
                self.logger.info("Podium updated: %s",
                                 ",".join(e.get("code") or "?" for e in top))
        if not podium or podium.get("dropped"):
            self._podium_cards = []
            return
        if live and not live.get("finished") and live.get("session_key") != podium.get("session_key"):
            self._drop_podium("%s is live" % (live.get("session_name") or "a session"))
            return
        if self._podium_published(podium):
            self._drop_podium("Jolpica has the result")
            return
        now = time.time()
        if now - (podium.get("captured_at") or 0) > self._PODIUM_TTL:
            self._drop_podium("too old")
            return
        if podium.get("winner_left", 0) > 0 and now >= podium.get("winner_next", 0):
            self._offer_alert("winner:%s:%d" % (podium.get("session_key"), podium["winner_left"]),
                              self._winner_card(podium))
            podium["winner_left"] -= 1
            podium["winner_next"] = now + self._WINNER_GAP_S
            self._save_podium()
        sig = json.dumps([podium.get("session_key"), self._podium_rows(podium["entries"])],
                         default=str)
        if sig != self._podium_sig:
            self._podium_sig = sig
            self._podium_cards = self._build_podium_cards(podium)
            self.logger.info("Podium cards: %s n=%d",
                             ",".join(e.get("code") or "?" for e in podium["entries"]),
                             len(self._podium_cards))

    def _build_podium_cards(self, podium: Dict) -> List[Image.Image]:
        """The PODIUM header and a race row each for the top three: WINNER on
        the first, the gap to the winner on the others."""
        r = self._scroll_renderer
        cards = [r.render_session_result_header("PODIUM", podium.get("meeting_name") or "")]
        for e in podium.get("entries") or []:
            row = live_row_from_entry(e, podium.get("lap"), race_gap="leader")
            if e.get("position") == 1:
                row["time"] = "WINNER"
            cards.append(r.render_race_row(row))
        return cards

    def _resolve_timezone(self, config: Dict, cache_manager, plugin_manager=None) -> str:
        """Resolve timezone: plugin config → global config → system zone → UTC.

        Consulting only ``cache_manager.config_manager`` used to fall through to
        UTC on cores that expose ``config_manager`` via the plugin manager
        instead, rendering every session start time in UTC.
        """
        return resolve_timezone_name(
            config=config,
            plugin_manager=plugin_manager if plugin_manager is not None
            else getattr(self, "plugin_manager", None),
            cache_manager=cache_manager,
            log=self.logger,
        )

    def _build_enabled_modes(self) -> List[str]:
        """Build list of enabled display modes from config."""
        modes = []
        mode_configs = {
            "f1_live_driver_standings": self.config.get(
                "driver_standings", {}).get("enabled", True),
            "f1_live_constructor_standings": self.config.get(
                "constructor_standings", {}).get("enabled", True),
            "f1_live_recent_races": self.config.get(
                "recent_races", {}).get("enabled", True),
            "f1_live_upcoming": self.config.get(
                "upcoming", {}).get("enabled", True),
            "f1_live_qualifying": self.config.get(
                "qualifying", {}).get("enabled", True),
            "f1_live_practice": self.config.get(
                "practice", {}).get("enabled", True),
            "f1_live_sprint": self.config.get(
                "sprint", {}).get("enabled", True),
            "f1_live_calendar": self.config.get(
                "calendar", {}).get("enabled", True),
        }

        for mode, enabled in mode_configs.items():
            if enabled:
                modes.append(mode)

        return modes

    # ─── Update ────────────────────────────────────────────────────────

    def update(self):
        """Fetch and update all F1 data from APIs."""
        now = time.time()

        # Check live session status more frequently than full data update
        if now - self._last_live_check >= self._live_check_interval:
            self._last_live_check = now
            try:
                self._is_live, self._live_session = (
                    self.data_source.detect_live_session())
                self._is_race_weekend = (
                    self.data_source.get_is_race_weekend())

                # Dynamic update interval: faster during race weekends
                if self._is_live:
                    self._update_interval = 300   # 5 min when live
                elif self._is_race_weekend:
                    self._update_interval = 600   # 10 min during weekend
                else:
                    self._update_interval = self._base_update_interval

                if self._is_live:
                    self.logger.info(
                        "LIVE session detected: %s", self._live_session)
            except Exception as e:
                self.logger.warning("Live check error: %s", e, exc_info=True)

        # f1-live: a hand-made alert, to prove the Vegas alert path end to
        # end outside a live session (see get_vegas_alert).
        self._maybe_test_alert()

        # f1-live: live_race -- poll on the update worker, never on the
        # render path. HTTP in get_vegas_content has frozen the panel.
        interval = self._live_feed_interval
        if (self._live_snapshot is None
                and not self._live_feed.replay_at
                and not self._live_feed.replay_session_key):
            interval = max(interval, 60)
        if now - self._last_live_feed >= interval:
            self._last_live_feed = now
            try:
                self._poll_live_race()
            except Exception as e:
                self.logger.warning("Live race feed error: %s", e, exc_info=True)

        if now - self._last_update < self._update_interval:
            return

        self.logger.info("Updating F1 data (live=%s, weekend=%s)...",
                        self._is_live, self._is_race_weekend)
        self._last_update = now

        for step in (self._update_standings,
                     self._update_recent_races,
                     self._update_upcoming,
                     self._update_qualifying,
                     self._update_practice,
                     self._update_sprint,
                     self._update_calendar,
                     self._prepare_scroll_content):
            try:
                step()
            except Exception as e:
                self.logger.error("Error in %s: %s", step.__name__,
                                 e, exc_info=True)

    def _update_standings(self):
        """Update driver and constructor standings."""
        # Driver standings
        if "f1_live_driver_standings" in self.modes:
            standings = self.data_source.fetch_driver_standings()
            if standings:
                # Calculate poles
                self._pole_positions = (
                    self.data_source.calculate_pole_positions())

                # Shallow copy entries before adding poles/gaps to avoid
                # mutating the cached standings dicts
                standings = [dict(e) for e in standings]
                for entry in standings:
                    code = entry.get("code", "")
                    entry["poles"] = self._pole_positions.get(code, 0)

                # Annotate with championship gap data
                standings = self.data_source.get_championship_gaps(standings)

                # Save pre-filter P1/P2 for battle card (not affected by top_n)
                if len(standings) >= 1:
                    self._driver_battle_p1 = standings[0]
                if len(standings) >= 2:
                    self._driver_battle_p2 = standings[1]

                # Apply favorite filter
                top_n = self.config.get(
                    "driver_standings", {}).get("top_n", 10)
                always_show = self.config.get(
                    "driver_standings", {}).get("always_show_favorite", True)

                self._driver_standings = self.data_source.apply_favorite_filter(
                    standings, top_n,
                    favorite_driver=self.favorite_driver,
                    favorite_team=self.favorite_team,
                    always_show_favorite=always_show)

        # Constructor standings
        if "f1_live_constructor_standings" in self.modes:
            standings = self.data_source.fetch_constructor_standings()
            if standings:
                # Annotate with championship gap data
                standings = self.data_source.get_championship_gaps(standings)

                # Save pre-filter P1/P2 for battle card (not affected by top_n)
                if len(standings) >= 1:
                    self._constructor_battle_p1 = standings[0]
                if len(standings) >= 2:
                    self._constructor_battle_p2 = standings[1]

                top_n = self.config.get(
                    "constructor_standings", {}).get("top_n", 10)
                always_show = self.config.get(
                    "constructor_standings", {}).get(
                        "always_show_favorite", True)

                self._constructor_standings = (
                    self.data_source.apply_favorite_filter(
                        standings, top_n,
                        favorite_team=self.favorite_team,
                        always_show_favorite=always_show,
                        driver_key="constructor_id",
                        team_key="constructor_id"))

    def _update_recent_races(self):
        """Update recent race results."""
        if "f1_live_recent_races" not in self.modes:
            return

        count = self.config.get("recent_races", {}).get("number_of_races", 3)
        races = self.data_source.fetch_recent_races(count=count)
        if races:
            top_finishers = self.config.get(
                "recent_races", {}).get("top_finishers", 3)
            always_show = self.config.get(
                "recent_races", {}).get("always_show_favorite", True)

            # Shallow copy race dicts before mutating results to avoid
            # altering the cached objects from fetch_recent_races
            filtered_races = []
            for race in races:
                race_copy = dict(race)
                results = race.get("results", [])
                # Preserve full results for the points haul card
                race_copy["all_results"] = results
                race_copy["results"] = self.data_source.apply_favorite_filter(
                    results, top_finishers,
                    favorite_driver=self.favorite_driver,
                    always_show_favorite=always_show)
                filtered_races.append(race_copy)

            self._recent_races = filtered_races

    def _update_upcoming(self):
        """Update upcoming race data."""
        if "f1_live_upcoming" not in self.modes:
            return

        upcoming = self.data_source.get_upcoming_race()
        if upcoming:
            self._upcoming_race = upcoming
        # f1-live: the whole schedule, for _stale_sections(). The data
        # source caches it and get_upcoming_race() has just read it, so
        # this costs no request; a failure keeps the last good copy.
        try:
            events = self.data_source.fetch_schedule()
        except Exception:
            events = None
        if events:
            self._schedule_events = events

    def _update_qualifying(self):
        """Update qualifying results."""
        if "f1_live_qualifying" not in self.modes:
            return

        qualifying = self.data_source.fetch_qualifying()
        if qualifying:
            self._qualifying = qualifying

    def _update_practice(self):
        """Update free practice results."""
        if "f1_live_practice" not in self.modes:
            return

        sessions = self.config.get(
            "practice", {}).get("sessions_to_show", ["FP1", "FP2", "FP3"])
        top_n = self.config.get("practice", {}).get("top_n", 10)

        session_name_map = {
            "FP1": "Practice 1",
            "FP2": "Practice 2",
            "FP3": "Practice 3",
        }

        for fp_key in sessions:
            session_name = session_name_map.get(fp_key)
            if not session_name:
                continue

            result = self.data_source.fetch_practice_results(session_name)
            if result:
                # Shallow copy before slicing to avoid mutating cached dict
                result_copy = dict(result)
                if result_copy.get("results"):
                    result_copy["results"] = result_copy["results"][:top_n]
                self._practice_results[fp_key] = result_copy

    def _update_sprint(self):
        """Update sprint race results."""
        if "f1_live_sprint" not in self.modes:
            return

        sprint = self.data_source.fetch_sprint_results()
        if sprint:
            # Shallow copy before slicing to avoid mutating cached dict
            sprint_copy = dict(sprint)
            top_n = self.config.get("sprint", {}).get("top_finishers", 10)
            if sprint_copy.get("results"):
                sprint_copy["results"] = sprint_copy["results"][:top_n]
            self._sprint = sprint_copy

    def _update_calendar(self):
        """Update race calendar."""
        if "f1_live_calendar" not in self.modes:
            return

        cal_config = self.config.get("calendar", {})
        calendar = self.data_source.get_calendar(
            show_practice=cal_config.get("show_practice", False),
            show_qualifying=cal_config.get("show_qualifying", True),
            show_sprint=cal_config.get("show_sprint", True),
            max_events=cal_config.get("max_events", 5))
        if calendar:
            self._calendar = calendar

    # ─── Gap Trend Helper ──────────────────────────────────────────────

    def _compute_race_gap_trend(self, code1: str, code2: str,
                                 use_constructor: bool = False) -> int:
        """
        Return points delta (code1 - code2) from the most recent race.
        Positive = code1 extended its lead; negative = code2 is closing.
        Returns 0 if data is unavailable.
        """
        if not self._recent_races:
            return 0
        all_res = self._recent_races[0].get("all_results", [])
        if use_constructor:
            pts1 = sum(e.get("points", 0) for e in all_res
                       if e.get("constructor_id", "") == code1)
            pts2 = sum(e.get("points", 0) for e in all_res
                       if e.get("constructor_id", "") == code2)
            if pts1 == 0 and pts2 == 0:
                return 0
        else:
            pts1 = next(
                (e.get("points", 0) for e in all_res
                 if e.get("code", "").upper() == code1.upper()), None)
            pts2 = next(
                (e.get("points", 0) for e in all_res
                 if e.get("code", "").upper() == code2.upper()), None)
            if pts1 is None or pts2 is None:
                return 0
        return int(pts1 - pts2)

    # ─── Scroll Content Preparation ────────────────────────────────────

    def _scroll_content_signature(self) -> str:
        """Fingerprint every input _prepare_scroll_content renders from.

        Rendering all twelve scroll modes is the single most expensive thing
        this plugin does -- measured at 12.46s on a Pi, one image of which was
        11250x64px -- and update() ran it unconditionally on every refresh.
        Outside a race weekend the refreshed data is byte-identical to the last
        one, so nearly all of that work rebuilt images that were already
        correct. The plugin update runs on a worker thread, but the render loop
        shares the interpreter with it, and the marquee visibly stalled.

        Anything a card is drawn from belongs here. A field left out means the
        panel keeps showing stale content, so this errs toward including too
        much: the hash costs microseconds against seconds of rendering.
        """
        r = self._scroll_renderer
        payload = {
            "live": [self._is_live, self._live_session],
            "driver_standings": self._driver_standings,
            "constructor_standings": self._constructor_standings,
            "driver_battle": [self._driver_battle_p1, self._driver_battle_p2],
            "constructor_battle": [self._constructor_battle_p1,
                                   self._constructor_battle_p2],
            "recent_races": self._recent_races,
            "upcoming_race": self._upcoming_race,
            "qualifying": self._qualifying,
            "practice": self._practice_results,
            "sprint": self._sprint,
            "calendar": self._calendar,
            "favorites": [self.favorite_driver, self.favorite_team],
            # Renderer toggles decide which cards exist at all.
            "flags": {name: getattr(r, name, None)
                      for name in sorted(dir(r)) if name.startswith("show_")},
            "recent_races_cfg": self.config.get("recent_races", {}),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _prepare_scroll_content(self, force: bool = False):
        """Pre-render all scroll mode content.

        Skips the work when the underlying data is unchanged since the last
        build. Pass force=True when a mode is known to be unprepared -- the
        signature can match while the images themselves are missing, e.g. on
        the first display after a mode was added.
        """
        signature = self._scroll_content_signature()
        if not force and signature == self._scroll_content_sig:
            self.logger.debug(
                "F1 data unchanged since the last build; keeping the "
                "prepared scroll content")
            return
        self._scroll_content_sig = signature

        r = self._scroll_renderer
        separator = r.render_f1_separator()
        is_live = self._is_live
        live_sess = self._live_session

        # Round / season info (used by headers and battle card)
        season = datetime.now(timezone.utc).year
        round_num = self.data_source.get_latest_round(season)
        total_rounds = len(self._calendar) if self._calendar else 24
        remaining_races = max(0, total_rounds - round_num)

        # Championship leaders intro card (very first in Vegas scroll)
        if r.show_championship_leaders and self._driver_standings and self._constructor_standings:
            drv_leader = self._driver_standings[0] if self._driver_standings else None
            con_leader = self._constructor_standings[0] if self._constructor_standings else None
            if drv_leader and con_leader:
                leaders_card = r.render_championship_leaders(
                    drv_leader, con_leader,
                    is_live=is_live, live_session=live_sess)
                self._scroll_manager.prepare_and_display(
                    "championship_leaders", [leaders_card], separator)

        # Driver championship battle card (P1 vs P2, follows leaders)
        # Uses pre-filter standings so top_n config doesn't affect P1/P2 selection
        if r.show_championship_battle and self._driver_battle_p1 and self._driver_battle_p2:
            p1 = self._driver_battle_p1
            p2 = self._driver_battle_p2
            gap_trend = self._compute_race_gap_trend(
                p1.get("code", ""), p2.get("code", ""))
            battle_card = r.render_championship_battle_card(
                p1, p2, remaining_races=remaining_races,
                gap_trend=gap_trend,
                is_live=is_live, live_session=live_sess)
            self._scroll_manager.prepare_and_display(
                "championship_battle", [battle_card], separator)

        # Constructor championship battle card (P1 vs P2 constructor)
        if r.show_constructor_battle and self._constructor_battle_p1 and self._constructor_battle_p2:
            cp1 = self._constructor_battle_p1
            cp2 = self._constructor_battle_p2
            con_gap_trend = self._compute_race_gap_trend(
                cp1.get("constructor_id", ""), cp2.get("constructor_id", ""),
                use_constructor=True)
            con_battle = r.render_constructor_battle_card(
                cp1, cp2, remaining_races=remaining_races,
                gap_trend=con_gap_trend,
                is_live=is_live, live_session=live_sess)
            self._scroll_manager.prepare_and_display(
                "constructor_battle", [con_battle], separator)

        # Spotlight card for favorite driver (appears first in sequence)
        if self.favorite_driver and self._driver_standings:
            fav_entry = next(
                (e for e in self._driver_standings
                 if e.get("code", "").upper() == self.favorite_driver),
                None)
            if fav_entry:
                spotlight = r.render_favorite_driver_spotlight(
                    fav_entry, is_live=is_live, live_session=live_sess,
                    recent_races=self._recent_races)
                self._scroll_manager.prepare_and_display(
                    "driver_spotlight", [spotlight], separator)

        # Spotlight card for favorite team
        if self.favorite_team and self._constructor_standings:
            fav_team = next(
                (e for e in self._constructor_standings
                 if e.get("constructor_id", "") == self.favorite_team),
                None)
            if fav_team:
                # Also find team drivers in driver standings
                team_drivers = [
                    e for e in self._driver_standings
                    if e.get("constructor_id", "") == self.favorite_team
                ] if self._driver_standings else []
                spotlight = r.render_favorite_team_spotlight(
                    fav_team, driver_entries=team_drivers,
                    is_live=is_live, live_session=live_sess)
                self._scroll_manager.prepare_and_display(
                    "team_spotlight", [spotlight], separator)

        # Build last-race points lookup (driver code → points scored in most recent race)
        last_race_pts_map: Dict[str, int] = {}
        last_race_con_pts_map: Dict[str, int] = {}
        if self._recent_races:
            for res in self._recent_races[0].get("all_results", []):
                code = res.get("code", "").upper()
                cid_res = res.get("constructor_id", "")
                pts = int(res.get("points", 0))
                last_race_pts_map[code] = pts
                last_race_con_pts_map[cid_res] = (
                    last_race_con_pts_map.get(cid_res, 0) + pts)

        # Driver standings
        if self._driver_standings:
            cards = []
            if r.show_standings_header:
                cards.append(r.render_standings_header(
                    "DRIVER STANDINGS", round_num=round_num,
                    total_rounds=total_rounds, season=season))
            # Driver form guide card (recent race positions at a glance)
            if r.show_driver_form and self._recent_races:
                form_card = r.render_driver_form_card(
                    self._driver_standings[:8], self._recent_races)
                cards.append(form_card)
            for e in self._driver_standings:
                enriched = dict(e)
                enriched["last_race_pts"] = last_race_pts_map.get(
                    e.get("code", "").upper(), 0)
                cards.append(r.render_driver_standing(
                    enriched, is_live=is_live, live_session=live_sess))
            self._scroll_manager.prepare_and_display(
                "driver_standings", cards, separator)

        # Constructor standings (enriched with per-driver points)
        if self._constructor_standings:
            cards = []
            if r.show_standings_header:
                cards.append(r.render_standings_header(
                    "CONSTRUCTOR STANDINGS", round_num=round_num,
                    total_rounds=total_rounds, season=season))
            for e in self._constructor_standings:
                cid = e.get("constructor_id", "")
                team_drivers = sorted(
                    [d for d in self._driver_standings
                     if d.get("constructor_id") == cid],
                    key=lambda d: d.get("position", 99))
                entry = dict(e)
                entry["team_drivers"] = team_drivers
                entry["last_race_pts"] = last_race_con_pts_map.get(cid, 0)
                cards.append(r.render_constructor_standing(
                    entry, is_live=is_live, live_session=live_sess))
            self._scroll_manager.prepare_and_display(
                "constructor_standings", cards, separator)

        # Recent races (winners summary + podium cards + favorite highlight + points haul + gap chart)
        rr_cfg = self.config.get("recent_races", {})
        show_winners = rr_cfg.get("show_winners_summary", True)
        self._vegas_last_race_cards = []
        if self._recent_races:
            cards = []
            # Winners summary at the top (only if showing 2+ races)
            if show_winners and len(self._recent_races) > 1:
                cards.append(r.render_recent_winners_card(self._recent_races))
            for index, race in enumerate(self._recent_races):
                race_cards = self._build_race_cards(race)
                # _recent_races is most-recent-first, so index 0 is the race the
                # marquee's "last_race" section shows. Captured here rather than
                # re-rendered later, and rather than sliced back out of `cards`
                # below — the per-race card count varies with config and with
                # whether the favorite finished off the podium.
                if index == 0:
                    self._vegas_last_race_cards = list(race_cards)
                cards.extend(race_cards)
            self._scroll_manager.prepare_and_display(
                "recent_races", cards, separator)

        # Qualifying
        if self._qualifying:
            cards = self._build_qualifying_cards()
            if cards:
                self._scroll_manager.prepare_and_display(
                    "qualifying", cards, separator)

        # Practice
        practice_cards = self._build_practice_cards()
        if practice_cards:
            self._scroll_manager.prepare_and_display(
                "practice", practice_cards, separator)

        # Sprint
        if self._sprint and self._sprint.get("results"):
            cards = [r.render_sprint_header(
                        self._sprint.get("race_name", ""))]
            for entry in self._sprint["results"]:
                cards.append(r.render_sprint_entry(entry))
            self._scroll_manager.prepare_and_display(
                "sprint", cards, separator)

        # Calendar
        if self._calendar:
            cards = [r.render_calendar_entry(e)
                    for e in self._calendar]
            self._scroll_manager.prepare_and_display(
                "calendar", cards, separator)

    def _build_race_cards(self, race: Dict) -> List[Image.Image]:
        """
        Build the cards for a single race: the result, then whichever extras
        are enabled.

        Shared by the recent_races scroll mode and the marquee's "last_race"
        section so a race is presented the same way in both, and so the
        recent_races toggles keep applying in the marquee.

        Args:
            race: One entry from self._recent_races

        Returns:
            Cards for that race, in display order
        """
        r = self._scroll_renderer
        rr_cfg = self.config.get("recent_races", {})
        # Local patch (repo patches/patch_f1_race_rows.py): a name card then one
        # card per finisher, the way the qualifying section already reads.
        # render_race_result()'s three-column podium is left in the renderer,
        # unused here, so reverting is just restoring this file.
        #
        # `results` has already been trimmed to recent_races.top_finishers by
        # apply_favorite_filter() in _update_recent_races(), which also appends
        # the favourite driver when they finish outside that cut -- so they get
        # a row of their own and the separate render_favorite_race_card() is
        # redundant on this path.
        results = race.get("results", [])
        cards = [r.render_race_header(race)]
        for entry in results:
            cards.append(r.render_race_row(entry))

        # Gap chart bar visualization (skip if no result data available)
        if rr_cfg.get("show_gap_chart", True) and race.get("all_results"):
            cards.append(r.render_race_gap_chart(
                race, top_n=rr_cfg.get("gap_chart_drivers", 5)))

        # Points haul bar chart (uses full unfiltered results)
        if rr_cfg.get("show_points_haul", True):
            cards.append(r.render_race_points_haul(
                race, top_n=rr_cfg.get("points_haul_drivers", 5)))

        return cards

    def _build_qualifying_cards(self) -> List[Image.Image]:
        """Build qualifying result cards grouped by Q session."""
        if not self._qualifying:
            return []

        r = self._scroll_renderer
        cards = []
        quali_config = self.config.get("qualifying", {})
        results = self._qualifying.get("results", [])
        race_name = self._qualifying.get("race_name", "")

        # Team H2H card at the start of qualifying section
        if quali_config.get("show_team_duel", True) and results:
            cards.append(r.render_qualifying_team_duel_card(self._qualifying))

        for session_key, show_key, label in [
            ("q3", "show_q3", "Q3"),
            ("q2", "show_q2", "Q2"),
            ("q1", "show_q1", "Q1"),
        ]:
            if not quali_config.get(show_key, True):
                continue

            # Add session header
            cards.append(r.render_qualifying_header(
                label, race_name))

            # Add entries for this session
            for entry in results:
                # Only show entries that have a time for this session
                if entry.get(session_key):
                    cards.append(r.render_qualifying_entry(
                        entry, label))
                elif entry.get("eliminated_in") == label:
                    # Show eliminated driver
                    cards.append(r.render_qualifying_entry(
                        entry, label))

        return cards

    def _build_practice_cards(self) -> List[Image.Image]:
        """Build practice result cards for all configured sessions."""
        r = self._scroll_renderer
        cards = []

        for fp_key in ["FP3", "FP2", "FP1"]:  # Most recent first
            if fp_key not in self._practice_results:
                continue

            fp_data = self._practice_results[fp_key]
            cards.append(r.render_practice_header(
                fp_key, fp_data.get("circuit", "")))

            for entry in fp_data.get("results", []):
                cards.append(r.render_practice_entry(entry))

        return cards

    # ─── Display ───────────────────────────────────────────────────────

    def display(self, force_clear=False, display_mode=None) -> bool:
        """
        Display the current F1 mode.

        Args:
            force_clear: Whether to clear display first
            display_mode: Specific mode to display (from manifest display_modes)

        Returns:
            True if content was displayed, False if mode has no data
        """
        if not self.enabled:
            return False

        if display_mode is None:
            display_mode = self.modes[0] if self.modes else "f1_live_driver_standings"

        self._current_display_mode = display_mode

        if display_mode == "f1_live_upcoming":
            return self._display_upcoming(force_clear)
        elif display_mode in ("f1_live_driver_standings",
                               "f1_live_constructor_standings",
                               "f1_live_recent_races",
                               "f1_live_qualifying",
                               "f1_live_practice",
                               "f1_live_sprint",
                               "f1_live_calendar"):
            return self._display_scroll_mode(display_mode, force_clear)
        else:
            self.logger.warning("Unknown display mode: %s", display_mode)
            return False

    def _enrich_upcoming_with_countdown(self,
                                        race: Dict) -> Dict:
        """Return a shallow copy of race with fresh countdown_seconds set."""
        upcoming = dict(race)
        upcoming["countdown_seconds"] = None

        now = datetime.now(timezone.utc)

        for session in upcoming.get("sessions", []):
            if session.get("status_state") == "pre" and session.get("date"):
                try:
                    parsed_dt = datetime.fromisoformat(
                        session["date"].replace("Z", "+00:00"))
                    if parsed_dt > now:
                        upcoming["countdown_seconds"] = max(
                            0, (parsed_dt - now).total_seconds())
                        upcoming["next_session_type"] = session.get(
                            "type_abbr", "")
                        break
                except (ValueError, TypeError):
                    continue

        return upcoming

    def _display_upcoming(self, force_clear: bool) -> bool:
        """Display the upcoming race card (static)."""
        if not self._upcoming_race:
            return False

        if force_clear:
            self.display_manager.image.paste(
                Image.new("RGB",
                          (self.display_width, self.display_height),
                          (0, 0, 0)),
                (0, 0))

        upcoming = self._enrich_upcoming_with_countdown(self._upcoming_race)
        card = self.renderer.render_upcoming_race(upcoming)
        self.display_manager.image.paste(card, (0, 0))
        self.display_manager.update_display()
        return True

    def _display_scroll_mode(self, display_mode: str,
                              force_clear: bool) -> bool:
        """Display a scrolling mode."""
        mode_key = self._MODE_KEY_MAP.get(display_mode, display_mode)

        if not self._scroll_manager.is_mode_prepared(mode_key):
            # Unprepared despite a matching signature -- force past the skip.
            self._prepare_scroll_content(force=True)

        if not self._scroll_manager.is_mode_prepared(mode_key):
            return False

        self._scroll_manager.display_frame(mode_key, force_clear)
        return True

    # ─── Live race (OpenF1) ────────────────────────────────────────────

    def _poll_live_race(self) -> None:
        """Fetch a snapshot and rebuild live cards if it moved.

        f1-live: live_race. Called from update() only.
        """
        snap = self._live_feed.state()
        try:
            self._poll_last_session(snap)
        except Exception as e:
            self.logger.warning("Session result error: %s", e, exc_info=True)
        try:
            self._poll_podium(snap)
        except Exception as e:
            self.logger.warning("Podium error: %s", e, exc_info=True)
        sig = self._live_cards_signature(snap)
        self._live_snapshot = snap
        if sig == self._live_cards_sig:
            return
        self._live_cards_sig = sig
        if not snap:
            if self._vegas_live_race_cards:
                self.logger.info("Live race ended; reverting to normal cards")
            self._vegas_live_race_cards = []
            self._alert_flag = None
            return
        cards = self._build_live_race_cards(snap)
        self._vegas_live_race_cards = cards
        self._offer_flag_alert(snap, cards)
        self.logger.info(
            "Live race cards: %s lap=%s flag=%s n=%d session=%s",
            ",".join(e.get("code", "?") for e in (snap.get("entries") or [])[:8]),
            snap.get("lap"), snap.get("flag"), len(cards),
            snap.get("session_name") or "race")

    @staticmethod
    def _live_cards_signature(snap: Optional[Dict]) -> str:
        if not snap:
            return "none"
        payload = {
            "lap": snap.get("lap"),
            "flag": snap.get("flag"),
            "circuit": snap.get("circuit"),
            "entries": [
                (e.get("position"), e.get("code"), e.get("gap_to_leader"),
                 e.get("grid"), e.get("lap"))
                for e in (snap.get("entries") or [])
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _live_race_name(self, snap: Dict) -> str:
        """Prefer a proper GP name from data we already have."""
        circuit = (snap.get("circuit") or "").lower()
        country = (snap.get("country") or "").lower()
        candidates = []
        if self._upcoming_race:
            candidates.append(self._upcoming_race)
        candidates.extend(self._recent_races or [])
        for race in candidates:
            if not race:
                continue
            name = race.get("race_name") or ""
            blob = " ".join([
                name,
                race.get("circuit") or "",
                race.get("locality") or "",
                race.get("country") or "",
            ]).lower()
            if circuit and circuit in blob:
                return name
            if country and country in blob:
                return name
        return ""

    # --- Vegas alert (f1-live) ------------------------------------------
    #
    # The scroll takes each plugin's cards only on its turn in the rotation, so
    # a flag could take minutes to reach the wall. With the core patch
    # patches/patch_core_vegas_alert.py the render pipeline polls
    # get_vegas_alert() about once a second and splices a new alert's card into
    # the strip just ahead of the screen. Asked for on 2026-09-12. Without the
    # core patch nothing calls it, and this only keeps a card and logs a line.

    _ALERT_FLAGS = ("RED", "SC", "VSC")
    _ALERT_TTL_S = 60
    # Create this file to have one test alert offered on the next update; it is
    # removed when used. It sits in the plugin's own directory, the one place a
    # plugin may write (the plugin store's rule, 2026-09-13), rather than /tmp,
    # which may be private to the service. Its first word picks the card: see
    # _ALERT_TEST_FLAGS and _maybe_test_alert().
    _ALERT_TEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert-test")

    def get_vegas_alert(self):
        """(alert_id, card) while an alert is fresh, else None. Called on the
        render thread, so it only hands over a card built on the update tick."""
        alert = self._vegas_alert
        if not alert or time.time() - alert[2] > self._ALERT_TTL_S:
            return None
        return alert[0], alert[1]

    def _offer_alert(self, alert_id: str, card: Image.Image) -> None:
        self._vegas_alert = (alert_id, card.convert("RGB"), time.time())
        self.logger.info("Vegas alert offered: %s", alert_id)

    def _offer_flag_alert(self, snap: Dict, cards: List[Image.Image]) -> None:
        """A red flag, safety car or VSC has just come out: offer the new header
        card as an alert. Once per change of flag, not once per rebuild."""
        flag = str(snap.get("flag") or "").upper() or None
        if flag == self._alert_flag:
            return
        self._alert_flag = flag
        if flag in self._ALERT_FLAGS and cards:
            # Milliseconds, so a flag that ends and comes straight back is a
            # new alert rather than a repeat the core would skip.
            self._offer_alert("%s:%s:%.3f" % (snap.get("session_key") or "live", flag,
                                              time.time()), cards[0])

    # What the test file may ask for (2026-09-13: the user wanted to see a
    # yellow). An empty file, or anything else, is the red flag as before.
    # A yellow on this board is the safety car's yellow header: sector yellows
    # are not shown at all.
    _ALERT_TEST_FLAGS = {"RED": "RED", "SC": "SC", "YELLOW": "SC", "VSC": "VSC"}

    def _maybe_test_alert(self) -> None:
        try:
            if not os.path.exists(self._ALERT_TEST_FILE):
                return
            try:
                with open(self._ALERT_TEST_FILE, encoding="utf-8") as fh:
                    want = fh.read(16).strip().upper()
            except (OSError, UnicodeDecodeError):
                want = ""
            os.remove(self._ALERT_TEST_FILE)
        except OSError:
            return
        # WINNER: the winner card -- the podium's, or a sample without one.
        if want == "WINNER":
            podium = getattr(self, "_podium", None) or {}
            try:
                card = (self._winner_card(podium) if podium.get("entries")
                        else self._scroll_renderer.render_winner_card("TEST", "", ""))
                self._offer_alert("winner-test:%.3f" % time.time(), card)
            except Exception as e:
                self.logger.warning("Test winner alert failed: %s", e)
            return
        flag = self._ALERT_TEST_FLAGS.get(want, "RED")
        try:
            card = self._scroll_renderer.render_live_header(
                "ALERT TEST", flag, session_label="F1 LIVE")
            self._offer_alert("test:%.3f" % time.time(), card)
        except Exception as e:
            self.logger.warning("Test alert failed: %s", e)

    def _build_live_race_cards(self, snap: Dict) -> List[Image.Image]:
        """Header + one row per driver, same layout as the finished-race grid.

        f1-live: live_race
        """
        r = self._scroll_renderer
        # Name only on the title line; lap and flag go to the subtitle so the
        # LIVE chip has somewhere to sit.
        # F1's feed names the meeting itself ("Spanish Grand Prix"), which
        # beats matching it against the schedule: ESPN's copy carries the
        # sponsor ("Tag Heuer Spanish Grand Prix") and would not fit.
        name = snap.get("meeting_name") or self._live_race_name(snap)
        title = (name.replace("Grand Prix", "GP").strip().upper() if name
                 else (snap.get("circuit") or snap.get("country") or "RACE").upper())
        timed = str(snap.get("session_type") or "Race").lower() != "race"
        cards = [r.render_live_header(
            title, snap.get("flag"), snap.get("lap"),
            session_label=snap.get("session_name") if timed else None)]
        # How many cars: live.cars, 10 by default (asked for on 2026-09-13:
        # "make it an option to show all twenty-two cars"). It used to be
        # recent_races.top_finishers, the finished-race grid's own setting.
        try:
            top_n = max(1, int((self.config.get("live") or {}).get("cars") or 10))
        except (TypeError, ValueError):
            top_n = 10
        entries = snap.get("entries") or []
        shown = list(entries[:top_n])
        codes = {(e.get("code") or "").upper() for e in shown}
        if self.favorite_driver and self.favorite_driver not in codes:
            fav = next(
                (e for e in entries
                 if (e.get("code") or "").upper() == self.favorite_driver),
                None)
            if fav:
                shown.append(fav)
        leader_lap = snap.get("lap")
        # In a race, the time to the car ahead by default, as on TV (asked for
        # on 2026-09-13); live.race_gap "leader" puts the gap to the leader back.
        race_gap = str((self.config.get("live") or {}).get("race_gap")
                       or "interval").strip().lower()
        if race_gap not in ("interval", "leader"):
            race_gap = "interval"
        # The race's fastest lap so far, marked on its holder's row (asked for
        # on 2026-09-13). Not in practice or qualifying, where every row is
        # already a best lap.
        fastest = None if timed else fastest_lap_code(entries)
        for entry in shown:
            row = live_row_from_entry(entry, leader_lap, timed=timed, race_gap=race_gap)
            if fastest and (entry.get("code") or "") == fastest:
                row["fastest_lap"] = True
                row["fastest_lap_time"] = entry.get("best_lap") or ""
            cards.append(r.render_race_row(row, live=True))
        return cards

    # ─── Vegas Mode ────────────────────────────────────────────────────

    # Sections the marquee can show, in the order they are emitted. The keys
    # are what a user puts in `vegas.sections`.
    #
    # Deliberately a small default. Contributing every prepared mode measured
    # 114 cards / 14,592px on a 512px panel — near six minutes of uninterrupted
    # F1 at 50px/s, because what the plugin's own rotation shows as eight
    # separate screens the marquee splices into one unbroken block.
    _VEGAS_SECTION_ORDER = (
        "live_race",
        "leaders",
        "battles",
        "spotlight",
        "upcoming",
        "last_race",
        "driver_standings",
        "constructor_standings",
        "recent_races",
        "qualifying",
        "practice",
        "sprint",
        "calendar",
    )
    _VEGAS_DEFAULT_SECTIONS = ("upcoming", "last_race")

    # Sections that are just one or more prepared scroll modes, concatenated.
    # "upcoming" and "last_race" are not here: the first renders fresh so its
    # countdown is current, the second comes from its own card list.
    _VEGAS_SECTION_MODES = {
        "leaders": ("championship_leaders",),
        "battles": ("championship_battle", "constructor_battle"),
        "spotlight": ("driver_spotlight", "team_spotlight"),
        "driver_standings": ("driver_standings",),
        "constructor_standings": ("constructor_standings",),
        "recent_races": ("recent_races",),
        "qualifying": ("qualifying",),
        "practice": ("practice",),
        "sprint": ("sprint",),
        "calendar": ("calendar",),
    }

    def _vegas_sections(self) -> List[str]:
        """
        Which sections this plugin contributes to the marquee.

        Unknown names are dropped with a warning rather than failing the whole
        list, so one typo costs the user that section and not the plugin.
        """
        # The schema forbids a non-object here, but config.json is hand-edited
        # often enough that a null or a stray list must not raise on the
        # marquee's render path.
        vegas_cfg = self.config.get("vegas") or {}
        if not isinstance(vegas_cfg, dict):
            self.logger.warning(
                "vegas should be an object, got %r — using the default sections",
                vegas_cfg)
            return list(self._VEGAS_DEFAULT_SECTIONS)

        raw = vegas_cfg.get("sections", self._VEGAS_DEFAULT_SECTIONS)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            self.logger.warning(
                "vegas.sections should be a list, got %r — using the default",
                raw)
            return list(self._VEGAS_DEFAULT_SECTIONS)

        wanted, unknown = [], []
        for name in raw:
            key = str(name).strip().lower()
            if key in self._VEGAS_SECTION_ORDER:
                wanted.append(key)
            elif key:
                unknown.append(key)

        if unknown:
            self.logger.warning(
                "Ignoring unknown vegas.sections entries: %s (valid: %s)",
                ", ".join(unknown), ", ".join(self._VEGAS_SECTION_ORDER))

        # An empty list is a deliberate "keep F1 out of the marquee", so it is
        # honoured; only a list with nothing usable in it falls back.
        if not wanted and unknown:
            return list(self._VEGAS_DEFAULT_SECTIONS)
        return wanted

    def _vegas_section_images(self, section: str) -> List[Image.Image]:
        """Rendered cards for one marquee section, empty when it has no data."""
        if section == "live_race":
            return list(self._vegas_live_race_cards)

        if section == "upcoming":
            if not self._upcoming_race:
                return []
            # Rendered per call, not taken from a prepared mode, so the
            # countdown is current every time the marquee rebuilds the strip.
            images = [self._scroll_renderer.render_upcoming_race(
                self._enrich_upcoming_with_countdown(self._upcoming_race))]
            if self._scroll_renderer.show_circuit_info:
                images.append(self._scroll_renderer.render_circuit_info_card(
                    self._upcoming_race))
            return images

        if section == "last_race":
            return list(self._vegas_last_race_cards)

        images = []
        for mode_key in self._VEGAS_SECTION_MODES.get(section, ()):
            if self._scroll_manager.is_mode_prepared(mode_key):
                images.extend(
                    self._scroll_manager.get_vegas_items_for_mode(mode_key))
        return images

    @staticmethod
    def _session_time(value) -> Optional[datetime]:
        try:
            ts = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    @staticmethod
    def _called_off(session) -> bool:
        """A session ESPN lists as cancelled or postponed. It never starts, so a
        weekend made only of those -- Bahrain and Saudi Arabia in April 2026,
        every session "Canceled" -- is not a newer weekend, and must not hide
        the last real one's results."""
        status = " ".join(str(session.get(k) or "") for k in ("status_detail", "status_short"))
        return "cancel" in status.lower() or "postpone" in status.lower()

    def _newest_started_weekend(self, now: Optional[datetime] = None):
        """(race day, name) of the newest Grand Prix weekend whose first session
        has started -- the one under way, or the one just finished. None if none.

        From the whole schedule, kept by _update_upcoming(). Falls back to the
        upcoming race alone, which is enough during a weekend but not after it:
        once a race is over the upcoming race is the next one, and only the
        schedule still knows the weekend that just ended. Sessions called off
        do not count (_called_off).
        """
        now = now or datetime.now(timezone.utc)
        events = self._schedule_events or ([self._upcoming_race] if self._upcoming_race else [])
        best = None
        for ev in events:
            if not isinstance(ev, dict):
                continue
            starts, race_day = [], None
            for s in ev.get("sessions") or []:
                ts = self._session_time(s.get("date"))
                if ts is None or self._called_off(s):
                    continue
                starts.append(ts)
                if str(s.get("type_abbr") or "").lower() == "race":
                    race_day = ts.date()
            if not starts:
                continue
            first = min(starts)
            if first <= now and (best is None or first > best[0]):
                best = (first, race_day or max(starts).date(), ev.get("name") or "")
        return (best[1], best[2]) if best else None

    def _race_starts(self) -> List[datetime]:
        """Start times of the races in the schedule; called-off ones left out."""
        events = (getattr(self, "_schedule_events", None)
                  or ([self._upcoming_race] if getattr(self, "_upcoming_race", None) else []))
        starts = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            for s in ev.get("sessions") or []:
                if str(s.get("type_abbr") or "").lower() != "race" or self._called_off(s):
                    continue
                ts = self._session_time(s.get("date"))
                if ts is not None:
                    starts.append(ts)
        return starts

    def _race_started(self, day, now: datetime) -> bool:
        """Has the race Jolpica dates `day` started? Its start is the schedule's
        race a day either side (the sources could date a race that starts after
        midnight UTC a day apart); a race the schedule lacks counts once its day
        is over."""
        starts = [ts for ts in self._race_starts() if abs((ts.date() - day).days) <= 1]
        if starts:
            return min(starts) <= now
        return now.date() > day

    def _race_started_since(self, t0: float) -> bool:
        """Has a race started after t0 (epoch seconds), and by now?"""
        now = time.time()
        return any(t0 < ts.timestamp() <= now for ts in self._race_starts())

    def _stale_sections(self, now: Optional[datetime] = None) -> set:
        """Result sections showing a Grand Prix older than the newest weekend.

        Asked for on 2026-09-11. First qualifying: once practice starts for a
        new Grand Prix, the previous one's qualifying should be off the ticker
        -- Monza's Q3 cards were still scrolling on the Friday of the Spanish
        GP, because Jolpica publishes the new round only on Saturday. Then the
        race result too ("I'm still seeing last week results").

        Keyed on the newest weekend that has STARTED, not on the upcoming race,
        so last weekend does not come back on Sunday evening: by then the
        upcoming race is the next one, while Jolpica can take an hour or two to
        publish the race that just finished. Dates, not rounds -- the Jolpica
        results and the ESPN schedule share no round numbers. A result is stale
        when its Grand Prix is dated before the newest weekend's race day, less a
        day: the two sources agree on every 2026 date, Las Vegas included, but a
        race that starts after midnight UTC could be dated a day apart.

        Qualifying also goes as its own race starts, without waiting for the
        next weekend (asked for on 2026-09-13, after the Spanish GP): from then
        on the wall shows the race's results. See _race_started().
        """
        now = now or datetime.now(timezone.utc)
        newest = self._newest_started_weekend(now)
        hide = set()
        notes = getattr(self, "_stale_notes", None)
        if notes is None:
            notes = self._stale_notes = {}
        latest_race = (self._recent_races or [None])[0]
        for section, data, what in (("qualifying", self._qualifying, "qualifying"),
                                    ("last_race", latest_race, "race result")):
            day = None
            if isinstance(data, dict):
                try:
                    day = datetime.strptime(str(data.get("date") or "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    day = None
            why = None
            if newest and day is not None and day < newest[0] - timedelta(days=1):
                why = "the %s weekend has started" % (newest[1] or "next")
            elif section == "qualifying" and day is not None and self._race_started(day, now):
                why = "its race has started"
            note = (data.get("race_name"), why) if why else None
            if note != notes.get(section):
                notes[section] = note
                if why:
                    self.logger.info("Hiding the %s %s: %s",
                                     data.get("race_name") or "previous", what, why)
            if why:
                hide.add(section)
        return hide

    def get_vegas_content(self) -> Optional[List[Image.Image]]:
        """Return rendered cards for the configured marquee sections."""
        # Local patch (repo patches/patch_f1_race_rows.py): emit in the order
        # the user wrote in vegas.sections. This used to build a set and then
        # walk _VEGAS_SECTION_ORDER, where "last_race" (index 4) precedes
        # "qualifying" (index 9) -- so qualifying always played after the race,
        # which is backwards: qualifying happens first. The configured list was
        # already in the right order and was being discarded.
        images = []
        seen = set()
        emitted = []
        # f1-live: live_race -- always first when we have a snapshot, and
        # hide last_race so the previous GP is not scrolling next to this one.
        if self._vegas_live_race_cards:
            images.extend(self._vegas_live_race_cards)
            seen.add("live_race")
            seen.add("last_race")
            emitted.append("live_race")
        # f1-live: after a race, its top three, until Jolpica publishes it
        # (_poll_podium, asked for on 2026-09-13).
        elif getattr(self, "_podium_cards", None):
            images.extend(self._podium_cards)
            seen.add("podium")
            emitted.append("podium")
        # f1-live: last_session -- the last finished practice or qualifying,
        # from F1's feed, first until the next session goes live. A stored
        # qualifying result hides the qualifying section: that is the same
        # session from Jolpica once it catches up, and the previous weekend's
        # until then.
        elif self._vegas_last_session_cards:
            images.extend(self._vegas_last_session_cards)
            seen.add("last_session")
            emitted.append("last_session")
            if str((self._last_session or {}).get("session_type") or "").lower() == "qualifying":
                seen.add("qualifying")
        # f1-live: results older than the newest weekend under way (or just
        # done) go -- last weekend's qualifying and race once FP1 of the
        # next one starts, and they do not come back after it.
        seen.update(self._stale_sections())
        for section in self._vegas_sections():
            if section in seen:
                continue
            seen.add(section)
            chunk = self._vegas_section_images(section)
            if chunk:
                emitted.append(section)
                images.extend(chunk)
        if images:
            self.logger.info("vegas emit %s (%d images)",
                             ",".join(emitted), len(images))

        return images if images else None

    def get_vegas_content_type(self) -> str:
        """Return multi for scrolling content."""
        return "multi"

    def get_vegas_display_mode(self) -> VegasDisplayMode:
        """Return SCROLL for continuous scrolling."""
        return VegasDisplayMode.SCROLL

    # ─── Dynamic Duration ──────────────────────────────────────────────

    _SCROLL_MODES = frozenset({
        "f1_live_driver_standings", "f1_live_constructor_standings",
        "f1_live_recent_races", "f1_live_qualifying", "f1_live_practice",
        "f1_live_sprint", "f1_live_calendar",
    })

    _MODE_KEY_MAP = {
        "f1_live_driver_standings": "driver_standings",
        "f1_live_constructor_standings": "constructor_standings",
        "f1_live_recent_races": "recent_races",
        "f1_live_qualifying": "qualifying",
        "f1_live_practice": "practice",
        "f1_live_sprint": "sprint",
        "f1_live_calendar": "calendar",
    }

    def supports_dynamic_duration(self) -> bool:
        """Enable dynamic duration for scrolling modes."""
        dd = self.config.get("dynamic_duration", {})
        if not isinstance(dd, dict) or not dd.get("enabled", True):
            return False
        return (self._current_display_mode is not None
                and self._current_display_mode in self._SCROLL_MODES)

    def is_cycle_complete(self) -> bool:
        """Scroll cycle complete when ScrollHelper reports done."""
        if not self._current_display_mode:
            return True
        mode_key = self._MODE_KEY_MAP.get(self._current_display_mode)
        if not mode_key:
            return True
        return self._scroll_manager.is_scroll_complete(mode_key)

    def reset_cycle_state(self) -> None:
        """Reset scroll position for the current mode."""
        super().reset_cycle_state()
        if self._current_display_mode:
            mode_key = self._MODE_KEY_MAP.get(self._current_display_mode)
            if mode_key:
                self._scroll_manager.reset_mode(mode_key)

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def get_info(self) -> Dict[str, Any]:
        """Return diagnostic info for the web UI."""
        info = super().get_info()
        info.update({
            "name": "F1 Live",
            "enabled_modes": self.modes,
            "mode_count": len(self.modes),
            "last_update": self._last_update,
            "has_driver_standings": bool(self._driver_standings),
            "has_constructor_standings": bool(self._constructor_standings),
            "has_recent_races": bool(self._recent_races),
            "has_upcoming_race": self._upcoming_race is not None,
            "has_qualifying": self._qualifying is not None,
            "has_practice": bool(self._practice_results),
            "has_sprint": self._sprint is not None,
            "has_calendar": bool(self._calendar),
            "favorite_driver": self.favorite_driver,
            "favorite_team": self.favorite_team,
            "is_live": self._is_live,
            "live_session": self._live_session,
            "is_race_weekend": self._is_race_weekend,
            "effective_update_interval": self._update_interval,
        })
        return info

    def on_config_change(self, new_config):
        """Handle config changes."""
        super().on_config_change(new_config)

        self.favorite_driver = new_config.get("favorite_driver", "").upper()
        self.favorite_team = normalize_constructor_id(
            new_config.get("favorite_team", ""))
        self._base_update_interval = new_config.get("update_interval", 3600)
        self._update_interval = self._base_update_interval
        self.display_duration = new_config.get("display_duration", 30)
        self.modes = self._build_enabled_modes()

        # Re-resolve timezone in case global config changed. Kept in a shallow
        # copy (never written back into `new_config`) so it never gets
        # persisted as a stale plugin-level override.
        self.timezone = self._resolve_timezone(new_config, self.cache_manager)
        render_config = {**new_config, "timezone": self.timezone}

        # Force re-render with new settings
        scroll_cfg = render_config.get("scroll", {}) if isinstance(render_config.get("scroll"), dict) else {}
        self._card_width = scroll_cfg.get("game_card_width", 128)
        self.renderer = F1Renderer(
            self.display_width, self.display_height,
            render_config, self.logo_loader, self.logger)
        self._scroll_renderer = F1Renderer(
            self._card_width, self.display_height,
            render_config, self.logo_loader, self.logger)
        self._scroll_manager = ScrollDisplayManager(
            self.display_manager, render_config, self.logger,
            global_config=getattr(self, 'global_config', {}) or {})
        self.enable_scrolling = self._scroll_manager is not None
        self._scroll_content_sig = None
        self._stop_live_feed()
        self._live_feed = self._make_live_feed(new_config)
        self._live_feed_interval = self._live_poll_interval(new_config)
        self._last_live_feed = 0.0
        self._live_cards_sig = None
        self._vegas_live_race_cards = []
        self._live_snapshot = None
        # The renderer was just replaced, so the stored result's cards are
        # stale even though the result is not.
        self._last_session_sig = None
        self._vegas_last_session_cards = []

        # Force data refresh
        self._last_update = 0

    def cleanup(self):
        """Clean up resources."""
        self._stop_live_feed()
        try:
            self.logo_loader.clear_cache()
            self.logger.info("F1 Live cleanup completed")
        except Exception:
            self.logger.exception("Error during F1 Live cleanup")
        super().cleanup()
