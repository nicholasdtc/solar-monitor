"""The schedule is decoded by the library's own classes, from bytes in the inverter's format."""

import asyncio
import json

from goodwe.protocol import ProtocolResponse
from goodwe.sensor import EcoModeV1, EcoModeV2

from solar_monitor.schedule import describe_period, read_schedule

ON_745, OFF_745 = 0xF9, 0x06  # The newer inverters' on and off values, which also mark the type.
EVERY_DAY, ALL_YEAR = 0x7F, 0x0FFF


def group(start, end, on_off, power, soc, days=EVERY_DAY, months=ALL_YEAR):
    data = bytes((*start, *end, on_off, days))
    data += power.to_bytes(2, "big", signed=True) + soc.to_bytes(2, "big") + months.to_bytes(2, "big")
    return EcoModeV2("eco_mode_1", 0, "test").read_value(ProtocolResponse(data, None))


def test_an_enabled_overnight_charge():
    # Newer inverters hold power in tenths of a percent.
    assert describe_period(1, group((22, 30), (6, 0), ON_745, -350, 85)) == {
        "slot": 1,
        "state": "on",
        "start": "22:30",
        "end": "06:00",
        "next_day": True,
        "action": "charge",
        "power_pct": 35,
        "cutoff_soc": 85,
        "days": "Every day",
        "months": "All year",
        "raw": "161e0600f97ffea200550fff",
    }


def test_a_disabled_discharge_on_the_same_day():
    period = describe_period(2, group((18, 0), (20, 15), OFF_745, 800, 25))
    assert (period["state"], period["action"], period["power_pct"]) == ("off", "discharge", 80)
    assert (period["start"], period["end"], period["next_day"]) == ("18:00", "20:15", False)
    assert period["cutoff_soc"] == 25


def test_the_older_type_holds_power_in_whole_percent():
    assert describe_period(1, group((1, 0), (2, 0), 0xFF, -30, 50))["power_pct"] == 30


def test_some_days_and_months():
    period = describe_period(1, group((1, 0), (2, 0), ON_745, -100, 50, days=0b0000011, months=0b11))
    assert period["days"] == "Sun, Mon"
    assert period["months"] == "Jan,Feb"


def test_a_never_used_slot_is_empty():
    assert describe_period(4, group((0, 0), (0, 0), 0, 0, 0, days=0, months=0))["state"] == "empty"


def test_a_cleared_slot_is_empty():
    assert describe_period(4, group((48, 0), (48, 0), 0, 100, 100, days=0, months=0))["state"] == "empty"


def test_a_slot_used_for_something_else_is_not_shown_as_tou():
    peak_shaving = 0x03
    assert describe_period(3, group((1, 0), (2, 0), peak_shaving, 100, 50))["state"] == "other"


def test_the_oldest_format_has_no_cut_off():
    data = bytes((1, 0, 2, 0)) + (-30).to_bytes(2, "big", signed=True) + bytes((0xFF, EVERY_DAY))
    old = EcoModeV1("eco_mode_1", 0, "test").read_value(ProtocolResponse(data, None))
    period = describe_period(1, old)
    assert (period["state"], period["power_pct"], period["cutoff_soc"]) == ("on", 30, None)


class ReadOnlyInverter:
    """Has no way to write, so reading the schedule can't do so by accident."""

    def __init__(self, **settings):
        self.settings = settings

    async def read_setting(self, name):
        if name not in self.settings:
            raise ValueError(name)
        return self.settings[name]


def test_reading_the_whole_schedule():
    inverter = ReadOnlyInverter(
        work_mode=0,
        eco_mode_enable=1,
        backup_mode_enable=0,
        smart_charging_enable=0,
        eco_mode_1=group((22, 30), (6, 0), ON_745, -350, 85),
        eco_mode_2=group((18, 0), (20, 15), OFF_745, 800, 25),
        eco_mode_3=group((13, 0), (14, 0), OFF_745, -100, 40),
        eco_mode_4=group((0, 0), (0, 0), 0, 0, 0, days=0, months=0),
    )
    schedule = asyncio.run(read_schedule(inverter))
    assert schedule["work_mode"] == "Self-consumption"
    assert (schedule["tou_on"], schedule["backup_on"], schedule["smart_charge_on"]) == (True, False, False)
    assert [p["state"] for p in schedule["periods"]] == ["on", "off", "off", "empty"]
    assert [p.get("start") for p in schedule["periods"]] == ["22:30", "18:00", "13:00", None]
    json.dumps(schedule)  # It goes to the page as it is.


def test_unreadable_settings_are_unknown_rather_than_a_failed_poll():
    schedule = asyncio.run(read_schedule(ReadOnlyInverter(work_mode=9)))
    assert schedule["work_mode"] == "Unknown (9)"
    assert schedule["tou_on"] is None
    assert [p["state"] for p in schedule["periods"]] == ["unreadable"] * 4
