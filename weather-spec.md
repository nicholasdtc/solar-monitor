# Weather: forecast, warnings, dashboard widget, Weather tab, automation

Phase 7 of [spec.md](spec.md). It adds a weather forecast and weather warnings to the app: a Weather section on the Dashboard, a Weather tab with the hourly detail, weather kept by the hour alongside the statistics, and the groundwork for battery automation driven by the forecast.

Nothing in this phase writes to the inverter. The first automation that acts is V2, below.

## Goals

| # | Goal | Step |
|---|------|------|
| 1 | Pluggable weather sources: any forecast or warnings service can be added without touching the rest of the app | W1 |
| 2 | Fetch a forecast and warnings for the saved location on the Pi, and keep them in SQLite | W1 |
| 3 | Keep the weather for every hour, back to the start of the log, so the statistics can be compared with it | W2 |
| 4 | Weather tab: warnings, the next 48 hours by the hour, and 7 days by the day | W3 |
| 5 | Dashboard: a Weather section on the cards side, and sections that fold up from their headings | W4 |
| 6 | Expected solar output per hour, and how the forecast compared with what happened | W5 |
| 7 | Automation stub: works out what it would do, shows it and logs it, and never acts | W6 |
| 8 | V2: lower a chosen TOU period's charge cut-off before a sunny morning | V2 |

## Choosing the sources

| | Yr (MET Norway Locationforecast 2.0) | Open-Meteo | Met Éireann |
|---|---|---|---|
| Covers | The world | The world. For Ireland, the UK Met Office's 2 km model in its "best match" | Ireland |
| Cost and key | Free, no key | Free for non-commercial use, which covers personal home automation. No key | Free, no key |
| Solar radiation | **None.** Cloud cover and a clear-sky UV index only | **Yes**: global (GHI), direct, diffuse, and tilted (GTI) irradiance | No |
| Past data | No | Yes: past forecasts back to about 2022, and reanalysis back to 1940 | No |
| Warnings | Separate service (MetAlerts), Norway | No | Yes, a JSON feed by county |
| Terms | CC BY 4.0. A User-Agent naming the app and a contact. At most 4 decimals in coordinates. Don't ask again before `Expires`. "Yr" can't be in the app's name | CC BY 4.0. Under 10,000 calls a day | Licence based on CC BY 4.0. Credit Met Éireann |

**Does Yr work?** Yes, but it has no solar radiation and no past data, and both matter here: automation is really a question of how much the panels will make, and the statistics need past weather.

**Defaults for this house:**

- Forecast: **Open-Meteo**.
- Warnings: **Met Éireann**.
- Yr is the second forecast provider, written alongside, to prove the plug-in design works with a source that lacks irradiance and past data.

## Hard rules

- **Nothing about a place or a service is hardcoded outside its provider.** The core app knows only the provider interface (W1), its own units, and UTC. Someone in another country picks their sources, location and region on the Settings screen, or adds a provider module, and nothing else changes.
- **The location is personal data.** Latitude and longitude are entered on the Settings screen and kept in the database, never in code, git, tests, specs or logs. They're rounded to 2 decimals (about 1 km) when they're saved. No place name or address is stored or shown.
- **Fetched by the server, never by the browser.** The Pi fetches and caches, and the page reads only the app's own API. The location never leaves from viewers' devices, the kiosk keeps working without internet, and only one copy asks.
- **Never in the collector's way.** Fetches run on their own thread with short timeouts. A slow or failed fetch can't delay a poll or a flush.
- **Works offline.** Weather icons are bundled with the page, like the fonts. With no forecast the Weather section says so and gives the age of the last one. Nothing else on the page depends on the weather.
- **Polite fetching.** As often as the provider allows, at most hourly, at a random minute picked at start-up. Honour `Expires` and `If-Modified-Since` where the provider sends them. Back off on errors, and stop until the next hour on HTTP 429.
- **No writes to the inverter until V2,** and then only through `control.py`'s checked path (see V2).
- **Credit each source.** The Weather tab names each provider in use and links its licence, as CC BY 4.0 requires. Each provider supplies its own credit line.

## W1: providers, fetch and store

### The plug-in interface

A `weather/` package. The core sees only these:

