# SEMS+ against this dashboard: why the wattage can differ

On the evening of 6 November 2026, SEMS+ showed the house and battery at 0.379 kW while this dashboard showed 2,270 W and 2,334 W. Minutes later the two agreed. This document records what I measured to explain that.

**How this was gathered.** I watched the SEMS+ station page in a signed-in browser from about 20:23 to 20:37. A script in that tab recorded Load, Battery, charge level and Update Time every half second, keeping each change. Alongside it I read this app's `/api/latest` and its database, and read SEMS+'s per-minute chart by hovering over it. It was look-only: nothing was written to the inverter or changed in SEMS+.

**How to read the numbers.** Times are local. SEMS+ figures are in kW and ours in W, as each app shows them. It was after dark, so solar was 0 throughout and none of this tests the solar figures. Anything I worked out rather than read directly is marked *(inference)*.

## Short answer

- **SEMS+ is usually close to live.** Its live feed ran about 1 to 5 seconds behind, and it showed one load jump before our 15-second poll did.
- **It sometimes shows old readings.** It can repaint a stored reading roughly 1.5 to 6 minutes old until the live feed takes over. I saw this on page load and around leaving or returning to the tab.
- **The 0.379 kW was most likely one of those.** Our figures were correct for that moment.
- **Even when current, SEMS+ load reads about 2 to 3% above ours,** because it is calculated from the battery's volts × amps rather than measured.

## The difference that started this

The house load jumped at about 20:18:30. From our log:

| Time | House load (`load_ptotal`) | Battery volts × amps |
|---|---|---|
| 20:17:52 | 350 W | 379.3 V × 1.0 A = 379 W |
| 20:18:07 | 335 W | 379.6 V × 1.0 A = 380 W |
| 20:18:22 | 405 W | 380.0 V × 1.2 A = 456 W |
| 20:18:38 | 2,258 W | 382.3 V × 6.1 A = 2,332 W |
| 20:20:41 | 2,270 W | 383.5 V × 6.2 A = 2,378 W |

**Our screenshot** matches the 20:20:41 reading exactly: load 2,270 W, `pbattery1` 2,334 W, grid 21 W.

**The SEMS+ screenshot** fits the moment before the jump:

- 0.379 kW is the 379 V × 1.0 A from before the jump. Once the load jumped, battery current stayed at about 6 A or more until the battery stopped at 20:28:48, so no reading in between fits.
- The SEMS+ chart has 0.37 kW stamped 20:19 and 2.31 kW stamped 20:20. So the 20:19 record is the last one from before the jump.
- The chart cuts figures to two decimals (4.448 shows as 4.44), so 0.379 would appear as 0.37 *(inference)*.

**What I can't prove.** The SEMS+ screenshot shows no Update Time, so I don't know exactly when it was taken, and the chart only shows 0.37 rather than 0.379. Nothing else I found fits, though.

## How SEMS+ gets its numbers

The flow diagram has two sources, and it shows whichever arrived last *(inference)*:

- **A live feed.** Update Time moved about every 5 seconds. I saw no repeating data request, so this is probably a streaming connection *(inference)*.
- **Stored per-minute records.** These carry an Update Time on a whole minute. Each is stamped about a minute after the moment it describes *(inference)*: the 20:25 record holds the battery's 5.08 kW from 20:24.

The figures themselves are not the ones we display:

- **Battery is volts × amps, not `pbattery1`.** At 20:24:23 SEMS+ showed 5.085 kW, which is our 20:24:18 reading of 376.7 V × 13.5 A = 5,085 W, while `pbattery1` read 4,332 W. Current has 0.1 A resolution, so this figure moves in steps of about 38 W.
- **Load is calculated, as battery plus grid plus solar** *(inference, with strong evidence)*. The 20:25 record shows load 6.50 from battery 5.08 and grid −1.41. At 20:24:14 SEMS+ showed load and battery both at 5.095 kW, with the grid flow not yet added. Small grid flows display as 0.

## How far behind the live feed is

Update Time was a median of 1 second behind the clock. A large load step at about 20:24 gave a direct test:

| Time | SEMS+ | This dashboard |
|---|---|---|
| 20:24:02 | | Load 2,247 W, battery 380.7 V × 6.0 A = 2,284 W |
| 20:24:14 | Battery 5.095 kW | |
| 20:24:18 | | Load 6,967 W, battery 376.7 V × 13.5 A = 5,085 W |
| 20:24:23 | Battery 5.085 kW | |
| 20:24:34 | | Load 7,018 W, battery 376.6 V × 13.5 A = 5,084 W |
| 20:24:35 | Battery 5.084 kW | |

SEMS+ showed the step at least 4 seconds before our next poll, because we only read every 15 seconds. It then showed the same volts × amps values 1 to 5 seconds after we read them.

## When SEMS+ shows old readings

