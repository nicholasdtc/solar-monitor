# Weather: forecast, dashboard widget, Weather tab, automation stub

Phase 7 of [spec.md](spec.md). It adds a weather forecast to the app: a small widget on the Dashboard, a Weather tab with the hourly detail, and the groundwork for the next feature, battery automation driven by the forecast.

Nothing in this phase writes to the inverter.

## Goals

| # | Goal | Step |
|---|------|------|
| 1 | Fetch a free forecast for the house's location on the Pi, and keep it in SQLite | W1 |
| 2 | Weather tab: the next 48 hours by the hour, and 7 days by the day | W2 |
| 3 | Dashboard widget: now, the next few hours, and today's and tomorrow's sun | W3 |
| 4 | Expected solar output (kWh) per hour, learned from the log, and how the forecast compared with what happened | W4 |
| 5 | A stub for automation: works out what it would do, shows it and logs it, and never acts | W5 |

## Choosing the source

| | Yr (MET Norway Locationforecast 2.0) | Open-Meteo | Met Éireann |
|---|---|---|---|
| Covers Ireland | Yes | Yes, with the UK Met Office's 2 km model in its "best match" | Yes |
| Cost and key | Free, no key | Free for non-commercial use, which covers personal home automation. No key | Free, no key |
| Solar radiation | **None.** Cloud cover (total, low, medium, high) and a clear-sky UV index only | **Yes**: global (GHI), direct, diffuse, and tilted (GTI) irradiance | No |
| Time step | Hourly for the short range, then 6-hourly, out to 9-10 days. Where the hourly part ends outside the Nordics isn't confirmed | Hourly, 7 days by default, up to 16 | Hourly to 90 h, then 3- and 6-hourly to 10 days |
| Format | JSON | JSON | XML |
| Terms | CC BY 4.0. A User-Agent naming the app and a contact. At most 4 decimals in coordinates. Cache, and don't ask again before `Expires`. "Yr" can't be in the app's name | CC BY 4.0. Under 10,000 calls a day | Licence based on CC BY 4.0, with extra conditions such as showing weather warnings alongside |

**Does Yr work?** Yes, it covers Ireland and its terms suit this app. But it has no solar radiation, and the automation that follows this phase is really a question of how much the panels will make tomorrow. Cloud cover is a poor stand-in for that.

**Decision: Open-Meteo, as the only source.** It gives the weather for the widget and tab and the irradiance for the solar estimate from one request. One fetcher and one table. Yr stays the fallback if Open-Meteo's terms or service change: the stored fields below are ones both provide, apart from irradiance.

## Hard rules

