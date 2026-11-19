"""Tapo smart plug interface and implementations (Real and Mock)."""
from __future__ import annotations

import abc
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class BaseTapoClient(abc.ABC):
    """Abstract interface for a Tapo smart plug client."""

    @abc.abstractmethod
    async def connect(self) -> bool:
        """Establish or refresh connection to the smart plug."""
        pass

    @abc.abstractmethod
    async def get_current_power(self) -> float:
        """Return current power consumption in Watts (W)."""
        pass

    @abc.abstractmethod
    async def get_today_energy_kwh(self) -> float:
        """Return today's cumulative energy in Kilowatt-Hours (kWh)."""
        pass

    @abc.abstractmethod
    async def is_on(self) -> bool:
        """Return True if the plug relay is currently ON."""
        pass

    @abc.abstractmethod
    async def turn_on(self) -> bool:
        """Turn the plug relay ON."""
        pass

    @abc.abstractmethod
    async def turn_off(self) -> bool:
        """Turn the plug relay OFF."""
        pass

    @abc.abstractmethod
    async def get_device_info(self) -> dict:
        """Return dictionary containing device metadata."""
        pass


class TapoPlugClient(BaseTapoClient):
    """Production client using tapo-py library to communicate over LAN."""

    def __init__(self, ip: str, username: str, password: str):
        self.ip = ip
        self.username = username
        self.password = password
        self._device = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        async with self._lock:
            try:
                from tapo import ApiClient

                client = ApiClient(self.username, self.password)
                # First try p110; if it fails, try p115
                try:
                    self._device = await client.p110(self.ip)
                except Exception:
                    self._device = await client.p115(self.ip)
                logger.info("Successfully connected to Tapo plug at %s", self.ip)
                return True
            except Exception as e:
                logger.error("Failed to connect to Tapo plug at %s: %s", self.ip, e)
                return False

    async def _ensure_connected(self):
        if self._device is None:
            ok = await self.connect()
            if not ok:
                raise ConnectionError(f"Cannot connect to Tapo plug at {self.ip}")

    async def get_current_power(self) -> float:
        await self._ensure_connected()
        try:
            power_res = await self._device.get_current_power()
            return float(power_res.current_power)
        except Exception as e:
            logger.warning("Error fetching current power: %s. Refreshing session...", e)
            await self.connect()
            power_res = await self._device.get_current_power()
            return float(power_res.current_power)

    async def get_today_energy_kwh(self) -> float:
        await self._ensure_connected()
        try:
            usage = await self._device.get_energy_usage()
            # today_energy is in Watt-hours (Wh)
            return float(usage.today_energy) / 1000.0
        except Exception as e:
            logger.warning("Error fetching energy usage: %s. Refreshing session...", e)
            await self.connect()
            usage = await self._device.get_energy_usage()
            return float(usage.today_energy) / 1000.0

    async def is_on(self) -> bool:
        await self._ensure_connected()
        try:
            info = await self._device.get_device_info()
            return bool(info.device_on)
        except Exception as e:
            logger.warning("Error fetching device status: %s. Reconnecting...", e)
            await self.connect()
            info = await self._device.get_device_info()
            return bool(info.device_on)

    async def turn_on(self) -> bool:
        await self._ensure_connected()
        try:
            await self._device.on()
            logger.info("Tapo plug turned ON")
            return True
        except Exception as e:
            logger.error("Failed to turn plug ON: %s", e)
            return False

    async def turn_off(self) -> bool:
        await self._ensure_connected()
        try:
            await self._device.off()
            logger.info("Tapo plug turned OFF")
            return True
        except Exception as e:
            logger.error("Failed to turn plug OFF: %s", e)
            return False

    async def get_device_info(self) -> dict:
        await self._ensure_connected()
        try:
            info = await self._device.get_device_info()
            return info.to_dict()
        except Exception as e:
            logger.warning("Error fetching device info: %s", e)
            return {"ip": self.ip, "connected": False}


class MockTapoPlugClient(BaseTapoClient):
    """Mock client for development, testing and offline simulation."""

    def __init__(
        self,
        ip: str = "127.0.0.1",
        simulated_charging_watts: float = 2300.0,
        simulated_idle_watts: float = 3.5,
    ):
        self.ip = ip
        self._is_on = True
        self._simulated_charging_watts = simulated_charging_watts
        self._simulated_idle_watts = simulated_idle_watts
        self._is_car_connected = True
        self._accumulated_wh = 1200.0  # base history
        self._last_tick = time.time()

    async def connect(self) -> bool:
        logger.info("Connected to Mock Tapo plug (%s)", self.ip)
        return True

    def _tick_energy(self) -> float:
        now = time.time()
        elapsed_s = now - self._last_tick
        self._last_tick = now

        current_w = self._get_instant_watts()
        # Add delta energy in Wh: W * seconds / 3600
        delta_wh = (current_w * elapsed_s) / 3600.0
        self._accumulated_wh += delta_wh
        return current_w

    def _get_instant_watts(self) -> float:
        if not self._is_on:
            return 0.0
        if self._is_car_connected:
            return self._simulated_charging_watts
        return self._simulated_idle_watts

    async def get_current_power(self) -> float:
        power = self._tick_energy()
        return power

    async def get_today_energy_kwh(self) -> float:
        self._tick_energy()
        return self._accumulated_wh / 1000.0

    async def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self) -> bool:
        self._tick_energy()
        self._is_on = True
        logger.info("Mock Tapo plug turned ON")
        return True

    async def turn_off(self) -> bool:
        self._tick_energy()
        self._is_on = False
        logger.info("Mock Tapo plug turned OFF")
        return True

    async def get_device_info(self) -> dict:
        return {
            "device_id": "MOCK-TAPO-P110",
            "model": "P110(EU)-MOCK",
            "nickname": "EV Charger (Mock)",
            "device_on": self._is_on,
            "ip": self.ip,
        }

    # Simulation helper controls for testing
    def set_car_connected(self, connected: bool):
        self._is_car_connected = connected

    def set_charging_watts(self, watts: float):
        self._simulated_charging_watts = watts

    def add_simulated_kwh(self, kwh: float):
        self._accumulated_wh += kwh * 1000.0
