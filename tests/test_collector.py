import asyncio
import itertools
import json

import pytest
from fastapi import HTTPException

from solar_monitor import collector, db
from solar_monitor.api import create_app
from solar_monitor.collector import Collector

SERIAL = "SERIAL-MUST-NOT-LEAK"


class FakeInverter:
    model_name = "test"
    rated_power = 5000
    firmware = "1"
    arm_firmware = "2"
    modbus_version = 3
    serial_number = SERIAL

    def __init__(self, failures=()):
        self.reads = 0
        self.failures = set(failures)

    async def read_runtime_data(self):
        self.reads += 1
        if self.reads in self.failures:
            raise OSError("read failed")
        return {"ppv": 700, "e_total": 5.0}

    def sensors(self):
        return []


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """No waiting between polls, and each reading gets its own timestamp."""
    monkeypatch.setattr(collector, "POLL_SECONDS", 0)
    ticks = itertools.count(1_800_000_000, 15)
    monkeypatch.setattr(collector.time, "time", lambda: next(ticks))


def use_inverter(monkeypatch, inverter):
    async def connect(*_, **__):
        return inverter

    monkeypatch.setattr(collector.goodwe, "connect", connect)


async def wait_for(condition):
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never became true")


def rows(conn):
    return conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]


def test_readings_stay_in_memory_until_the_flush_interval(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 3600)
    inverter = FakeInverter()
    use_inverter(monkeypatch, inverter)
    app_collector = Collector("host", 502, conn)

    async def scenario():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: inverter.reads >= 3)
            assert rows(conn) == 0
            assert app_collector.storage["pending"] >= 3

    asyncio.run(scenario())


def test_shutdown_saves_what_is_still_buffered(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 3600)
    inverter = FakeInverter()
    use_inverter(monkeypatch, inverter)
    app_collector = Collector("host", 502, conn)

    async def scenario():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: inverter.reads >= 3)

    asyncio.run(scenario())
    assert rows(conn) == inverter.reads
    assert app_collector.buffer == []
    assert app_collector.storage["saved"] == inverter.reads
    assert app_collector.storage["pending"] == 0


class SwallowsFirstCancel(FakeInverter):
    """Like goodwe, which turns a cancel during a read into an ordinary failed read."""

    blocked = False

    async def read_runtime_data(self):
        self.reads += 1
        if self.reads == 1:
            self.blocked = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise OSError("read failed") from None
        return {"ppv": 700, "e_total": 5.0}


def test_shutdown_still_saves_when_a_read_swallows_the_cancel(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 3600)
    inverter = SwallowsFirstCancel()
    use_inverter(monkeypatch, inverter)
    app_collector = Collector("host", 502, conn)
    app_collector.buffer.append(db.Sample(1000, {"ppv": 700}))

    async def lifetime():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: inverter.blocked)

    async def scenario():
        done, _ = await asyncio.wait([asyncio.create_task(lifetime())], timeout=2)
        assert done, "shutdown never finished"

    asyncio.run(scenario())
    assert rows(conn) >= 1


def test_flush_interval_saves_while_running(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 0)
    use_inverter(monkeypatch, FakeInverter())
    app_collector = Collector("host", 502, conn)

    async def scenario():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: rows(conn) >= 3)
            assert app_collector.storage["saved_at"] is not None

    asyncio.run(scenario())


def test_failed_read_saves_no_sample_and_polling_carries_on(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 3600)
    inverter = FakeInverter(failures=[2])
    use_inverter(monkeypatch, inverter)
    app_collector = Collector("host", 502, conn)

    async def scenario():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: app_collector.latest.get("error"))
            await wait_for(lambda: inverter.reads >= 4)
            assert app_collector.latest["error"] is None

    asyncio.run(scenario())
    assert rows(conn) == inverter.reads - 1


