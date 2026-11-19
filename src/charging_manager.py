"""Charging manager state machine, monitoring loop, and target enforcement."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import Callable, Optional

from src.config import AppConfig
from src.database import Database
from src.tapo_client import BaseTapoClient

logger = logging.getLogger(__name__)


class ChargingManager:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        tapo_client: BaseTapoClient,
    ):
        self.config = config
        self.db = database
        self.tapo = tapo_client

        # Active state
        self.active_session_id: Optional[int] = None
        self.session_start_time: Optional[float] = None
        self.session_charged_kwh: float = 0.0
        self.target_kwh: Optional[float] = None
        self.start_battery_pct: Optional[float] = None
        self.target_battery_pct: Optional[float] = None

        self.last_power_w: float = 0.0
        self.last_poll_time: float = time.time()
        self.is_charging: bool = False
        self.idle_since: Optional[float] = None
        self.armed_plan: Optional[dict] = None

        self._monitor_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._lock = asyncio.Lock()

        # Callbacks
        self.on_charge_started: Optional[Callable[[dict], asyncio.Future]] = None
        self.on_charge_stopped: Optional[Callable[[dict], asyncio.Future]] = None

    async def start(self) -> None:
        """Start the background monitor task."""
        if self._running:
            return
        self._running = True
        self.last_poll_time = time.time()

        # Check if there is an unfinished session in the database
        active_sess = await self.db.get_active_session()
        if active_sess:
            self.active_session_id = active_sess["id"]
            self.target_kwh = active_sess["target_kwh"]
            self.start_battery_pct = active_sess["start_battery_pct"]
            self.target_battery_pct = active_sess["target_battery_pct"]
            self.session_charged_kwh = active_sess["charged_kwh"] or 0.0
            try:
                dt = datetime.datetime.fromisoformat(active_sess["start_time"])
                self.session_start_time = dt.timestamp()
            except Exception:
                self.session_start_time = time.time()
            self.is_charging = True
            logger.info("Resumed existing active charging session #%s", self.active_session_id)

        # Check if an armed deferred plan was saved in database
        armed_raw = await self.db.get_setting("armed_plan")
        if armed_raw:
            try:
                armed_data = json.loads(armed_raw)
                if armed_data and not self.is_charging and self.active_session_id is None:
                    self.armed_plan = armed_data
                    self.target_kwh = armed_data.get("target_kwh")
                    self.start_battery_pct = armed_data.get("start_battery_pct")
                    self.target_battery_pct = armed_data.get("target_battery_pct")
                    logger.info("Restored armed deferred charging plan: %s", self.armed_plan)
            except Exception as e:
                logger.warning("Failed to restore armed plan: %s", e)

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("ChargingManager monitor loop started")

    async def stop(self) -> None:
        """Stop the background monitor task."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("ChargingManager monitor loop stopped")

    async def get_battery_capacity(self) -> float:
        return await self.db.get_float_setting(
            "battery_capacity_kwh", self.config.default_battery_capacity_kwh
        )

    async def get_charging_efficiency(self) -> float:
        return await self.db.get_float_setting(
            "charging_efficiency", self.config.charging_efficiency
        )

    async def get_rolling_window_hours(self) -> float:
        return await self.db.get_float_setting(
            "rolling_window_hours", self.config.default_rolling_window_hours
        )

    async def get_idle_threshold_watts(self) -> float:
        return await self.db.get_float_setting(
            "charging_idle_threshold_watts", self.config.charging_idle_threshold_watts
        )

    async def get_auto_off_on_full(self) -> bool:
        return await self.db.get_bool_setting(
            "auto_off_on_full", self.config.auto_off_on_full
        )

    async def get_electricity_cost_rate(self) -> float:
        return await self.db.get_float_setting(
            "electricity_cost_per_kwh", self.config.electricity_cost_per_kwh
        )

    def calculate_kwh_from_percentage(
        self, start_pct: float, target_pct: float, capacity: float, efficiency: float
    ) -> float:
        """Calculate required wall kWh to charge from start_pct to target_pct."""
        delta_pct = max(0.0, target_pct - start_pct)
        battery_kwh = (delta_pct / 100.0) * capacity
        wall_kwh = battery_kwh / max(0.1, efficiency)
        return round(wall_kwh, 3)

    async def clear_armed_plan_internal(self) -> None:
        """Internal helper to clear armed deferred state and persisted setting."""
        self.armed_plan = None
        await self.db.set_setting("armed_plan", "")

    async def arm_charge_plan(
        self,
        start_pct: Optional[float] = None,
        target_pct: Optional[float] = None,
        target_kwh: Optional[float] = None,
    ) -> dict:
        """
        Configure a deferred charge plan.
        Keeps or turns the plug OFF and waits for an external schedule (e.g. Tapo app schedule)
        to turn it ON and start charging.
        """
        async with self._lock:
            capacity = await self.get_battery_capacity()
            efficiency = await self.get_charging_efficiency()

            if target_kwh is None:
                if start_pct is not None and target_pct is not None:
                    target_kwh = self.calculate_kwh_from_percentage(
                        start_pct, target_pct, capacity, efficiency
                    )
                else:
                    raise ValueError("Must provide either target_kwh or both start_pct and target_pct")

            self.target_kwh = round(target_kwh, 3)
            self.start_battery_pct = start_pct
            self.target_battery_pct = target_pct

            # Ensure plug is OFF for deferred charge
            try:
                await self.tapo.turn_off()
            except Exception as e:
                logger.warning("Could not turn off Tapo plug when arming: %s", e)

            # If there was a lingering session, conclude it
            if self.active_session_id is not None:
                await self._conclude_session(status="ARMED_NEW", turned_off=True)

            self.armed_plan = {
                "target_kwh": self.target_kwh,
                "start_battery_pct": self.start_battery_pct,
                "target_battery_pct": self.target_battery_pct,
                "created_at": time.time(),
            }

            await self.db.set_setting("armed_plan", json.dumps(self.armed_plan))
            logger.info("Deferred charging plan armed: %s", self.armed_plan)

            return {
                "armed": True,
                "start_battery_pct": self.start_battery_pct,
                "target_battery_pct": self.target_battery_pct,
                "target_kwh": self.target_kwh,
                "capacity": capacity,
                "efficiency": efficiency,
            }

    async def cancel_armed_plan(self) -> bool:
        """Cancel the armed deferred charge plan."""
        async with self._lock:
            await self.clear_armed_plan_internal()
            self.target_kwh = None
            self.start_battery_pct = None
            self.target_battery_pct = None
            logger.info("Armed deferred charging plan cancelled")
            return True

    async def set_charge_plan(
        self,
        start_pct: Optional[float] = None,
        target_pct: Optional[float] = None,
        target_kwh: Optional[float] = None,
    ) -> dict:
        """
        Configure a charge plan and turn on the plug if needed.
        Can specify either (start_pct, target_pct) or direct target_kwh.
        """
        async with self._lock:
            capacity = await self.get_battery_capacity()
            efficiency = await self.get_charging_efficiency()

            if target_kwh is None:
                if start_pct is not None and target_pct is not None:
                    target_kwh = self.calculate_kwh_from_percentage(
                        start_pct, target_pct, capacity, efficiency
                    )
                else:
                    raise ValueError("Must provide either target_kwh or both start_pct and target_pct")

            self.target_kwh = round(target_kwh, 3)
            self.start_battery_pct = start_pct
            self.target_battery_pct = target_pct

            # Clear any armed plan since immediate start was selected
            await self.clear_armed_plan_internal()

            # Check if plug was already on
            plug_was_on = False
            try:
                plug_was_on = await self.tapo.is_on()
            except Exception:
                pass

            # Turn on plug
            await self.tapo.turn_on()

            # If there was an active session and vehicle is actively charging:
            if self.active_session_id is not None and plug_was_on and self.is_charging:
                # Update current active session target limit
                await self.db.update_session_targets(
                    self.active_session_id,
                    target_kwh=self.target_kwh,
                    start_battery_pct=self.start_battery_pct,
                    target_battery_pct=self.target_battery_pct,
                )
            else:
                # If an active session was hanging around while plug was off, close it first
                if self.active_session_id is not None:
                    await self._conclude_session(status="RESET", turned_off=False)

                today_kwh = await self.tapo.get_today_energy_kwh()
                self.session_charged_kwh = 0.0
                self.session_start_time = time.time()
                self.active_session_id = await self.db.start_session(
                    target_kwh=self.target_kwh,
                    start_battery_pct=start_pct,
                    target_battery_pct=target_pct,
                    start_energy_kwh=today_kwh,
                )

            return {
                "session_id": self.active_session_id,
                "start_battery_pct": self.start_battery_pct,
                "target_battery_pct": self.target_battery_pct,
                "target_kwh": self.target_kwh,
                "capacity": capacity,
                "efficiency": efficiency,
            }

    async def manual_stop(self) -> Optional[dict]:
        """Manually stop charging and turn off the plug."""
        async with self._lock:
            await self.tapo.turn_off()
            if self.active_session_id is not None:
                summary = await self._conclude_session(
                    status="MANUAL_STOP", turned_off=True
                )
                return summary
            return None

    async def manual_turn_on(self) -> bool:
        """Manually turn on the plug without a target (or keeping existing)."""
        return await self.tapo.turn_on()

    async def manual_turn_off(self) -> bool:
        """Manually turn off the plug."""
        await self.tapo.turn_off()
        if self.active_session_id is not None:
            await self._conclude_session(status="MANUAL_STOP", turned_off=True)
        return True

    async def _conclude_session(self, status: str, turned_off: bool) -> dict:
        """Internal helper to finalize a session, save to DB, and return summary."""
        now = time.time()
        duration_s = max(0.0, now - (self.session_start_time or now))
        charged = round(self.session_charged_kwh, 3)
        cost_rate = await self.get_electricity_cost_rate()
        cost = round(charged * cost_rate, 2)
        lookback_h = await self.get_rolling_window_hours()
        capacity = await self.get_battery_capacity()
        efficiency = await self.get_charging_efficiency()

        # Battery % added estimate
        battery_kwh_added = charged * efficiency
        battery_pct_added = round((battery_kwh_added / max(0.1, capacity)) * 100.0, 1)

        sess_id = self.active_session_id
        if sess_id is not None:
            await self.db.close_session(
                session_id=sess_id,
                status=status,
                charged_kwh=charged,
                cost=cost,
            )

        rolling_kwh = round(await self.db.get_energy_in_window(lookback_h), 3)

        summary = {
            "session_id": sess_id,
            "status": status,
            "duration_seconds": duration_s,
            "charged_kwh": charged,
            "battery_pct_added": battery_pct_added,
            "rolling_window_hours": lookback_h,
            "rolling_window_kwh": rolling_kwh,
            "cost": cost,
            "cost_currency": "€",
            "plug_turned_off": turned_off,
            "start_battery_pct": self.start_battery_pct,
            "target_battery_pct": self.target_battery_pct,
            "target_kwh": self.target_kwh,
        }

        # Reset active state
        self.active_session_id = None
        self.session_start_time = None
        self.session_charged_kwh = 0.0
        self.target_kwh = None
        self.start_battery_pct = None
        self.target_battery_pct = None
        self.is_charging = False
        self.idle_since = None

        return summary

    async def _monitor_loop(self) -> None:
        """Main periodic polling loop."""
        while self._running:
            try:
                await self._monitor_tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in monitor tick: %s", e, exc_info=True)

            await asyncio.sleep(self.config.polling_interval_seconds)

    async def _monitor_tick(self) -> None:
        """Single tick of energy polling and limit evaluation."""
        now = time.time()
        dt = max(0.1, now - self.last_poll_time)
        self.last_poll_time = now

        try:
            power_w = await self.tapo.get_current_power()
            today_kwh = await self.tapo.get_today_energy_kwh()
            plug_is_on = await self.tapo.is_on()
        except Exception as e:
            logger.warning("Could not poll Tapo plug: %s", e)
            return

        self.last_power_w = power_w
        idle_threshold = await self.get_idle_threshold_watts()
        debounce_sec = self.config.charging_debounce_seconds

        # Calculate energy delta for this tick: W * dt / 3600000 -> kWh
        delta_kwh = (power_w * dt) / 3_600_000.0

        # Record energy sample in SQLite
        await self.db.record_sample(
            power_watts=power_w,
            energy_delta_kwh=delta_kwh,
            energy_today_kwh=today_kwh,
            session_id=self.active_session_id,
        )

        async with self._lock:
            # Check if charging is active based on power draw
            if plug_is_on and power_w >= idle_threshold:
                # Vehicle is actively drawing power
                self.idle_since = None

                if not self.is_charging:
                    # Vehicle just started charging!
                    self.is_charging = True
                    was_armed = self.armed_plan is not None
                    if self.armed_plan:
                        await self.clear_armed_plan_internal()

                    if self.active_session_id is None:
                        self.session_start_time = now
                        self.session_charged_kwh = 0.0
                        self.active_session_id = await self.db.start_session(
                            target_kwh=self.target_kwh,
                            start_battery_pct=self.start_battery_pct,
                            target_battery_pct=self.target_battery_pct,
                            start_energy_kwh=today_kwh,
                        )
                        logger.info(
                            "Charging started! Created session #%s (target=%s kWh, was_armed=%s)",
                            self.active_session_id,
                            self.target_kwh,
                            was_armed,
                        )
                        if self.on_charge_started:
                            status_dict = await self.get_status_data()
                            status_dict["was_deferred"] = was_armed
                            asyncio.create_task(self._safe_callback(self.on_charge_started, status_dict))

                # Accumulate energy
                self.session_charged_kwh += delta_kwh
                if self.active_session_id:
                    await self.db.update_session_energy(
                        self.active_session_id, round(self.session_charged_kwh, 4)
                    )

                # Check TARGET LIMIT condition
                if self.target_kwh is not None and self.session_charged_kwh >= self.target_kwh:
                    logger.info(
                        "Target reached! (%.2f / %.2f kWh). Turning off plug.",
                        self.session_charged_kwh,
                        self.target_kwh,
                    )
                    await self.tapo.turn_off()
                    summary = await self._conclude_session(
                        status="TARGET_REACHED", turned_off=True
                    )
                    if self.on_charge_stopped:
                        asyncio.create_task(self._safe_callback(self.on_charge_stopped, summary))

            else:
                # Power is below idle threshold or plug is off
                if self.is_charging:
                    if self.idle_since is None:
                        self.idle_since = now
                        logger.info(
                            "Power dropped to %.1f W (< %.1f W). Starting debounce of %ss.",
                            power_w,
                            idle_threshold,
                            debounce_sec,
                        )
                    elif now - self.idle_since >= debounce_sec:
                        # Confirmed end of natural charge
                        logger.info("Debounce elapsed. Natural end of charging detected.")
                        auto_off = await self.get_auto_off_on_full()
                        turned_off = False
                        if auto_off:
                            await self.tapo.turn_off()
                            turned_off = True

                        summary = await self._conclude_session(
                            status="NATURAL_STOP", turned_off=turned_off
                        )
                        if self.on_charge_stopped:
                            asyncio.create_task(self._safe_callback(self.on_charge_stopped, summary))

    async def _safe_callback(self, callback: Callable[[dict], any], data: dict) -> None:
        try:
            res = callback(data)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.error("Callback execution failed: %s", e)

    async def get_status_data(self) -> dict:
        """Returns comprehensive status dictionary for dashboard display."""
        try:
            plug_is_on = await self.tapo.is_on()
        except Exception:
            plug_is_on = False

        capacity = await self.get_battery_capacity()
        efficiency = await self.get_charging_efficiency()
        lookback_h = await self.get_rolling_window_hours()
        rolling_kwh = await self.db.get_energy_in_window(lookback_h)
        idle_thresh = await self.get_idle_threshold_watts()

        now = time.time()
        duration_s = max(0.0, now - (self.session_start_time or now)) if self.is_charging else 0.0

        pct_added = 0.0
        if capacity > 0:
            pct_added = round(((self.session_charged_kwh * efficiency) / capacity) * 100.0, 1)

        rolling_pct_added = 0.0
        if capacity > 0:
            rolling_pct_added = round(((rolling_kwh * efficiency) / capacity) * 100.0, 1)

        return {
            "plug_is_on": plug_is_on,
            "current_power_w": self.last_power_w,
            "is_charging": self.is_charging,
            "armed_plan": self.armed_plan,
            "idle_threshold_w": idle_thresh,
            "session_id": self.active_session_id,
            "session_duration_s": duration_s,
            "session_charged_kwh": round(self.session_charged_kwh, 3),
            "target_kwh": self.target_kwh,
            "start_battery_pct": self.start_battery_pct,
            "target_battery_pct": self.target_battery_pct,
            "battery_pct_added": pct_added,
            "battery_capacity_kwh": capacity,
            "charging_efficiency": efficiency,
            "rolling_window_hours": lookback_h,
            "rolling_window_kwh": round(rolling_kwh, 3),
            "rolling_pct_added": rolling_pct_added,
        }
