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

All of these are in the "Solar panels and location" pane on the Appliances & tariffs tab. The provider choices list only the providers the registry has, and the region list follows the warnings provider. A region kept from another provider means all of them. All have the same permission as the other settings, and each change is recorded in the Action history (the location only as "changed").

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
    ...the Hour fields...
) WITHOUT ROWID;
```

**Pruning forecast runs.** Automation needs to know how good the evening-before forecast was, so old runs are scored, not just the latest:

- The latest run keeps all its days.
- Older runs keep only the 48 hours after they were fetched.
- After 14 days, one run per day is kept, the last before the day-ahead decision time (W6), and the rest are deleted.

That's about 1,200 rows a day for the last fortnight, then 48 a day, so a few MB a year.

The warnings and the text settings go into the CSV export and the importer like the other tables, and the location goes with the other settings. The forecast runs and the fetch log are only in the full database export, and the importer leaves them out: an old forecast is no use on another copy. `weather_hours` (W2) will go in both, as it's the weather as it happened.

## W2: weather for every hour, for the statistics

`weather_hours` holds the weather for every hour the app has a log for, whichever provider is in use:

- **With a provider that has past data** (Open-Meteo): once a night, fill yesterday from its past service. Where the log has hours that `weather_hours` doesn't, fill those too: that covers the first backfill back to the start of the log, and the Pi being offline.
- **With a provider that hasn't**: when an hour has passed, keep the latest forecast for it, the one made closest to it, with its source marked as a forecast. Nothing is lost if the provider is changed later: the past service can overwrite these rows.
- The backfill runs in small chunks (a month per request), well inside the provider's limits, and shows its progress on the Weather tab.

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

## W5: expected solar output

The aim is expected PV per hour in kWh.

- **With the tilt and rating set**: expected PV = tilted irradiance ÷ 1000 W/m² × the panels' kWp × a learned performance ratio. The ratio absorbs shading, heat, wiring and inverter losses; a typical 0.75 to 0.85 is the starting guess until the log has enough to fit it.
- **Without them**: use GHI, with a factor learned for each time of day and time of year.
- **The ceiling** is the lower of the panels' rating and the most PV seen in the log.
- **Learning**: each night, pair each logged hour's mean `pv_w` with the irradiance for that hour from `weather_hours` and from the day-ahead forecast run, over the last 30 to 60 days, and fit the factors. With the W2 backfill, learning can start on the first day, from the whole log. The tab says "learning" until the fit is good enough.
- **Scoring**: each day, record expected against actual, by day and by hour, for the day-ahead run. The Weather tab shows yesterday's comparison and a 30-day error. Automation uses the error to decide how far to trust a forecast.
- If the GHI fit turns out as good as the tilted one, the panel settings stay optional.

This lives in `solar_forecast.py`, with tests on made-up logs.

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

## V2: lower a TOU charge cut-off before a sunny morning

Once the weather has run for a while and the forecast's error is known. Example: TOU charges from the grid overnight to 90%. If tomorrow morning will be sunny, charging only to 60% leaves room for free solar instead of bought grid power.

**The rule's settings**, chosen by the user on the Weather tab or Battery schedule tab:

- Which TOU period (one of the inverter's charge periods, read live).
- The usual cut-off, and the sunny cut-off. Both are checked against the range `control.py` already enforces.
- How much margin to keep, as a share of the forecast. The default comes from the forecast's own measured error, not a fixed number.

**Deciding:** the morning counts as sunny enough when expected PV, less the typical load, from sunrise to midday comes to more than the room the lower cut-off leaves (the capacity measured from the log × the difference between the two cut-offs), after the margin.

**Acting:**

- At the decision time, the rule works out which cut-off should apply tonight, and only writes if the inverter holds the other one. That means a rule that does nothing leaves the usual value in place, and a sunny night is put back the next night by the same rule.
- Every write goes through `control.py`'s existing path: re-read the period, refuse if it isn't exactly what's expected, log before sending, read back byte for byte, never retry by itself. Only the cut-off byte differs.
- If the period has been changed by hand, so it holds neither of the rule's two values, the rule stops and says so, and doesn't overwrite.
- It starts as a "Do it" button on the suggestion. Running it unattended is a second switch, off by default, with the user's approval for the first write, which is the smallest change (one night, one period).
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

- Whether the Open-Meteo past-forecast service has the same free terms as the forecast. Its pages don't say; check before the backfill runs.
- Whether Met Éireann's warnings feed needs anything beyond the statements in its credit.
- How long the forecast needs to run before V2's suggestions are trusted enough to offer the "Do it" button.

## Sources

- [MET Norway terms of service](https://api.met.no/doc/TermsOfService)
- [Locationforecast data model](https://docs.api.met.no/doc/locationforecast/datamodel)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
- [Open-Meteo historical forecast API](https://open-meteo.com/en/docs/historical-forecast-api)
- [Open-Meteo terms](https://open-meteo.com/en/terms)
- [Met Éireann forecast API](https://data.gov.ie/dataset/met-eireann-forecast-api)
- [Met Éireann weather warnings](https://data.gov.ie/dataset/weather-warnings)