```python
class ForecastProvider(Protocol):
    name: str                      # 'open-meteo', 'met-norway'
    title: str                     # 'Open-Meteo', shown on the Settings screen
    credit: Credit                 # text, licence name and link
    has_irradiance: bool
    has_past: bool                 # can it return past hours?
    min_interval: int              # seconds between fetches it allows

    def forecast(self, where: Location, fetch: Fetch) -> list[Hour]: ...
    def past(self, where: Location, start: int, end: int, fetch: Fetch) -> list[Hour]: ...  # if has_past

class WarningsProvider(Protocol):
    name: str
    title: str
    credit: Credit
    regions: list[Region]          # codes and names, offered on the Settings screen; empty if it works from coordinates

    def warnings(self, where: Location, region: str | None, fetch: Fetch) -> list[Warning]: ...
```

- **`Hour`** is the app's own record, in fixed units: °C, m/s, mm, %, W/m², degrees, UTC seconds for the start of the hour it covers. Any field a provider lacks is `None`. Converting units and moving end-of-hour labels to starts is the provider's job.
- **`condition`** in `Hour` is the app's own short list, not any provider's codes: clear, mostly clear, partly cloudy, cloudy, fog, drizzle, rain, heavy rain, sleet, snow, showers, thunder. Each provider maps its own codes (WMO codes for Open-Meteo, `symbol_code` for Yr) onto it. The page's bundled icons follow this list, so a new provider needs no new icons.
- **`Warning`** has the level (the app's own scale: minor, moderate, severe, extreme, mapped from yellow, orange and red or the CAP severity), type, headline, description, start, end, the regions covered, and a link to the issuer's page.
- **`fetch`** is injected. Tests pass a stub with saved responses, so tests never touch the network.
- Everything specific to a service lives in its module: URLs, parameter lists, code tables, units, rate limits and the User-Agent rule. Where a service can be self-hosted (Open-Meteo can), its base URL is a setting with the public one as the default.
- **A registry** lists the providers. Adding one is a module, a line in the registry, and tests with a saved response.

**As built in W1**, in `src/solar_monitor/weather/`:

| File | What |
|---|---|
| `base.py` | The interface: `Hour`, `Warning`, `Location`, `Region`, `Credit`, the conditions and levels, and the fetch errors |
| `http.py` | The one real `fetch`: urllib, short timeouts, the User-Agent, and HTTP 304 and 429 as their own errors |
| `__init__.py` | The registry, `FORECASTS` and `WARNINGS`, and the default forecast |
| `open_meteo.py`, `met_norway.py`, `met_eireann.py` | The first providers |
| `service.py` | The fetch loop the app starts and stops with itself |

**Credits.** Each provider's `credit` is what its terms ask for. Open-Meteo and MET Norway are CC BY 4.0. Met Éireann's licence asks for copyright, source, licence and a disclaimer, so its credit carries all four. The Weather tab (W3) and the Dashboard section (W4) show them.

**Warning text is shown as text, never as HTML.** Met Éireann's descriptions come as HTML. They are stored as issued, and the page strips the tags and sets `textContent`, so nothing from a feed runs on the page.

### Settings

All of these are in the "Solar panels and location" pane on the Setup tab's "Panels & weather" page. The provider choices list only the providers the registry has, and the region list follows the warnings provider. A region kept from another provider means all of them. All have the same permission as the other settings, and each change is recorded in the Action history (the location only as "changed").

| Setting | Default | Notes |
|---|---|---|
| Location | none | Latitude and longitude, rounded to 2 decimals. Without one, nothing is fetched and the Weather tab asks for it |
| Panels' total rating | none | kWp, as installed. The inverter can't report it |
| Panel tilt and facing | none | Degrees from flat, and the compass direction (180 is south). Entered by hand: the inverter can't report them, and SEMS+ shows what the installer typed |
| Second facing | none | Optional. For panels split between two directions, such as east and west roofs. The split is taken as even; a share setting can come if anyone has an uneven one |
| Forecast provider | Open-Meteo | From the registry |
| Warnings provider | none | From the registry. Met Éireann for this house |
| Warnings region | none | From the provider's list, when it has one. For Met Éireann, the county |
| Provider options | per provider | Later. For example Open-Meteo's model (default "best match") and base URL. W1 uses each provider's defaults |
| Units | the user's display preferences | With W3. Temperature, wind and rain units, alongside the existing date and time choices. Stored in the app's units, converted on the page |

`settings` holds numbers only, so the names and region go in a new key-value table for text settings.

### Requests (Open-Meteo, as the first provider)

- Forecast: `/v1/forecast` with the hourly fields the `Hour` record needs (temperature, feels like, precipitation and its probability, weather code, cloud cover total, low, mid and high, wind, gusts, direction, shortwave, direct and diffuse radiation, is_day), and daily sunrise, sunset and sunshine. `forecast_days=7`, `timeformat=unixtime`, `timezone=GMT`, `wind_speed_unit=ms`. W1 asks for the hours only: the daily figures are summed from them, and sunrise and sunset come in W3, worked out from the location or asked for then.
- With the panel tilt set, global tilted irradiance (GTI) for that tilt and facing as well (facing south if it isn't set). The provider converts the app's compass bearing to its own convention: Open-Meteo counts 0 as south, −90 east, 90 west. With a second facing set, each forecast or past request is followed by one more for that facing's GTI only, and each hour's GTI is the mean of the two.
- Past: the same fields from `historical-forecast-api.open-meteo.com/v1/forecast`, with start and end dates.
- User-Agent: the app and its public repo, `solar-monitor (github.com/nicholasdtc/solar-monitor)`. No email address.
- Open-Meteo labels radiation and precipitation by the end of the hour they cover, while temperature, cloud and wind are the value at that instant. The provider moves the first group back one hour, so every `Hour` is labelled by its start. Check against a sunny day's log.

### Warnings (Met Éireann, as the first provider)

- `https://www.met.ie/Open_Data/json/warning_IRELAND.json`, every 15 to 30 minutes (warnings change faster than forecasts), filtered to the chosen county.
- The county list ships with the provider module, as Met Éireann's own region codes.
- Kept in a `weather_warnings` table, keyed by the issuer's id, with first and last seen, so the history shows past warnings too.

### Tables

```sql
CREATE TABLE weather_fetches (
    id          INTEGER PRIMARY KEY,
    fetched_at  INTEGER NOT NULL,      -- UTC seconds
    provider    TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- 'forecast', 'past', 'warnings'
    outcome     TEXT NOT NULL,         -- 'ok', 'not modified', 'rate limited' or 'error'
    items       INTEGER NOT NULL,
    error       TEXT                   -- what went wrong, briefly
);

-- Forecast runs: what was expected, and when it was expected.
CREATE TABLE forecast_hours (
    fetch_id     INTEGER NOT NULL REFERENCES weather_fetches(id),
    hour_start   INTEGER NOT NULL,
    ...the Hour fields...,
    PRIMARY KEY (fetch_id, hour_start)
) WITHOUT ROWID;

-- The weather as it happened: one row per hour, kept for good (W2).
CREATE TABLE weather_hours (
    hour_start   INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,        -- 'past:open-meteo', or 'forecast:met-norway' when only a forecast was available
    fetched_at   INTEGER NOT NULL,
    panels       TEXT,                 -- the tilt and facings gti_wm2 is for, such as '30/180' or '30/90/270'
    ...the Hour fields...
) WITHOUT ROWID;
```

**Pruning forecast runs.** Automation needs to know how good the evening-before forecast was, so old runs are scored, not just the latest:

- The latest run keeps all its days.
- Older runs keep only the 48 hours after they were fetched.
- After 14 days, one run per day is kept, the last before the day-ahead decision time (W6), and the rest are deleted.

That's about 1,200 rows a day for the last fortnight, then 48 a day, so a few MB a year.

The warnings and the text settings go into the CSV export and the importer like the other tables, and the location goes with the other settings. The forecast runs and the fetch log are only in the full database export, and the importer leaves them out: an old forecast is no use on another copy. `weather_hours` (W2) goes in both, as it's the weather as it happened.

## W2: weather for every hour, for the statistics

`weather_hours` holds the weather for every hour the app has a log for, whichever provider is in use:

- **With a provider that has past data** (Open-Meteo): once a night, fill yesterday from its past service. Where the log has hours that `weather_hours` doesn't, fill those too: that covers the first backfill back to the start of the log, and the Pi being offline.
- **With a provider that hasn't**: when an hour has passed, keep the latest forecast for it, the one made closest to it, with its source marked as a forecast. Nothing is lost if the provider is changed later: the past service can overwrite these rows.
- The backfill runs in small chunks (a month per request), well inside the provider's limits, and shows its progress on the Weather tab.

**As built in W2:**

- The fetch loop's past job finds the hours from the start of the log (the UTC midnight before the first reading, day's counters or meter half-hour) to the start of today with no past row for the current panels. It asks for up to 31 days at a time, newest first, ten seconds apart, then waits for 03:00 UTC plus the run's minute and asks for the day just over.
- Precedence: a past row replaces a forecast row or an older past row; a forecast row replaces an older forecast row but never a past one. Each forecast fetch copies the hours that have begun.
- Hours the provider sent nothing for, or only empty values, aren't asked for again until the night. A 4xx on older days stops those and all before them until the night, as they're most likely before the provider's records begin; on the newest days it stops only those. Other errors back off as the forecast does.
- A change of panels counts the hours as missing, so they're fetched again for the new tilt and facings. A change of location isn't tracked: the rows keep the weather of the old place.
- Cost on Open-Meteo: a 31-day chunk is about 4 calls by its weighting, plus a smaller one for a second facing, so two years of log is about 120 calls, once.
- `GET /api/weather` has a `history` block: the log's start, the end, the hours and how many are filled. `GET /api/weather/history` serves up to 366 days at once.
- The CSV and database exports carry the table, and the importer brings it back with the same precedence.
- The Statistics overlays below came in W3b. The progress line is on the Weather tab's footer.

**In Statistics:** the history charts can show weather beside the readings. That means an optional row for irradiance and cloud under the solar chart, temperature beside the load, and the condition icon per day on the daily view. Each is off by default and is a display preference.

## W3: the Weather tab

A new tab after Dashboard: `<li><a href="#weather">Weather</a></li>` and `<section class="view" id="view-weather" hidden>`. It reads from `GET /api/weather`.

From the top:

1. **Warnings**, while any are in force or coming up, coloured by level, with time, type, headline and the issuer's link.
2. **Now.** Icon, temperature, feels like, wind and gusts, rain this hour, and when it was fetched.
3. **Next 48 hours, as charts**, sharing the time axis and drawing code of the Statistics charts:
   - Solar: expected PV (W5) per hour as bars, with actual PV over the hours already past. Irradiance until W5 has learned enough.
   - Cloud: total, split into low, mid and high.
   - Rain: probability, with mm.
   - Temperature and feels like.
4. **Next 48 hours, as a table**, one row per hour: time, icon, temperature, rain (mm and %), cloud, wind and gust, irradiance, expected PV. Night hours fold up.
5. **Next 7 days**, one column per day: icon, low and high, rain, sunshine hours, sunrise and sunset, expected PV.
6. **Yesterday's forecast against what happened** (W5), and the 30-day error.
7. **Automation would…** (W6).
8. **Footer**: each source's credit and licence link, the last good fetch of each kind, the backfill's progress, and a note if a fetch failed.

Fields the provider doesn't have are left out, not shown empty. With Yr, for example, there's no irradiance row.

**As built in W3:**

- The Weather tab is one page with no sub-tabs. Like the Dashboard it needs no permission, as the weather routes are open to every account.
- Warnings: level and type, headline, from and to, the description as plain text under "Details", and a "Source" link only when it's an http or https address.
- Now: four tiles: the condition and temperature with feels like, wind with gusts and the compass point it comes from, rain this hour with its chance and the cloud, and the sun on the panels (tilted, or flat ground without a facing) with sunrise and sunset. Under them, the provider and when the forecast was fetched.
- Charts: from 12 hours back (or the start of today, if that's earlier) to 48 hours ahead, with a dashed line at now. The left side draws the hours kept in `weather_hours`, the right side the forecast. Sun: on the panels and flat ground, with the solar made as bars underneath. Cloud: total, low, mid and high. Rain: the chance, with mm as bars. Temperature: the temperature and feels like. A chart with nothing to draw is left out, and the charts don't zoom. Expected PV waits for W5.
- Hour by hour: 48 rows under a heading for each day. A run of three or more night hours inside a day, other than the current hour, folds into one row, "Night 17:00–00:00 · 6° to 7° · 6.5 mm", that opens on a click. Columns with no values are left out.
- Next days: one row per day rather than one column, so it fits a phone. Up to 7 days from today; a later day with under 12 hours of forecast is left out. The weather is the commonest daytime condition. Sunshine is the hours with direct sun of at least 120 W/m², the WMO's measure.
- Footer: each source's credit and licence link, the last fetch of each kind with its outcome, and "Weather kept for X of the log's Y hours" while the log has any.
- Days, times and the day headings follow the time zone chosen in Preferences, as the rest of the page does. Temperature, wind and rain follow the unit preferences.
- The tab refreshes when opened and every 5 minutes while it's showing.

**As built in W3b, the Statistics overlays:**

- Three display preferences, all off at first, under Weather in Preferences: "Sun and cloud in Statistics", "Temperature in Statistics" and "Weather by day in Statistics".
- Sun and cloud: a chart under Solar, of the sun on the panels (flat ground when there's no facing) in W/m², with the cloud in % as a strip beneath it.
- Temperature: a chart under House, in the unit chosen in Preferences, its scale fitted to the values.
- Each reading's weather is the mean of the hours its interval covers, so the 1-hour, 1-day and longer intervals all line up. The tooltip and the table carry the same values, and the table's columns show only while their chart does. A chart with no weather kept for its stretch says so.
- Weather by day: a row of icons over the daily charts, each day's commonest daytime condition, the same measure as the Weather tab's next days. It's in the daily tooltip and table too. Days follow the monitor's own clock, as the daily totals do.
- The page asks `GET /api/weather/history` for at most 366 days at a time, and doesn't ask again for a stretch it fetched in the last 15 minutes.

## W4: the Dashboard

**Weather on the cards side.** A new "Weather" section above "Last 24 hours", in the same pane, so on a phone it falls in the same column. It shows:

- Any warning in force or due today: level colour, type and time, in one line linked to the Weather tab.
- Now: icon, temperature, wind.
- The next 6 hours as a strip: icon and rain chance per hour.
- Solar today: expected kWh with what's been made so far, and tomorrow's expected kWh. Sunshine hours until W5 has learned enough.
- "Weather →" to the tab, as "Statistics →" is beside "Last 24 hours".

With no forecast, the section shows "No forecast" and the age of the last one, at the same height, so the layout doesn't jump. Tooltips stay short, for example "Rain 60%, 0.4 mm", "Gust 14 m/s", "Open-Meteo, 14:12".

**Sections fold up.** Each section heading on the cards side ("Weather" and "Last 24 hours") becomes a button with a chevron: ▾ open, ▸ folded. Clicking it, or Enter or Space on it, folds or opens that section.

- The button carries `aria-expanded` and `aria-controls`. The "Weather →" and "Statistics →" links stay outside it, so they still just navigate.
- Folded state is remembered per device in browser storage, so the kiosk and a phone can differ. If storage isn't available, everything starts open.
- A folded section's charts stop redrawing until it's opened, so a phone does less work.
- Folding is written once and used by both sections, so the next section added to the cards side gets it for free.

**As built in W4:**

- The Weather section shows only once the location is set in Setup; without one it's hidden, and "Last 24 hours" is the first section.
- Warnings: those in force, or starting before the end of today, one line each with the level's colour on the left: the headline, then "from 15:00" or "until 18:00" (a date when it runs past today). Each links to the Weather tab.
- Now: the icon, temperature and wind with its compass point. Its tooltip has the condition, feels like, gust, and the provider with the fetch time.
- The next 6 hours: time, icon, temperature and rain chance, each tooltip "Rain 60%, 0.4 mm" and the wind.
- Under them, "Solar today 12.3 kWh" from the inverter's own count, live with each reading, and "Sunshine 3 h more today, 5 h tomorrow" (tomorrow once the forecast covers 12 of its hours), the same 120 W/m² measure as the Weather tab. Expected kWh replaces the sunshine hours in W5.
- With no forecast: "No forecast. The last is from 3 h ago." or "No forecast yet.", in a box the same height as the forecast.
- "History →" stays the link beside "Last 24 hours", as it was.
- Folded sections are kept in browser storage under `solar-monitor.folded`, a list of the folded sections' names (`weather`, `now`).
- The Dashboard asks for the weather on load, when going back to it, and every 5 minutes while it's showing, sharing the Weather tab's fetch.

## W5: expected solar output

The aim is expected PV per hour in kWh.

- **With the tilt and rating set**: expected PV = tilted irradiance ÷ 1000 W/m² × the panels' kWp × a learned performance ratio. The ratio absorbs shading, heat, wiring and inverter losses; a typical 0.75 to 0.85 is the starting guess until the log has enough to fit it.
- **Without them**: use GHI, with a factor learned for each time of day and time of year.
- **The ceiling** is the lower of the panels' rating and the most PV seen in the log.
- **Learning**: each night, pair each logged hour's mean `pv_w` with the irradiance for that hour from `weather_hours` and from the day-ahead forecast run, over the last 30 to 60 days, and fit the factors. With the W2 backfill, learning can start on the first day, from the whole log. The tab says "learning" until the fit is good enough.
- **Scoring**: each day, record expected against actual, by day and by hour, for the day-ahead run. The Weather tab shows yesterday's comparison and a 30-day error. Automation uses the error to decide how far to trust a forecast.
- If the GHI fit turns out as good as the tilted one, the panel settings stay optional.

This lives in `solar_forecast.py`, with tests on made-up logs.

**As built in W5:**

- One model for both cases: each hour's expected output is its sunshine times a factor in W per W/m². The sunshine is on the panels (`gti_wm2`) when the tilt is set, otherwise on flat ground (`ghi_wm2`). With a tilt only the hours kept for the same tilt and facings are used, so changing them starts the learning again.
- The factor is learned for each hour of the day (UTC), so panels facing two ways, or shaded at one end of the day, are learned as they are. Each hour's factor leans on the factor for all hours until it has data of its own, and that one leans on the rating × 0.8 when the rating is set. With neither the rating nor any usable data there's no fit, and the page says nothing about expected solar.
- Hours used: the last 60 days, where the readings span at least 45 minutes of the hour, the sun is at least 50 W/m², and the output is under 90% of the ceiling (above that it was most likely held down by it). The ceiling is the lower of the rating and the most the log has seen.
- The fit is redone at startup, each night, and when the tilt, facings or rating change. It's kept in `solar_fit`. It's "learning" until 14 days have at least 3 usable hours each, then "ready".
- Day-ahead: after each forecast is saved, the expected output for each hour of tomorrow is kept in `solar_expected`, so the last forecast before midnight is the one each day is scored against.
- Scoring: the last 30 whole days with a day-ahead figure for every hour and the inverter's daily count. The error is the sum of the misses over the sum made, per day and per hour; the bias is the same with the sign kept, positive when the forecast was high.
- `/api/weather` carries a `solar` block: the status and fit, `expected_w` on each forecast hour, the day-ahead figures for yesterday and today, yesterday's comparison and the 30-day score.
- Weather tab: a Solar chart (expected and made, W) above Sun, which loses its bars of solar made then. Hours gone show what was expected the day before, hours to come the latest forecast. A "Solar (kWh)" column in the hour by hour and next days tables (a day only when every hour has a figure). A "Solar forecast" pane after the next days: learning or learned, yesterday expected against made, the 30-day errors and bias, and the ratio and ceiling.
- Dashboard: once "ready", "Expected 4.1 kWh more today, 7.8 kWh tomorrow" replaces the sunshine hours; the hour under way counts for what's left of it, and once the day's sun is done only tomorrow is said. While learning, the sunshine hours stay.
- `solar_fit` and `solar_expected` aren't in the export: the fit is remade from the log, and the day-ahead figures only matter where they were made.
- Known limit: an hour's factor averages over the seasons it has seen, while the sun's place at that hour moves with them. Learning from the last 60 days keeps that small; a fit by sun position would remove it if the error shows it's needed.

## W6: automation stub

The shape of the automation, so the data, the screens and the log are ready before anything acts. It recommends and logs, and **never writes**.

- `automation.py` exposes `rules`, a registry of rules like the providers', and `evaluate(rule, forecast, state) -> Recommendation | None`.
  - `state` is what the app already reads: charge level, the floors and ceiling, the TOU periods, the capacity measured from the log, the typical load per hour from the log, and the forecast's scored error.
  - A `Recommendation` has the setting it concerns, the current value, the value it would choose, the reason in a few words, and the figures behind it.
- Each rule has its own settings, kept in the database, never in code.
- The decision time is a setting per rule, default an hour before the TOU period it concerns starts.
- Each evaluation goes in an `automation_runs` table (time, rule, inputs as JSON, recommendation as JSON), shown on the Weather tab under "Automation would…", labelled "Suggestions only".
- There is no on switch in this phase, and no path from `automation.py` to `control.py`. A test proves it.
- The first rule is the V2 one below, so its suggestions can be checked against what actually happened before it's ever allowed to act.

**As built in W6:**

- The one rule, `sunny_morning`, has five settings: the TOU period, the usual and sunny cut-offs (10 to 100%, the range `control.py` enforces), the margin (0 to 90%) and how many minutes before the period to decide (5 to 720: deciding at the start itself would be too late). They're ordinary settings, checked against the rule's bounds by the API and the import.
- Unset settings take a default from what's live: the first charge period that's on, that period's own cut-off as the usual one, the forecast's 30-day day error as the margin, and an hour's lead. The sunny cut-off has no default, so the rule says nothing useful until it's chosen.
- The morning is sunrise to midday (local) on the day the period ends, with part hours counted by their share. Without a sunrise it starts at the first forecast hour with sun. The load is the mean for each local hour over the last 28 days of the log.
- It decides in this order, and says why when it can't: the period is a charge period that's on, TOU is on, the period runs some day, the sunny cut-off is set and below the usual one, the period holds one of the two (otherwise "Changed by hand"), the capacity, the forecast and its error are known, and the forecast reaches the morning. A forecast still learning still decides, and the page says the figures are rough.
- A loop in the app checks each minute and logs one decision per rule for each start of its period, at its decision time, in `automation_runs`. The inputs are logged with it, without the periods' raw bytes.
- `/api/automation` gives each rule's settings (set value, value in use, and whether it's a default), its charge periods, what it would do now, when it next decides, and the last 14 decisions. Each decision, once its morning is over and the log covers at least half of it, has what the morning made and whether the decision was right: sunny when solar less load came to the room or more. `PUT /api/automation/{name}` changes the settings and goes in the Action history. Seeing it needs the schedule permission; changing it needs edit as well.
- Weather tab: an "Automation would…" pane after the solar forecast, shown when a location is set and the user may see the schedule. It has the suggestion and why, when it next decides, the settings (unset ones show their default greyed), and the decisions with how each morning turned out.
- `automation_runs` isn't in the CSV export; the SQLite copy has it.
- A test walks everything `automation.py` imports, however far down, and checks none of it is `control.py` or the collector.
- Older forecasts: after 14 days, only the last forecast fetched each day keeps its hours, so how far ahead the forecasts were right can still be looked at.

**As built, the dry runs** (collecting data through the winter, before V2 acts):

- Every decision logged is a dry run: what V2 would have written, never sent. A night the rule can't decide on is still logged with the morning's figures (its start and end, the forecast solar and typical load), so the forecast can be checked against every morning, not only the decided ones.
- A forecast that isn't in yet doesn't lose the night: without the morning's figures, the loop tries again each check until 3 minutes before the period starts, then logs why it couldn't decide.
- Only what can't be rebuilt is stored: the decision-time figures. What each morning made and used, and the battery's highest charge in it, come from the log when the page asks, and are kept in memory once the morning's over.
- Each morning is checked for the cut-off set and, as dry runs, for cut-offs 5, 10, 20 and 30 points below the usual one (5 is one a winter morning can fill). For each: did the forecast, less the margin, call it sunny; was it, from solar made less load; and so was the call right.
- A battery that reached its ceiling (the run's max charge, or 100%) may have had solar held back, so a morning that fell short of the room then is "can't tell", not wrong.
- `/api/automation` sums the dry runs over the last 400 decisions, more than a year, and shows the latest 14 in full. The page shows each decision as what V2 would write ("90% → 60%" or "Keep 90%"), the forecast and made solar, the spare, and whether it was right, with a Dry runs table beneath.

## V2: lower a TOU charge cut-off before a sunny morning

Once the weather has run for a while and the forecast's error is known. Example: TOU charges from the grid overnight to 90%. If tomorrow morning will be sunny, charging only to 60% leaves room for free solar instead of bought grid power.

**The rule's settings**, chosen by the user on the Weather tab or Battery tab:

- Which TOU period (one of the inverter's charge periods, read live).
- The usual cut-off, and the sunny cut-off. Both are checked against the range `control.py` already enforces.
- How much margin to keep, as a share of the forecast. The default comes from the forecast's own measured error, not a fixed number.

**Deciding:** the morning counts as sunny enough when expected PV, less the typical load, from sunrise to midday comes to more than the room the lower cut-off leaves (the capacity measured from the log × the difference between the two cut-offs), after the margin.

**Acting:**

- At the decision time, the rule works out which cut-off should apply tonight, and only writes if the inverter holds the other one. That means a rule that does nothing leaves the usual value in place, and a sunny night is put back the next night by the same rule.
- Every write goes through `control.py`'s existing path: re-read the period, refuse if it isn't exactly what's expected, log before sending, read back byte for byte, never retry by itself. Only the cut-off byte differs.
- If the period has been changed by hand, so it holds neither of the rule's two values, the rule stops and says so, and doesn't overwrite.
- It starts as a "Do it" button on the suggestion. Running it unattended is a second switch, off by default, with the user's approval for the first write, which is the smallest change (one night, one period).
- That switch is its own setting, never one of the rule's settings: the dry runs are set up with those, so choosing a sunny cut-off and margin for them must never be enough to cause a write.
- Every write is in the inverter write log and the Action history, with the forecast and figures that led to it.

## API

| Method | Path | What |
|---|---|---|
| GET | `/api/weather` | Warnings, the latest run (48 h by the hour, 7 days by the day), past 24 h of actual PV, fetch status, expected PV once learned |
| GET | `/api/weather/history?start=&end=` | `weather_hours` for a range, for the Statistics charts |
| GET | `/api/weather/accuracy` | Expected against actual, by day, for the last 30 days |
| GET | `/api/weather/providers` | The registry: names, titles, what each has, regions, options |
| GET | `/api/automation` | The rules, their settings, the latest suggestions and recent runs |

Settings go through the existing settings endpoints, extended for text values.

## Tests

- Each provider: parsing a saved response with made-up coordinates, units, the end-of-hour shift, mapping its codes to the app's conditions and levels, errors and 429.
- The core with a stub provider: no location, a provider without irradiance or past data, switching provider, pruning runs, filling `weather_hours` from past data and from forecasts, the backfill in chunks, and that a slow fetch doesn't hold up the collector.
- Warnings: filtering by region, levels, first and last seen.
- The solar estimate on made-up logs: GHI and tilted, too little data, the PV ceiling.
- Automation: the V2 rule's decision from fixed inputs, and that the stub can't reach `control.py`.
- The API: shapes, and permissions for the settings.
- The page: folding sections and remembering them, the Weather section with no forecast, and a warning line.

Fixtures use made-up coordinates, never the house's.

## Open questions

- ~~Whether the Open-Meteo past-forecast service has the same free terms as the forecast.~~ Its docs offer the same free non-commercial use as the forecast, and the terms cover the API as a whole, so the credit already shown covers it.
- Whether Met Éireann's warnings feed needs anything beyond the statements in its credit.
- How long the forecast needs to run before V2's suggestions are trusted enough to offer the "Do it" button. The dry runs are there to answer it: once a few sunny mornings are in, their Right counts say how often the call would have been right.

## Sources

- [MET Norway terms of service](https://api.met.no/doc/TermsOfService)
- [Locationforecast data model](https://docs.api.met.no/doc/locationforecast/datamodel)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
- [Open-Meteo historical forecast API](https://open-meteo.com/en/docs/historical-forecast-api)
- [Open-Meteo terms](https://open-meteo.com/en/terms)
- [Met Éireann forecast API](https://data.gov.ie/dataset/met-eireann-forecast-api)
- [Met Éireann weather warnings](https://data.gov.ie/dataset/weather-warnings)
