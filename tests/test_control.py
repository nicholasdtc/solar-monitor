"""Writes to the inverter: what gets sent, what stops it being sent, and what the read-back reports.

Bytes here are the inverter's own format, as read live from a GW5K-EHA-G20.
"""

import asyncio

import pytest
from goodwe.protocol import ProtocolResponse
from goodwe.sensor import EcoModeV1, EcoModeV2

from solar_monitor import control, db
from solar_monitor.schedule import period_bytes

CHARGE_40 = bytes.fromhex("17010500f97ffe70005a0fff")  # 23:01-05:00, on, every day, charge 40%, cut-off 90%, all year.
CHARGE_30 = bytes.fromhex("17010500f97ffed4005a0fff")
DISCHARGE_100 = bytes.fromhex("103b1301067f03e8000f0fff")  # The same type, switched off, discharging.
WHOLE_PERCENT = bytes.fromhex("17010500ff7fffd8005a0fff")  # The older type: on is 0xff, and power is -40, not -400.
EMPTY = bytes(12)
PEAK_SHAVING = bytes.fromhex("17010500037f00640032" + "0fff")
NO_POWER = bytes.fromhex("17010500f97f0000005a0fff")


def decoded(data):
    return EcoModeV2("eco_mode_1", 0, "test").read_value(ProtocolResponse(data, None))


class FakeInverter:
    """Holds one period's bytes, and hands back a reused object as the library does."""

    def __init__(self, held=CHARGE_40):
        self.held = held
        self.writes = []
        self.write_error = None
        self.applies = True
        self.applied_as = None  # Something other than what was sent.
        self.reads_until_applied = 0
        self.pending = None
        self.unreadable_after_write = False
        self.group = EcoModeV2("eco_mode_1", 0, "test")

    async def read_setting(self, name):
        if self.unreadable_after_write and self.writes:
            raise OSError("no reply")
        if self.pending is not None:
            if self.reads_until_applied == 0:
                self.held, self.pending = self.pending, None
            else:
                self.reads_until_applied -= 1
        return self.group.read_value(ProtocolResponse(self.held, None))

    async def write_setting(self, name, value):
        self.writes.append((name, value))
        if self.applies:
            self.pending = self.applied_as or value
        if self.write_error:
            raise self.write_error


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(control, "READ_BACK_WAIT_SECONDS", 0)


def change(inverter, conn, percent=30, expected=CHARGE_40.hex(), slot=1):
    return asyncio.run(control.set_period_power(inverter, conn, slot, percent, expected))


def logged(conn):
    return conn.execute("SELECT slot, old_percent, new_percent, old_bytes, new_bytes, outcome, held_bytes FROM inverter_writes").fetchall()


@pytest.mark.parametrize("data", [CHARGE_40, DISCHARGE_100, WHOLE_PERCENT, bytes.fromhex("0f00101e067fff38003c0fff")])
def test_a_period_rebuilds_to_the_same_bytes(data):
    assert period_bytes(decoded(data)) == data


def test_only_the_power_bytes_change_on_a_real_period(conn):
    inverter = FakeInverter()
    assert change(inverter, conn) == {"outcome": "confirmed", "old_percent": 40, "new_percent": 30}
    assert inverter.writes == [("eco_mode_1", CHARGE_30)]
    assert inverter.held == CHARGE_30


def test_the_setting_written_is_the_slot_asked_for(conn):
    inverter = FakeInverter()
    change(inverter, conn, slot=3)
    assert inverter.writes[0][0] == "eco_mode_3"


def test_a_discharge_stays_a_discharge(conn):
    inverter = FakeInverter(DISCHARGE_100)
    change(inverter, conn, percent=50, expected=DISCHARGE_100.hex(), slot=2)
    assert inverter.writes[0][1] == bytes.fromhex("103b1301067f01f4000f0fff")


def test_a_period_of_the_older_type_gets_whole_percent(conn):
    inverter = FakeInverter(WHOLE_PERCENT)
    change(inverter, conn, expected=WHOLE_PERCENT.hex())
    sent = inverter.writes[0][1]
    assert sent == bytes.fromhex("17010500ff7fffe2005a0fff")  # -30, where the newer type would hold -300.
    assert decoded(sent).get_power() == -30


@pytest.mark.parametrize("percent", [0, -30, 101, 1000])
def test_a_power_out_of_range_is_refused(conn, percent):
    inverter = FakeInverter()
    with pytest.raises(control.Refused):
        change(inverter, conn, percent=percent)
    assert inverter.writes == []


def test_a_period_that_changed_since_the_page_read_it_is_refused(conn):
    inverter = FakeInverter(CHARGE_30)  # SEMS+ changed it, say.
    with pytest.raises(control.Refused, match="changed on the inverter"):
        change(inverter, conn, percent=20, expected=CHARGE_40.hex())
    assert inverter.writes == []
    assert logged(conn) == []


def test_zero_power_is_refused_because_it_has_no_direction(conn):
    inverter = FakeInverter(NO_POWER)
    with pytest.raises(control.Refused, match="charges or discharges"):
        change(inverter, conn, expected=NO_POWER.hex())
    assert inverter.writes == []


