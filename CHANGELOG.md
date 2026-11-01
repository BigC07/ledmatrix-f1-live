# Changelog

Every release of F1 Live, newest first. The plugin is a fork of ChuckBuilds'
[F1 Scoreboard](https://github.com/ChuckBuilds/ledmatrix-plugins) 1.8.6; changes upstream made
before the fork are not repeated here.

Dates are the day the version was published. Settings named here are described in
[README.md](README.md).

## [1.12.1] - 2026-11-01

### Fixed

- **A red flag stays RED for the whole suspension.** Mexico City, live: the race was red-flagged,
  the board showed RED three times, and then it showed **YELLOW with the cars still stopped**. The
  owner saw it on the wall.
- `TrackStatus` does not hold `5` for the length of a stoppage. At that moment the feed read
  `SessionStatus {"Status": "Aborted", "Started": "Started"}` with `TrackStatus {"Status": "2",
  "Message": "Yellow"}`, and the flag was taken from `TrackStatus` alone, so it downgraded.
- `Aborted` **is** the suspension -- it is why that value already counts as a live status -- so it
  now outranks the track's moment-to-moment colour. The same reasoning that mapped code 7 to VSC so
  the rail would stop snapping back for the last seconds of a VSC period.
- The hold does not latch: once the session reports running again the flag follows the track, so a
  local yellow after a restart still reads YELLOW. A safety car, a VSC and a clear track while
  running are all unchanged.

## [1.12.0] - 2026-11-01

### Added

- **Race control's incident marker on the live board** -- the amber `!` the broadcast puts beside a
  driver, asked for from a photograph of the Mexico City race where the tower carried it next to
  LEC and HAM and the wall carried nothing.
- There is **no per-driver field for it in the feed**. Checked on the wire: a driver line carries
  `BestLapTime`, `GapToLeader`, `InPit`, `IntervalToPositionAhead`, `LastLapTime`, `Line`,
  `NumberOfLaps`, `PitOut`, `Position`, `RacingNumber`, `Retired`, `Sectors`, `ShowPosition`,
  `Speeds`, `Status`, `Stopped` -- and nothing about the stewards. `RaceControlMessages` was
  already subscribed to and had never been read. The broadcast parses the same prose we now do.
- `TURN 4 INCIDENT INVOLVING CARS 44 (HAM) AND 16 (LEC) NOTED` marks **both** cars; the second one
  carries no "CAR" of its own, so the parse matches the number-and-code pair rather than the word.
  `UNDER INVESTIGATION` upgrades `NOTED`, a `TIME PENALTY` outranks both, and a car keeps the worst
  thing open against it. A deleted lap marks nobody: it is about one lap time, settled the moment
  it is announced, and the broadcast marks it differently.
- Marks clear after `live.incident_ttl_s`, default 600s. The stewards announce incidents and never
  an all-clear, so without a clock an incident noted on lap 2 would still be on the card at the flag.
- One amber for all three states -- at a glance they all mean the stewards are looking at this car,
  and the row has no width for three symbols. Amber, not red: red already means LIVE here, on the
  rail and on the badge.
- Known gap: only the **race** row carries it. The practice/qualifying row (`_render_driver_row`)
  is a separate renderer fed by a separate builder and was left alone rather than changed untested.

## [1.11.1] - 2026-11-01

### Fixed

- **The live board no longer goes up before the race starts.** F1 sets
  `SessionStatus.Started` to `"Started"` during the pre-race procedure, and the feed reader treated
  that field on its own as "the session is running". On 2026-11-01 the board appeared at 14:15 for a
  15:00 race -- `{"Status": "Inactive", "Started": "Started"}`, the session clock still on a full
  `02:00:00` and not extrapolating, the pit exit only just closed -- and because `live.solo_wall` is
  keyed to the live cards, F1 took the whole wall 45 minutes early, holding back football,
  basketball, the clock and the weather.
- The clause could not simply be deleted: it is also what holds the board up through a **red flag**,
  where `Status` falls back to `"Inactive"` while the session is still running (seen in FP2 on
  2026-09-11, with 18 minutes left). Pre-race and a red-flag suspension look identical on those two
  fields and differ only in history. The reader now **latches** once the session has genuinely been
  seen under way -- a live `Status`, or an `ExtrapolatedClock` that is counting down -- and only
  honours `Started` after that. Before the lights it claims nothing; a red flag behaves exactly as
  it did. The latch resets whenever the session changes.
- Known gap, **corrected 2026-11-01**: this first said a connection made *during a red flag* would
  leave the board down. That is wrong, and was written without checking -- a red flag reports
  `SessionStatus.Status` as `"Aborted"`, which is itself one of the live statuses, so the first
  clause fires and the board comes straight up. The real gap is narrower: a first connection during
  a suspension that reports `Inactive` / `Started` (the FP2 shape, where neither field says
  running) waits for the session to resume. Rarer still, self-healing, and far cheaper than taking
  the wall before every race.

## [1.11.0] - 2026-10-12

### Added
- **A local yellow flag reaches the board.** F1's `TrackStatus` code **2** was never in the map, so a
  waved yellow produced nothing at all -- no badge, no rail -- while a safety car and a VSC both did.
  It now draws a `YELLOW FLAG` badge on the header and turns the rail the same yellow a safety car
  does. Deliberately the *same* yellow: three shades are not tellable apart on a two-pixel rail, and
  at a glance a local yellow and a safety car say the same thing -- caution on track. The badge is
  what distinguishes them.

### Fixed
- **A VSC's last seconds no longer snap the rail back to red.** `TrackStatus` **7** is the VSC
  winding down, and it was unmapped; the VSC is still in force until the track goes green, so the
  rail went red while the cars were still behind the delta. Code 7 now maps to `VSC`.

## [1.10.0] - 2026-10-11

### Changed
- **The flag colours the rail, on every card in the live block.** A safety car turns the rail the
  `SAFETY CAR` badge's yellow and a virtual safety car the `VIRTUAL SC` badge's darker yellow --
  on the header *and* on every driver row under it, so the whole section reads as flagged from any
  glance. Asked for during the Singapore race: the badge rides the header, and a header is on screen
  about 1.5% of a strip cycle, so once it scrolled past nothing said a safety car was out. That is
  the same reasoning that put the rail on every row in the first place. The rail reuses the badges'
  own colours, so the two agree instead of being two different yellows, and a flag outranks the
  sprint colour -- a safety car matters more than which race this is.
- **A red flag is deliberately unchanged.** Rendered and looked at: the alarm red (`230,0,0`) and
  the live rail (`255,40,40`) are the same to the eye on a driver row, so a red flag cannot announce
  itself in red. White and a 4px rail both worked and both were offered; the owner chose to leave it
  alone. The `RED FLAG` badge still carries it, and the session is stopped anyway.

## [1.9.1] - 2026-10-10

### Changed
- **A sprint is named by the card, not by the chip.** `LIVE` is back in the chip at the top right,
  where it says `LIVE` in every session as it always did. A sprint says so two other ways, neither
  of which costs any width: the live header turns sprint orange -- the same orange the sprint
  results card uses -- and the lap counter reads `SPRINT 12/20` in place of `LAP 12/20`. Because
  nothing on the card got wider, the title keeps `GP`; 1.9.0 dropped it to make room for the wider
  chip, on a measurement taken against a 128px card. On a 256px panel `SINGAPORE GP` is 84px
  against 211 available. Sprint qualifying is timed, names itself on the second line, and is
  unchanged.

### Fixed
- `last_updated` in the manifest, which 1.9.0 left at the 1.8.0 date.

## [1.9.0] - 2026-10-10

### Changed
- **The live header says when it is a sprint.** F1's feed types a sprint race as `Race` and names
  it `Sprint`, so the board drew the Singapore sprint as the Grand Prix -- `SINGAPORE GP` with the
  LIVE chip, and `LAP 12/20` the only hint it was not Sunday. During a sprint the chip reads
  `SPRINT` and the title drops `GP` so the name keeps its full-size title beside the wider chip (on a
  128px card the title is 7x13 and the chip 6x10, and the header steps a title that does not fit down
  a font tier, so `SINGAPORE GP` beside `SPRINT` would shrink). Sprint qualifying already named
  itself on the second line and is unchanged.

## [1.8.0] - 2026-09-26

### Changed
- **The chequered winner card rides the marquee instead of interrupting it.** It was offered
  through the alert path, which splices a card into the running strip just ahead of the viewport --
  a mechanism for things that matter in the next thirty seconds, like a red flag or a safety car. A
  race winner is not that: the race is over and nothing is changing. Watching it at Baku it wedged
  a chequered card between two driver rows mid-scroll. It now leads the podium block for
  `live.winner_rotations` passes (2 by default, `0` to turn it off) and then drops away, leaving
  the block as the result.
- Race gaps are one decimal, as the TV tower shows them: `+13.7`, not `+13.740`. Qualifying and
  practice keep their thousandths, where they decide the order.

### Added
- `live.race_gap: "auto"`, now the default: the gap column alternates the way the TV timing tower
  does -- it swaps between gap-to-leader and interval during a race -- and the header card carries
  `TO LEADER` or `INTERVAL` so you can tell which is up. `live.race_gap_seconds` sets the swap
  interval (60 by default). A flag badge takes the header line ahead of the label.
- The lap counter shows the total: `LAP 49/51`. It was in the feed already.

## [1.7.0] - 2026-09-26

### Changed
- **Race rows show the gap to the leader by default**, not the time to the car ahead. That is the
  column the TV timing tower shows -- checked against a photo of the broadcast during the 2026
  Azerbaijan GP, which read `VER +0.8, HAD +4.5, LEC +5.7, HAM +8.9` where this plugin was showing
  the differences between adjacent cars.
- It had been the interval since 1.1.0, on the belief that the interval was what TV displayed. It
  is not. Anyone watching a live race with this beside the broadcast saw two sets of numbers that
  disagreed and nothing to explain why, which is why the default moved rather than one wall's
  config.
- `live.race_gap` set to `"interval"` restores the old behaviour. It is genuinely the better
  number for seeing who is about to be caught -- it just is not what the broadcast shows.

## [1.6.0] - 2026-09-26

### Added
- `visual.row_logo_height`: how tall a driver row's team badge is drawn, in pixels. `0`, the
  default, keeps the automatic size, so an update changes nothing unless you ask it to. Much past
  22 on a 32px card and a round badge starts crowding the row.

### Fixed
- A driver row's badge is centred in the height it actually has. Both row renderers centred it
  against the full card, ignoring the 2px team-colour line along the bottom and, on a live row,
  the 2px rail along the top -- so a badge sat low by a pixel or two. Invisible at the automatic
  size, obvious as soon as the badge is made bigger.

## [1.5.0] - 2026-09-26

### Added
- `live.tyres`: the compound each car is on, as a coloured ring beside the team logo -- red soft,
  yellow medium, white hard, green intermediate, blue wet -- with the laps on that set beside it.
  A car that has just pitted reads at a glance.
- On live rows it takes the place of the places-gained figure, which shares that strip of the row;
  finished race results are untouched and keep theirs. Off by default for that reason.
- Where a car also holds the fastest lap, the lap time shortens itself to make room rather than
  running into the ring.
- `live.tyres.position` picks where it goes. `beside_logo` (the default) puts the ring between the
  driver name and the team badge and leaves the places-gained figure alone; `places_slot` takes that
  figure's place and adds the laps on the set beside the ring.
- A set that has not completed a lap reads `NEW` rather than `0`, for the one lap that is true. A
  scrubbed set is not caught by it: the feed counts the laps already on those tyres.
- No extra requests: `TimingAppData` was already one of the topics subscribed at connect and only
  `GridPos` was being read out of it. Tyres come from F1's own feed, so an OpenF1 replay shows none.

### Changed
- A driver surname that will not fit now drops one font size before it is given up for the
  three-letter code. 7x13 was chosen because `P3 VERSTAPPEN` is 91px of the 99 available; anything
  that narrows that column by even a pixel used to cost the whole name. At 6x10 the same name is
  60px, so the step buys far more than it costs.

## [1.4.0] - 2026-09-25

### Added
- `live.solo_wall`: while a live session is running, the plugin can take the whole display
  instead of its turn in the rotation -- a race ticker rather than a race card between the
  football and the clock. `enabled` is off by default; `session_types` picks which sessions
  take the wall, the race alone by default.
- The claim is tied to the live cards, not to the feed's own liveness, so it cannot outlive the
  board it exists for: when the cards go, at the hold after the chequered flag, the wall goes
  back by itself. Nothing is stored and there is nothing to switch off afterwards.
- A core has to ask the plugin for this (`get_vegas_solo()`); LEDMatrix does not upstream yet.
  Where it does not ask, the setting does nothing and the rotation carries on as before.

## [1.3.1] - 2026-09-20

### Added
- This changelog. Until now the release notes lived only in `manifest.json` and the README's
  Versions list, neither of which reads as a history.

### Fixed
- The README's version line still said 1.2.1 two releases later. It now matches the manifest.

No code changed in this release: 1.3.1 draws exactly what 1.3.0 draws.

## [1.3.0] - 2026-09-19

### Added
- **`recent_races.podium_only_after_days`** — a shelf life for a race result. One race stays on
  the ticker until the next one, so a ten-row result can still be scrolling mid-week. Once a race
  is this many days old the block shows only the podium: the winner and second and third.
  `0`, the default, keeps the result as it is until the next race. In the web UI it is
  **Recent Race Results → Podium Only After**.
- The favourite driver is **not** appended once a result has dropped to the podium, so the podium
  is exactly three rows whoever the favourite is. Inside the window `always_show_favorite` works
  as before.

### Notes
- The full classification is kept behind the scenes, so the points-haul and gap cards still see
  every driver.
- A race whose date the results feed does not give is never trimmed.
- The cutoff is re-read at every update, so a result drops to the podium by itself. No restart.

## [1.2.1] - 2026-09-13

### Added
- Safety car and virtual safety car badges on the live header, drawn like the red flag's:
  **SAFETY CAR** in yellow and **VIRTUAL SC** in a darker yellow, at the end of the header's
  second line. Where the full words would cut the line — beside a practice session's name, for
  instance — the badge reads **SC** or **VSC**.

### Changed
- The live header keeps its usual colours while a badge is showing, rather than recolouring the
  whole line.

## [1.2.0] - 2026-09-13

### Changed
- **Display modes are `f1_live_*`** (`f1_live_upcoming`, `f1_live_qualifying`,
  `f1_live_recent_races` and the rest), so they no longer collide with F1 Scoreboard's `f1_*`
  modes when both plugins are installed.
- The alert test file is `alert-test` in the plugin's own folder, instead of a fixed path in
  LEDMatrix's cache directory.
- The README installs from this repository's URL.

### Added
- `example_config.json`, the main settings as they sit in `config.json`.
- **`live.cars`** — how many cars the live board shows, from the front. 10 by default, up to the
  whole field. The favourite driver is added when they finish outside that cut.

## [1.1.0] - 2026-09-13

First release under its own name and repository. It carries everything the fork had added to
F1 Scoreboard 1.8.6 since 2026-09-07.

### Added
- **Live timing from F1's own feed.** Live boards for the race, qualifying and practice, with the
  interval to the car ahead, places gained or lost against the grid, and lapped and retired cars.
  `live.source` still selects `openf1`, but OpenF1 answers 401 to anonymous clients while a
  session is running, which is why the default is F1's feed.
- **Flags on the live header**: red flag, safety car and virtual safety car.
- **The fastest lap**: a purple stopwatch and the lap time on the holder's row.
- **After the flag**: a chequered winner card, then the podium leading the block until the
  official result is published.
- **Results between sessions**: a finished practice or qualifying stays on the ticker until the
  next session goes live, and qualifying leaves when its race starts.
- **The race result as a grid** — a name card and then one row per finisher, the way qualifying
  already read, instead of a single three-column podium card. Depth comes from
  `recent_races.top_finishers`.
- **`vegas.sections`** — which sections join the marquee, in the order they are listed.

### Changed
- Readability on a 32-pixel panel: full surnames on race rows, and lap times, gaps and race times
  one size larger.
- Team logos cleaned up for the panel, with drawn crests for Cadillac and Audi.

### Notes
- Two small LEDMatrix core patches are needed for cards that jump into the scroll (the flag and
  winner cards) and for cards repainted while they scroll. Without them everything else works.
