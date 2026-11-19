"""Tests for charging manager state machine and auto-stop logic."""
import asyncio
import pytest
import pytest_asyncio
from src.charging_manager import ChargingManager
from src.config import AppConfig
from src.database import Database
from src.tapo_client import MockTapoPlugClient


@pytest_asyncio.fixture
async def setup_manager(tmp_path):
    db_file = tmp_path / "test_mgr.db"
    db = Database(str(db_file))
    await db.init()

    config = AppConfig(
        default_battery_capacity_kwh=11.8,
        charging_efficiency=0.85,
        default_rolling_window_hours=18.0,
        charging_idle_threshold_watts=15.0,
        charging_debounce_seconds=1,  # short debounce for fast test
        auto_off_on_full=False,
        polling_interval_seconds=1,
    )
    mock_tapo = MockTapoPlugClient(simulated_charging_watts=2300.0)
    manager = ChargingManager(config=config, database=db, tapo_client=mock_tapo)
    return manager, db, mock_tapo


def test_calculate_kwh_from_percentage(setup_manager):
    manager, _, _ = setup_manager
    # 20% to 80% = +60%
    # 60% of 11.8 kWh = 7.08 kWh battery energy
    # 7.08 / 0.85 efficiency = 8.3294 -> 8.33 kWh
    kwh = manager.calculate_kwh_from_percentage(
        start_pct=20.0,
        target_pct=80.0,
        capacity=11.8,
        efficiency=0.85,
    )
    assert pytest.approx(kwh, 0.01) == 8.33

    # Full 0% to 100%
    full_kwh = manager.calculate_kwh_from_percentage(
        start_pct=0.0,
        target_pct=100.0,
        capacity=11.8,
        efficiency=0.85,
    )
    # 11.8 / 0.85 = 13.882 -> 13.88 kWh
    assert pytest.approx(full_kwh, 0.02) == 13.88


@pytest.mark.asyncio
async def test_set_charge_plan(setup_manager):
    manager, db, mock_tapo = setup_manager
    mock_tapo._is_on = False

    plan = await manager.set_charge_plan(start_pct=25.0, target_pct=75.0)
    assert plan["start_battery_pct"] == 25.0
    assert plan["target_battery_pct"] == 75.0
    # 50% of 11.8 = 5.9 / 0.85 = 6.941 kWh
    assert pytest.approx(plan["target_kwh"], 0.02) == 6.94
    assert await mock_tapo.is_on() is True


@pytest.mark.asyncio
async def test_target_reached_auto_stop(setup_manager):
    manager, db, mock_tapo = setup_manager
    stopped_events = []

    async def on_stopped(summary):
        stopped_events.append(summary)

    manager.on_charge_stopped = on_stopped

    # Set a tiny target: 0.001 kWh
    await manager.set_charge_plan(target_kwh=0.001)
    assert await mock_tapo.is_on() is True

    # Run a monitor tick (mock delivers 2300W, which over dt=1.5s is > 0.0009 kWh)
    manager.last_poll_time -= 5.0  # simulate 5 seconds passed
    await manager._monitor_tick()
    # Allow background callback task to run
    await asyncio.sleep(0.05)

    # Plug should have automatically turned off!
    assert await mock_tapo.is_on() is False
    assert len(stopped_events) == 1
    assert stopped_events[0]["status"] == "TARGET_REACHED"
    assert stopped_events[0]["plug_turned_off"] is True


@pytest.mark.asyncio
async def test_natural_stop_keeps_plug_on(setup_manager):
    manager, db, mock_tapo = setup_manager
    stopped_events = []

    async def on_stopped(summary):
        stopped_events.append(summary)

    manager.on_charge_stopped = on_stopped

    # Start charging without target
    manager.is_charging = True
    manager.session_start_time = 1000.0
    manager.session_charged_kwh = 4.0
    manager.active_session_id = await db.start_session(start_energy_kwh=0.0)

    # Car finishes charging: power drops to idle (3.5 W < 15.0 W)
    mock_tapo.set_car_connected(False)
    assert await mock_tapo.is_on() is True

    # Tick 1: detects low power, starts debounce
    await manager._monitor_tick()
    assert len(stopped_events) == 0

    # Simulate debounce elapsed (config debounce is 1s)
    manager.idle_since -= 2.0
    await manager._monitor_tick()
    await asyncio.sleep(0.05)

    # Confirmed natural stop
    assert len(stopped_events) == 1
    assert stopped_events[0]["status"] == "NATURAL_STOP"
    # IMPORTANT: As requested by the user, plug must NOT turn off after full charge
    assert stopped_events[0]["plug_turned_off"] is False
    assert await mock_tapo.is_on() is True


@pytest.mark.asyncio
async def test_set_charge_plan_when_session_already_active(setup_manager):
    manager, db, mock_tapo = setup_manager
    mock_tapo._is_on = True
    manager.is_charging = True
    manager.active_session_id = await db.start_session(target_kwh=5.0)

    # Calling set_charge_plan while active session exists should NOT raise TypeError
    plan = await manager.set_charge_plan(start_pct=20.0, target_pct=80.0)
    assert plan["session_id"] == manager.active_session_id
    assert pytest.approx(plan["target_kwh"], 0.01) == 8.33

    # Check that database was properly updated
    active = await db.get_active_session()
    assert pytest.approx(active["target_kwh"], 0.01) == 8.33


@pytest.mark.asyncio
async def test_deferred_charge_arming_and_execution(setup_manager):
    manager, db, mock_tapo = setup_manager
    started_events = []
    stopped_events = []

    async def on_started(info):
        started_events.append(info)

    async def on_stopped(summary):
        stopped_events.append(summary)

    manager.on_charge_started = on_started
    manager.on_charge_stopped = on_stopped

    # Car plugged in, but plug is currently OFF (waiting for scheduled timer)
    mock_tapo._is_on = False
    mock_tapo.set_car_connected(True)

    # Arm deferred plan with 1.0 kWh target
    plan = await manager.arm_charge_plan(target_kwh=1.0)
    assert plan["armed"] is True
    assert manager.armed_plan is not None
    assert await mock_tapo.is_on() is False

    # Scheduled time arrives: Tapo plug turns ON via its internal schedule
    await mock_tapo.turn_on()
    assert await mock_tapo.is_on() is True

    # Monitor tick 1: detects power, activates charging using armed target
    manager.last_poll_time -= 1.0
    await manager._monitor_tick()
    await asyncio.sleep(0.05)

    assert manager.is_charging is True
    assert manager.armed_plan is None  # cleared once started
    assert len(started_events) == 1
    assert started_events[0]["was_deferred"] is True

    # Simulate accumulating past target limit
    manager.session_charged_kwh = 1.05
    manager.last_poll_time -= 1.0
    await manager._monitor_tick()
    await asyncio.sleep(0.05)

    assert await mock_tapo.is_on() is False
    assert len(stopped_events) == 1
    assert stopped_events[0]["status"] == "TARGET_REACHED"
    assert stopped_events[0]["plug_turned_off"] is True


@pytest.mark.asyncio
async def test_cancel_armed_plan(setup_manager):
    manager, db, mock_tapo = setup_manager
    await manager.arm_charge_plan(target_kwh=5.0)
    assert manager.armed_plan is not None

    await manager.cancel_armed_plan()
    assert manager.armed_plan is None
    assert manager.target_kwh is None
