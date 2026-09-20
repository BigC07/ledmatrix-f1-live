# Changelog

Every release of F1 Live, newest first. The plugin is a fork of ChuckBuilds'
[F1 Scoreboard](https://github.com/ChuckBuilds/ledmatrix-plugins) 1.8.6; changes upstream made
before the fork are not repeated here.

Dates are the day the version was published. Settings named here are described in
[README.md](README.md).

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