| Seen at | Update Time shown | Age | SEMS+ load | Our reading | What was happening |
|---|---|---|---|---|---|
| 20:25:23 | 20:22:00 | 3 min 23 s | 2.339 kW, 24% | 4,342 W, 22% | Not recorded |
| 20:28:20 | 20:22:00 | 6 min 20 s | 2.339 kW, 24% | 4,379 W, 21% | Not recorded |
| 20:28:24 | 20:27:00 | 1 min 24 s | 4.26 kW, 22% | 4,367 W, 20% | Straight after the row above |
| 20:29:51 | 20:28:00 | 1 min 51 s | 4.448 kW, 21% | 2,523 W, 20% | Page reload at 20:29:50, live again at 20:30:00 |
| 20:30:22 | 20:28:00 | 2 min 22 s | 4.448 kW, 21% | About 2,450 W, 20% | Tab visible, lasted about 6 s. I was working in the page around then |
| 20:33:25 | 20:31:00 | 2 min 25 s | 0.517 kW, 20% | 506 W, 20% | About 6 s after the tab was hidden at 20:33:19 |
| 20:35:23 | 20:31:00 | 4 min 23 s | 0.545 kW, 20% | 512 W, 20% | Tab hidden |
| 20:37:06 | 20:31:00, then 20:35:00 | 6 min 6 s, then 2 min 6 s | 0.524 kW, 20% | 533 W, 20% | 2 s after the tab returned at 20:37:04. Still 20:35:00 at 20:37:21 |

The reload is the clearest case. SEMS+ showed the battery supplying 4.448 kW at 21%, when the battery had already stopped at 20% and the grid was supplying 2,489 W.

An old reading only looks wrong if the power changed since it was stored. In the last three rows the load was steady, so the numbers looked fine even though they were old.

In three stretches with the tab visible and left alone, about 4.5 minutes in total, this didn't happen at all (20:23:08 to 20:24:55, 20:30:28 to 20:31:45 and 20:31:47 to 20:33:19).

## How to spot an old reading

Compare **Update Time** above the SEMS+ diagram with the clock. If it's a minute or more behind, usually ending in `:00`, the numbers are old. A time ending in `:00` isn't enough on its own, because live readings can land on it by chance.

To compare the two apps over a period, use the energy counters rather than any live figure.

## The small gap that remains

With little grid flow, battery volts × amps is what SEMS+ shows as load. Against our measured `load_ptotal`:

| Period | Load | Samples | Median above ours | Range |
|---|---|---|---|---|
| 20:19 to 20:24 | About 2.3 kW | 19 | 2.6% | 1.6 to 5.1% |
| 20:25 to 20:29:30 | About 4.3 kW | 12 | 1.8% | 0.9 to 2.7% |

Volts × amps is measured on the battery's DC side and `load_ptotal` on the AC side, so the gap is about what conversion losses would give *(inference)*. At low power the 38 W step in the SEMS+ figure matters more: it showed 379 W when we measured 350 W.

## The grid sign is confirmed

**A positive `active_power_total` means exporting, and a negative one means importing.** The dashboard already assumed this from the `goodwe` library's convention, but no large flow had tested it until this evening.

| Time | Solar | Battery current | Inverter output | House load | Grid reading |
|---|---|---|---|---|---|
| 20:24:18 | 0 W | 13.5 A | 4,999 W | 6,967 W | −1,988 W |
| 20:28:48 | 0 W | 0.0 A | −15 W | 4,315 W | −4,324 W |
| 20:29:50 | 0 W | 0.0 A | −15 W | 2,523 W | −2,489 W |

In the last two rows there was no solar and the battery had stopped, at 20%, so only the grid could supply the house. The reading was negative and about the size of the load. In the first row the inverter was at about 5,000 W, short of the load by 1,968 W, and the reading matches that shortfall.

The meter counters agree. Between 20:28 and 20:43, `meter_e_total_imp` rose from 67.2 to 67.4 kWh while the grid reading adds up to −0.25 kWh, and `meter_e_total_exp` stayed at 7.0 kWh.

Two limits on this evidence:

- **`grid_in_out_label` proves nothing here.** "Importing" comes from the `goodwe` library, which works it out from the sign of another reading (`active_power`, with a 90 W dead band). So it only repeats the convention.
- **No sustained export has been seen.** The log has two readings above +500 W: +1,117 W at 14:55:51 and +534 W at 19:03:54, each with inverter output about 650 W above load. The sign fits, but both were brief, and the meter read 0 at those moments.

## What this doesn't settle

- **The trigger.** I don't know exactly what makes SEMS+ repaint an old reading. Two of the cases above have no record of what was happening, and how long they last while the tab is hidden is unknown, because the browser slows the sampling script in a hidden tab.
- **The phone app.** I only tested the website. If the original screenshot came from the phone app, it may behave differently.
- **How typical this is.** All of this comes from about 15 minutes on one evening, after dark.
