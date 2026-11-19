"""Configuration manager for Tapo Charging Bot."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv

# Automatically load .env if present
load_dotenv()


@dataclass
class AppConfig:
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    allowed_user_ids: set[int] = field(default_factory=set)

    tapo_ip: str = field(
        default_factory=lambda: os.getenv("TAPO_IP", "192.168.1.100").strip()
    )
    tapo_username: str = field(
        default_factory=lambda: os.getenv("TAPO_USERNAME", "").strip()
    )
    tapo_password: str = field(
        default_factory=lambda: os.getenv("TAPO_PASSWORD", "").strip()
    )
    tapo_mock: bool = field(
        default_factory=lambda: os.getenv("TAPO_MOCK", "false").lower() in ("true", "1", "yes")
    )

    default_battery_capacity_kwh: float = field(
        default_factory=lambda: float(os.getenv("DEFAULT_BATTERY_CAPACITY_KWH", "11.8"))
    )
    charging_efficiency: float = field(
        default_factory=lambda: float(os.getenv("CHARGING_EFFICIENCY", "0.85"))
    )
    default_rolling_window_hours: float = field(
        default_factory=lambda: float(os.getenv("DEFAULT_ROLLING_WINDOW_HOURS", "18.0"))
    )
    charging_idle_threshold_watts: float = field(
        default_factory=lambda: float(os.getenv("CHARGING_IDLE_THRESHOLD_WATTS", "15.0"))
    )
    charging_debounce_seconds: int = field(
        default_factory=lambda: int(os.getenv("CHARGING_DEBOUNCE_SECONDS", "45"))
    )
    auto_off_on_full: bool = field(
        default_factory=lambda: os.getenv("AUTO_OFF_ON_FULL", "false").lower() in ("true", "1", "yes")
    )
    electricity_cost_per_kwh: float = field(
        default_factory=lambda: float(os.getenv("ELECTRICITY_COST_PER_KWH", "0.15"))
    )
    polling_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLLING_INTERVAL_SECONDS", "10"))
    )
    database_path: str = field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "tapo_charging.db")
    )

    def __post_init__(self):
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        parsed_ids = set()
        for piece in raw_ids.split(","):
            piece = piece.strip()
            if piece and piece.isdigit():
                parsed_ids.add(int(piece))
        self.allowed_user_ids = parsed_ids

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if user_id is allowed to interact with the bot."""
        if not self.allowed_user_ids:
            # If empty, allow all (or warn in logs)
            return True
        return user_id in self.allowed_user_ids


def get_config() -> AppConfig:
    return AppConfig()
