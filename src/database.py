"""SQLite database layer for Tapo Charging Bot using aiosqlite."""
from __future__ import annotations

import datetime
import logging
from typing import Any, Optional
import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "tapo_charging.db"):
        self.db_path = db_path

    async def init(self) -> None:
        """Create tables if they don't already exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS charging_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    start_energy_kwh REAL DEFAULT 0.0,
                    end_energy_kwh REAL DEFAULT 0.0,
                    charged_kwh REAL DEFAULT 0.0,
                    target_kwh REAL,
                    start_battery_pct REAL,
                    target_battery_pct REAL,
                    status TEXT NOT NULL, -- IN_PROGRESS, TARGET_REACHED, NATURAL_STOP, MANUAL_STOP
                    cost REAL DEFAULT 0.0
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS energy_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    timestamp TIMESTAMP NOT NULL,
                    power_watts REAL NOT NULL,
                    energy_delta_kwh REAL DEFAULT 0.0,
                    energy_today_kwh REAL DEFAULT 0.0,
                    FOREIGN KEY (session_id) REFERENCES charging_sessions(id)
                );
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_energy_samples_timestamp
                ON energy_samples(timestamp);
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_start_time
                ON charging_sessions(start_time);
                """
            )
            await db.commit()
            logger.info("Database initialized successfully at %s", self.db_path)

    # ------------------ SETTINGS ------------------ #
    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
                return default

    async def get_float_setting(self, key: str, default: float) -> float:
        val = await self.get_setting(key, None)
        if val is None:
            return default
        try:
            return float(val)
        except ValueError:
            return default

    async def get_bool_setting(self, key: str, default: bool) -> bool:
        val = await self.get_setting(key, None)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes")

    async def set_setting(self, key: str, value: Any) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (key, str(value)),
            )
            await db.commit()

    # ------------------ SESSIONS ------------------ #
    async def start_session(
        self,
        target_kwh: Optional[float] = None,
        start_battery_pct: Optional[float] = None,
        target_battery_pct: Optional[float] = None,
        start_energy_kwh: float = 0.0,
    ) -> int:
        """Start a new charging session and return its id."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO charging_sessions (
                    start_time, target_kwh, start_battery_pct,
                    target_battery_pct, start_energy_kwh, status
                ) VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS')
                """,
                (
                    now,
                    target_kwh,
                    start_battery_pct,
                    target_battery_pct,
                    start_energy_kwh,
                ),
            )
            session_id = cursor.lastrowid
            await db.commit()
            return session_id

    async def get_active_session(self) -> Optional[dict]:
        """Fetch current active IN_PROGRESS session if any."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM charging_sessions
                WHERE status = 'IN_PROGRESS'
                ORDER BY id DESC LIMIT 1
                """
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
                return None

    async def update_session_energy(
        self, session_id: int, charged_kwh: float, end_energy_kwh: float = 0.0
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE charging_sessions
                SET charged_kwh = ?, end_energy_kwh = ?
                WHERE id = ?
                """,
                (charged_kwh, end_energy_kwh, session_id),
            )
            await db.commit()

    async def update_session_targets(
        self,
        session_id: int,
        target_kwh: Optional[float] = None,
        start_battery_pct: Optional[float] = None,
        target_battery_pct: Optional[float] = None,
    ) -> None:
        """Update target limit and battery percentages of an ongoing session."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE charging_sessions
                SET target_kwh = ?, start_battery_pct = ?, target_battery_pct = ?
                WHERE id = ?
                """,
                (target_kwh, start_battery_pct, target_battery_pct, session_id),
            )
            await db.commit()

    async def close_session(
        self,
        session_id: int,
        status: str,
        charged_kwh: float,
        end_energy_kwh: float = 0.0,
        cost: float = 0.0,
    ) -> None:
        """Close an active session with final status and metrics."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE charging_sessions
                SET end_time = ?, status = ?, charged_kwh = ?, end_energy_kwh = ?, cost = ?
                WHERE id = ?
                """,
                (now, status, charged_kwh, end_energy_kwh, cost, session_id),
            )
            await db.commit()

    async def get_recent_sessions(self, limit: int = 5) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM charging_sessions
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ------------------ SAMPLES & ROLLING WINDOW ------------------ #
    async def record_sample(
        self,
        power_watts: float,
        energy_delta_kwh: float = 0.0,
        energy_today_kwh: float = 0.0,
        session_id: Optional[int] = None,
        timestamp: Optional[datetime.datetime] = None,
    ) -> None:
        if timestamp is None:
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            ts = timestamp.isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO energy_samples (
                    session_id, timestamp, power_watts,
                    energy_delta_kwh, energy_today_kwh
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, ts, power_watts, energy_delta_kwh, energy_today_kwh),
            )
            await db.commit()

    async def get_energy_in_window(self, hours: float = 18.0) -> float:
        """Calculate total energy (kWh) consumed in the last N hours."""
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours)
        ).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            # First attempt: Sum delta samples recorded in window
            async with db.execute(
                """
                SELECT SUM(energy_delta_kwh)
                FROM energy_samples
                WHERE timestamp >= ?
                """,
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] is not None and row[0] > 0.0:
                    return float(row[0])

            # Fallback if no fine-grained samples exist: sum session charged_kwh in window
            async with db.execute(
                """
                SELECT SUM(charged_kwh)
                FROM charging_sessions
                WHERE (start_time >= ? OR (end_time IS NOT NULL AND end_time >= ?))
                """,
                (cutoff, cutoff),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] is not None:
                    return float(row[0])

            return 0.0
