"""Main entry point for Tapo Charging Bot application."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from src.bot import TelegramChargingBot
from src.charging_manager import ChargingManager
from src.config import get_config
from src.database import Database
from src.tapo_client import MockTapoPlugClient, TapoPlugClient

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")


async def async_main() -> None:
    config = get_config()

    if not config.telegram_bot_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set in environment or .env file!\n"
            "Please create a .env file based on .env.example with your bot token."
        )
        sys.exit(1)

    # 1. Initialize Database
    logger.info("Initializing database: %s", config.database_path)
    db = Database(config.database_path)
    await db.init()

    # 2. Initialize Tapo Client
    if config.tapo_mock:
        logger.info("Using MOCK Tapo smart plug client for simulation.")
        tapo_client = MockTapoPlugClient(ip=config.tapo_ip)
    else:
        logger.info("Using real Tapo smart plug client at %s", config.tapo_ip)
        tapo_client = TapoPlugClient(
            ip=config.tapo_ip,
            username=config.tapo_username,
            password=config.tapo_password,
        )

    # Test initial connection
    connected = await tapo_client.connect()
    if not connected and not config.tapo_mock:
        logger.warning(
            "Initial connection to Tapo plug at %s failed. Will retry automatically during operation.",
            config.tapo_ip,
        )

    # 3. Initialize Charging Manager
    manager = ChargingManager(
        config=config,
        database=db,
        tapo_client=tapo_client,
    )
    await manager.start()

    # 4. Initialize Telegram Bot
    bot_handler = TelegramChargingBot(
        config=config,
        database=db,
        charging_manager=manager,
    )
    app = bot_handler.build_application()

    # 5. Signal handling for graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Signal handling might not be supported on some platforms/threads
            pass

    logger.info("Starting Telegram Bot...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Tapo Charging Bot is RUNNING. Press Ctrl+C to terminate.")

    # Wait until shutdown signal
    await stop_event.wait()

    logger.info("Shutting down bot and tasks...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await manager.stop()
    logger.info("Tapo Charging Bot shut down cleanly.")


def main():
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Program terminated.")


if __name__ == "__main__":
    main()