- **The location is personal data.** Latitude and longitude identify the house. They are entered on the Settings screen and kept in the `settings` table, never in code, git, tests, specs or logs. They're rounded to 2 decimals (about 1 km) before they're saved, which is ample for a forecast and also below MET Norway's 4-decimal limit. No place name or address is stored or shown.
- **Fetched by the server, never by the browser.** The Pi fetches and caches, and the page reads only `/api/weather`. So the location never leaves the Pi from viewers' devices, the kiosk keeps working without internet, and only one copy asks.
- **Never in the collector's way.** Fetches run on their own thread with a short timeout. A slow or failed fetch can't delay a poll or a flush.
- **Works offline.** Weather icons are bundled with the page (like the fonts), never loaded from the source. With no forecast the widget says so and gives the age of the last good one. Nothing else on the page depends on the forecast.
- **No hardware specs.** No panel size, tilt, azimuth or inverter rating is entered (as in spec.md phase 3, hardware specs that labels would give aren't stored). The solar estimate is learned from the log (W4).
- **Polite fetching.** Once an hour, at a random minute picked at start-up, one request each time. Back off on errors, and stop until the next hour on HTTP 429.
- **No writes to the inverter in this phase.** The automation stub only recommends. See W5.
- **Credit the source.** The Weather tab and the widget's tooltip name the source and link the licence, as CC BY 4.0 requires.

## W1: fetch and store

### Request

One call to `https://api.open-meteo.com/v1/forecast` with:

- `latitude`, `longitude` from settings (2 decimals).
- `hourly=temperature_2m,apparent_temperature,precipitation,precipitation_probability,weather_code,cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,wind_speed_10m,wind_gusts_10m,wind_direction_10m,shortwave_radiation,direct_radiation,diffuse_radiation,is_day`
- `daily=sunrise,sunset,daylight_duration,sunshine_duration`
- `forecast_days=7`, `timeformat=unixtime`, `timezone=GMT`, `wind_speed_unit=ms`.
- A User-Agent naming the app and its public repo, `solar-monitor (github.com/nicholasdtc/solar-monitor)`. No email address.

All times are kept in UTC, as the rest of the app does, and the page shows them in local time.

**Hour labels.** Open-Meteo labels radiation and precipitation by the end of the hour they cover (the mean or sum over the preceding hour), while temperature, cloud and wind are the value at that instant. Like the ESB meter file, the store keeps each value against the start of the hour it covers, so radiation and precipitation move back one hour on the way in. Check this against the docs and a sunny day's log before relying on it.

### Module

`weather.py` holds the fetcher and the parsing. It takes an injectable `fetch` callable, so tests never touch the network, and returns parsed hours or an error. A small loop in `main.py`, beside the collector, calls it hourly and saves the result. Without a saved location it doesn't fetch, and the tab asks for one.

### Tables

```sql
CREATE TABLE weather_fetches (
    id          INTEGER PRIMARY KEY,
    fetched_at  INTEGER NOT NULL,      -- UTC seconds
    source      TEXT NOT NULL,         -- 'open-meteo'
    outcome     TEXT NOT NULL,         -- 'ok', or a short error
    hours       INTEGER NOT NULL       -- how many hours it returned
);

CREATE TABLE forecast_hours (
    fetch_id     INTEGER NOT NULL REFERENCES weather_fetches(id),
    hour_start   INTEGER NOT NULL,     -- UTC seconds, start of the hour covered
    temp_c       REAL, feels_c REAL,
    precip_mm    REAL, precip_pct REAL,
    weather_code INTEGER,              -- WMO code
    cloud_pct    REAL, cloud_low_pct REAL, cloud_mid_pct REAL, cloud_high_pct REAL,
    wind_ms      REAL, gust_ms REAL, wind_dir REAL,
    ghi_wm2      REAL, direct_wm2 REAL, diffuse_wm2 REAL,
    is_day       INTEGER,
    PRIMARY KEY (fetch_id, hour_start)
) WITHOUT ROWID;
```

Daily sunrise, sunset and sunshine are small enough to keep as JSON on the fetch row, or in a third table if the tab needs them by day.

**Every run is kept, not just the latest.** Automation needs to know how good a forecast made the evening before was, and that means scoring old runs against what the panels did. To keep this small:

- The latest run keeps all 7 days.
- Older runs keep only the 48 hours after they were fetched.
- After 14 days, one run per day is kept (the last before 18:00 local, the "day-ahead" run) and the rest are deleted.

That is about 1,200 rows a day for the last fortnight, then 48 a day, so a few MB a year. It belongs with the open question in spec.md on retention.

### Export and import

Forecast tables are added to `/api/export` and the importer like the other tables, so a restore keeps the forecast history. The location goes with the other settings.

## W2: the Weather tab

A new tab after Dashboard: `<li><a href="#weather">Weather</a></li>` in `#tab-links` and a `<section class="view" id="view-weather" hidden>`. All of it comes from one `GET /api/weather`, which returns the latest run, the past 24 hours of logged solar and the fetch status.

From the top:

1. **Now.** Icon, temperature, feels like, wind and gusts, rain this hour, and when it was fetched.
2. **Next 48 hours, as charts**, sharing the time axis and drawing code of the Statistics charts:
   - Solar: expected PV (W4) per hour as bars, with the actual PV from the log over the hours already past. Until W4 exists, irradiance (W/m²) instead.
   - Cloud: total cover, split into low, mid and high.
   - Rain: probability, with mm.
   - Temperature and feels like.
3. **Next 48 hours, as a table**, one row per hour: time, icon, temperature, rain (mm and %), cloud %, wind and gust, irradiance, expected PV. Night hours fold up.
4. **Next 7 days**, one column per day: icon, low and high, rain, sunshine hours, sunrise and sunset, expected PV in kWh.
5. **Yesterday's forecast against what happened** (W4): expected and actual kWh by hour.
6. **Footer**: source, licence link, last good fetch, and a note if the latest fetch failed.

The location is set on the Settings screen, with the same permission that edits the other settings, and the change goes in the Action history like other settings.

## W3: the Dashboard widget

A compact pane beside "Last 24 hours". It shows:

- Now: icon, temperature, wind.
- The next 6 hours as a strip: icon and rain chance per hour.
- Solar today: expected kWh with what's been made so far, and tomorrow's expected kWh. Until W4 has learned enough, sunshine hours instead.
- A "Weather →" link to the tab.

Tooltips stay short, per CLAUDE.md, for example "Rain 60%, 0.4 mm", "Gust 14 m/s", "Open-Meteo, 14:12". With no forecast the pane shows "No forecast" and the age of the last one, at the same size, so the layout doesn't jump. It must still fit the kiosk layout.

## W4: expected solar output, learned from the log

The aim is expected PV per hour in kWh, without asking for any panel data.

- **The model.** Expected PV for an hour = forecast GHI × a factor learned for that time of day and time of year. Shading, the panels' angle and the inverter's ceiling are all folded into the factor. The ceiling is the most PV seen in the log, never a rating.
- **Learning.** Each night, pair each logged hour's mean `pv_w` with the GHI forecast for that hour from the day-ahead run, over the last 30 to 60 days, and fit the factors. The page shows "learning" until there are about 14 days.
- **A head start.** Open-Meteo also serves past forecasts for past days. If its terms allow, a one-off backfill of GHI for the days already in the log lets learning start straight away. To check.
- **If GHI isn't good enough.** Tilted irradiance (GTI) needs the panels' tilt and facing. Those would be fitted from the log, by trying angles against clear days, not typed in. Only if the GHI fit proves poor.
- **Scoring.** Every day, record expected against actual kWh, by day and by hour. The Weather tab shows the last day and a 30-day error figure, so it's clear how far automation could trust the forecast.

This lives in `planning.py` (or a new `solar_forecast.py` if it grows), with tests on made-up logs.

## W5: automation stub

Only the shape of the next feature, so the data and the screens are in place. It recommends and logs, and **never writes**.

- `automation.py` exposes `recommend(forecast, state) -> list[Recommendation]`. `state` is what the app already reads: charge level, the minimum and reserve settings, the TOU programmes, tariffs, and the typical load per hour from the log. A `Recommendation` has the setting it concerns, the current value, the value it would choose, the reason, and the figures behind it.
- The first rule, used to prove the plumbing: from tomorrow's expected PV and the typical load, the overnight grid charge level that would leave the battery full by mid-afternoon without buying more than needed. It's worked out at 21:00 each day.
- Each run goes in an `automation_runs` table (time, inputs as JSON, recommendations as JSON), shown on the Weather tab as "Automation would…", with an "Off: suggestions only" label. There is no switch to turn it on in this phase.
- No code path from `automation.py` to `control.py`, and a test that proves it.

**When automation does act (the next phase, not this one):** every write goes through `control.py`'s existing path: re-read the current value, refuse if it isn't what was expected, log before sending, read back byte for byte afterwards, never retry by itself. The first automated write is the smallest harmless change, approved by the user for that write, and it starts as a "do it" button on a suggestion before anything runs unattended. The forecast's scored accuracy (W4) decides how far it may move a setting.

## API

| Method | Path | What |
|---|---|---|
| GET | `/api/weather` | Latest run (48 h by the hour and 7 days by the day), past 24 h of actual PV, fetch status, expected PV once learned |
| GET | `/api/weather/accuracy` | Expected against actual, by day, for the last 30 days (W4) |
| GET | `/api/automation` | The latest suggestions and recent runs (W5) |

The location goes through the existing `PUT /api/settings`.

## Tests

- `tests/test_weather.py`: parsing a saved Open-Meteo response (with made-up coordinates), the one-hour shift for radiation and rain, errors, 429, no location, pruning old runs, and that a slow fetch doesn't hold up the collector.
- The solar estimate on made-up logs: learning, too little data, the PV ceiling.
- The automation stub: its recommendation from fixed inputs, and that it can't reach `control.py`.
- The API: shapes, and permissions for the location.

Test fixtures use made-up coordinates, never the house's.

## Open questions

- Where the widget sits on a phone, where the dashboard is already one column.
- Whether to show Met Éireann weather warnings. They'd be a second source, so only if wanted.
- Whether Open-Meteo's past-forecast service allows the backfill in W4.
- Whether GHI alone gives a good enough fit, or the panels' angle needs fitting.
- Which rule automation should start with once it can act: the overnight charge level, the discharge floor before a sunny day, or holding charge before a storm.

## Sources

- [MET Norway terms of service](https://api.met.no/doc/TermsOfService)
- [Locationforecast data model](https://docs.api.met.no/doc/locationforecast/datamodel)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
- [Open-Meteo terms](https://open-meteo.com/en/terms)
- [Met Éireann forecast API](https://data.gov.ie/dataset/met-eireann-forecast-api)