@pytest.mark.parametrize("data", [EMPTY, PEAK_SHAVING])
def test_anything_but_a_tou_period_is_refused(conn, data):
    inverter = FakeInverter(data)
    with pytest.raises(control.Refused, match="can be changed"):
        change(inverter, conn, expected=data.hex())
    assert inverter.writes == []


def test_the_older_format_is_refused(conn):
    class OldFormat(FakeInverter):
        async def read_setting(self, name):
            data = bytes((1, 0, 2, 0)) + (-30).to_bytes(2, "big", signed=True) + bytes((0xFF, 0x7F))
            return EcoModeV1("eco_mode_1", 0, "test").read_value(ProtocolResponse(data, None))

    inverter = OldFormat()
    with pytest.raises(control.Refused, match="can be changed"):
        change(inverter, conn)
    assert inverter.writes == []


def test_an_unreadable_period_is_refused(conn):
    class Unreadable(FakeInverter):
        async def read_setting(self, name):
            raise OSError("no reply")

    inverter = Unreadable()
    with pytest.raises(control.Refused, match="Couldn't read"):
        change(inverter, conn)
    assert inverter.writes == []


def test_asking_for_what_it_already_holds_sends_nothing(conn):
    inverter = FakeInverter()
    assert change(inverter, conn, percent=40)["outcome"] == "unchanged"
    assert inverter.writes == []
    assert logged(conn) == []


def test_new_bytes_that_fail_the_double_check_are_not_sent(conn, monkeypatch):
    monkeypatch.setattr(control, "_with_power", lambda group, percent: bytes.fromhex("18010500f97ffed4005a0fff"))
    inverter = FakeInverter()
    with pytest.raises(control.Refused, match="didn't check out"):
        change(inverter, conn)
    assert inverter.writes == []


def test_the_double_check_refuses_a_flipped_direction(conn, monkeypatch):
    monkeypatch.setattr(control, "_with_power", lambda group, percent: bytes.fromhex("17010500f97f012c005a0fff"))
    inverter = FakeInverter()
    with pytest.raises(control.Refused, match="didn't check out"):
        change(inverter, conn)
    assert inverter.writes == []


def test_a_confirmed_write_is_logged_with_the_old_value_for_undo(conn):
    change(FakeInverter(), conn)
    assert logged(conn) == [(1, 40, 30, CHARGE_40.hex(), CHARGE_30.hex(), "confirmed", CHARGE_30.hex())]


def test_it_is_on_record_before_it_is_sent(conn):
    seen = []

    class Recording(FakeInverter):
        async def write_setting(self, name, value):
            seen.extend(logged(conn))
            await super().write_setting(name, value)

    change(Recording(), conn)
    assert seen[0][5] == "sent"


def test_a_change_that_shows_up_late_is_still_confirmed(conn):
    inverter = FakeInverter()
    inverter.reads_until_applied = control.READ_BACK_TRIES - 1
    assert change(inverter, conn)["outcome"] == "confirmed"
    assert logged(conn)[0][5] == "confirmed"


def test_a_lost_reply_is_fine_if_the_read_back_shows_it_arrived(conn):
    inverter = FakeInverter()
    inverter.write_error = OSError("no reply")
    assert change(inverter, conn)["outcome"] == "confirmed"
    assert len(inverter.writes) == 1


def test_a_rejected_write_is_reported_and_not_retried(conn):
    inverter = FakeInverter()
    inverter.applies = False
    inverter.write_error = OSError("rejected")
    with pytest.raises(control.Unconfirmed, match="still holds the old value.*rejected"):
        change(inverter, conn)
    assert len(inverter.writes) == 1
    assert logged(conn)[0][5:] == ("not_applied", CHARGE_40.hex())


def test_a_write_the_inverter_ignored_is_reported(conn):
    inverter = FakeInverter()
    inverter.applies = False
    with pytest.raises(control.Unconfirmed, match="still holds the old value"):
        change(inverter, conn)
    assert len(inverter.writes) == 1


def test_a_write_that_landed_as_something_else_is_reported_and_not_retried(conn):
    other = bytes.fromhex("17010500f97ffe0c005a0fff")
    inverter = FakeInverter()
    inverter.applied_as = other
    with pytest.raises(control.Unconfirmed, match=other.hex()):
        change(inverter, conn)
    assert len(inverter.writes) == 1
    assert logged(conn)[0][5:] == ("mismatch", other.hex())


def test_not_being_able_to_read_it_back_is_reported_as_unknown(conn):
    inverter = FakeInverter()
    inverter.unreadable_after_write = True
    with pytest.raises(control.Unconfirmed, match="isn't known"):
        change(inverter, conn)
    assert len(inverter.writes) == 1
    assert logged(conn)[0][5:] == ("unknown", None)


def test_sending_a_period_its_own_bytes_is_confirmed(conn):
    inverter = FakeInverter()
    asyncio.run(control.send_period(inverter, conn, 1, CHARGE_40, CHARGE_40))
    assert inverter.writes == [("eco_mode_1", CHARGE_40)]
    assert logged(conn)[0][5] == "confirmed"


def test_the_log_lists_the_latest_first(conn):
    db.add_inverter_write(conn, 1, "aa", "bb", 40, 30)
    db.add_inverter_write(conn, 2, "cc", "dd", 50, 60)
    assert [write["slot"] for write in db.list_inverter_writes(conn)] == [2, 1]
    assert db.list_inverter_writes(conn)[0]["outcome"] == "sent"
