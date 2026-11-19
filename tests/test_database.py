"""Tests for database layer and rolling window calculations."""
import datetime
import pytest
import pytest_asyncio
from src.database import Database


@pytest_asyncio.fixture
async def temp_db(tmp_path):
    db_file = tmp_path / "test.db"
    db = Database(str(db_file))
    await db.init()
    return db


@pytest.mark.asyncio
async def test_settings_persistence(temp_db):
    # Default fallback
    val = await temp_db.get_float_setting("battery_capacity_kwh", 11.8)
    assert val == 11.8

    # Set and read float
    await temp_db.set_setting("battery_capacity_kwh", 13.5)
    val2 = await temp_db.get_float_setting("battery_capacity_kwh", 11.8)
    assert val2 == 13.5

    # Boolean setting
    await temp_db.set_setting("auto_off_on_full", True)
    assert await temp_db.get_bool_setting("auto_off_on_full", False) is True

    await temp_db.set_setting("auto_off_on_full", False)
    assert await temp_db.get_bool_setting("auto_off_on_full", True) is False


@pytest.mark.asyncio
async def test_session_lifecycle(temp_db):
    # No active session initially
    assert await temp_db.get_active_session() is None

    # Start session
    sess_id = await temp_db.start_session(
        target_kwh=8.33,
        start_battery_pct=20.0,
        target_battery_pct=80.0,
        start_energy_kwh=10.0,
    )
    assert sess_id is not None

    active = await temp_db.get_active_session()
    assert active is not None
    assert active["id"] == sess_id
    assert active["target_kwh"] == 8.33
    assert active["status"] == "IN_PROGRESS"

    # Update energy
    await temp_db.update_session_energy(sess_id, charged_kwh=4.15)
    active = await temp_db.get_active_session()
    assert active["charged_kwh"] == 4.15

    # Close session
    await temp_db.close_session(
        session_id=sess_id,
        status="TARGET_REACHED",
        charged_kwh=8.33,
        cost=1.25,
    )
    assert await temp_db.get_active_session() is None

    # Check recent sessions
    recent = await temp_db.get_recent_sessions(limit=5)
    assert len(recent) == 1
    assert recent[0]["id"] == sess_id
    assert recent[0]["status"] == "TARGET_REACHED"
    assert recent[0]["cost"] == 1.25


@pytest.mark.asyncio
async def test_rolling_window_energy_calculation(temp_db):
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Sample 20 hours ago: 5.0 kWh (Should NOT be included in 18h window)
    t_20h_ago = now - datetime.timedelta(hours=20)
    await temp_db.record_sample(
        power_watts=2300,
        energy_delta_kwh=5.0,
        energy_today_kwh=5.0,
        timestamp=t_20h_ago,
    )

    # 2. Sample 10 hours ago: 3.5 kWh (SHOULD be included in 18h window)
    t_10h_ago = now - datetime.timedelta(hours=10)
    await temp_db.record_sample(
        power_watts=2300,
        energy_delta_kwh=3.5,
        energy_today_kwh=8.5,
        timestamp=t_10h_ago,
    )

    # 3. Sample 2 hours ago: 2.0 kWh (SHOULD be included in 18h window)
    t_2h_ago = now - datetime.timedelta(hours=2)
    await temp_db.record_sample(
        power_watts=2300,
        energy_delta_kwh=2.0,
        energy_today_kwh=10.5,
        timestamp=t_2h_ago,
    )

    # In last 18 hours: 3.5 + 2.0 = 5.5 kWh
    total_18h = await temp_db.get_energy_in_window(hours=18.0)
    assert pytest.approx(total_18h, 0.01) == 5.5

    # In last 24 hours: 5.0 + 3.5 + 2.0 = 10.5 kWh
    total_24h = await temp_db.get_energy_in_window(hours=24.0)
    assert pytest.approx(total_24h, 0.01) == 10.5

    # In last 5 hours: 2.0 kWh
    total_5h = await temp_db.get_energy_in_window(hours=5.0)
    assert pytest.approx(total_5h, 0.01) == 2.0