def test_an_outage_logs_one_line_when_it_starts_and_one_when_it_ends(monkeypatch, conn, capsys):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 3600)
    inverter = FakeInverter(failures=[2, 3, 4])
    use_inverter(monkeypatch, inverter)
    app_collector = Collector("host", 502, conn)

    async def scenario():
        task = asyncio.create_task(app_collector.run())
        await wait_for(lambda: inverter.reads >= 6)
        task.cancel()

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert output.count("Inverter reads failing: OSError: read failed") == 1
    assert output.count("Inverter reads recovered") == 1


def test_failed_write_keeps_readings_reports_it_and_recovers(monkeypatch, conn):
    use_inverter(monkeypatch, FakeInverter())
    app_collector = Collector("host", 502, conn)
    app_collector.buffer.append(db.Sample(1000, {"ppv": 700}))
    conn.execute("CREATE TRIGGER fail BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT, 'disk trouble'); END")

    app_collector.flush()
    assert "disk trouble" in app_collector.storage["error"]
    assert len(app_collector.buffer) == 1
    assert rows(conn) == 0

    conn.execute("DROP TRIGGER fail")
    app_collector.flush()
    assert app_collector.storage["error"] is None
    assert app_collector.buffer == []
    assert rows(conn) == 1


def test_repeated_failed_writes_log_once_until_they_recover(conn, capsys):
    app_collector = Collector("host", 502, conn)
    app_collector.buffer.append(db.Sample(1000, {"ppv": 700}))
    conn.execute("CREATE TRIGGER fail BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT, 'disk trouble'); END")

    app_collector.flush()
    app_collector.flush()
    conn.execute("DROP TRIGGER fail")
    app_collector.flush()

    output = capsys.readouterr().out
    assert output.count("Saving failed") == 1
    assert output.count("Saving recovered") == 1


def test_a_failing_write_does_not_stop_live_readings(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 0)
    inverter = FakeInverter()
    use_inverter(monkeypatch, inverter)
    conn.execute("CREATE TRIGGER fail BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT, 'disk trouble'); END")
    app_collector = Collector("host", 502, conn)

    async def scenario():
        task = asyncio.create_task(app_collector.run())
        await wait_for(lambda: inverter.reads >= 5)
        assert app_collector.latest["read_at"]
        assert "disk trouble" in app_collector.storage["error"]
        task.cancel()

    asyncio.run(scenario())


def test_serial_number_reaches_neither_the_page_nor_the_database(monkeypatch, conn):
    monkeypatch.setattr(collector, "FLUSH_SECONDS", 0)
    use_inverter(monkeypatch, FakeInverter())
    app_collector = Collector("host", 502, conn)

    async def scenario():
        async with create_app(app_collector).router.lifespan_context(None):
            await wait_for(lambda: rows(conn) >= 1)

    asyncio.run(scenario())
    assert SERIAL not in json.dumps(app_collector.latest, default=str)
    raw = conn.execute("SELECT raw FROM samples").fetchone()[0]
    assert SERIAL not in json.dumps(db.decode_raw(raw))


def route(app_collector, path):
    """The route's own function, called directly so it stays on the connection's thread."""
    return next(r.endpoint for r in create_app(app_collector).routes if r.path == path)


def test_history_route_includes_saved_and_unsaved_readings(conn):
    app_collector = Collector("host", 502, conn)
    db.save_samples(conn, [db.Sample(600, {"ppv": 10})])
    app_collector.buffer.append(db.Sample(660, {"ppv": 20}))

    result = asyncio.run(route(app_collector, "/api/history")(start=0, end=3600))

    assert result["bucket_seconds"] == 60
    assert [(p["ts"], p["pv_w"]) for p in result["points"]] == [(600, 10), (660, 20)]


def test_history_route_refuses_a_backwards_range(conn):
    with pytest.raises(HTTPException) as refused:
        asyncio.run(route(Collector("host", 502, conn), "/api/history")(start=100, end=100))
    assert refused.value.status_code == 400


def test_daily_route_returns_one_row_per_day(conn):
    result = asyncio.run(route(Collector("host", 502, conn), "/api/daily")(days=3))
    assert len(result["days"]) == 3
