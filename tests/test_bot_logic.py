"""Tests for Telegram bot formatting and interactive 5% step button menus."""
import pytest
import pytest_asyncio
from src.bot import TelegramChargingBot
from src.charging_manager import ChargingManager
from src.config import AppConfig
from src.database import Database
from src.tapo_client import MockTapoPlugClient


@pytest_asyncio.fixture
async def bot_instance(tmp_path):
    db = Database(str(tmp_path / "bot_test.db"))
    await db.init()
    config = AppConfig(
        telegram_bot_token="TEST_TOKEN",
        default_battery_capacity_kwh=11.8,
        charging_efficiency=0.85,
    )
    config.allowed_user_ids = {123456}
    tapo = MockTapoPlugClient()
    manager = ChargingManager(config=config, database=db, tapo_client=tapo)
    bot = TelegramChargingBot(config=config, database=db, charging_manager=manager)
    return bot


def test_authorization_check(bot_instance):
    assert bot_instance._check_allowed(123456) is True
    assert bot_instance._check_allowed(999999) is False

    # When allowed list is empty, open access
    bot_instance.config.allowed_user_ids = set()
    assert bot_instance._check_allowed(999999) is True


def test_step1_5pct_step_buttons(bot_instance):
    text, markup = bot_instance._build_step1_current_pct_menu(user_id=123456)
    assert "Step 1/2" in text

    # Flatten keyboard buttons
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    button_texts = [b.text for b in buttons]

    # Verify 5% increments from 0% to 95%
    for pct in range(0, 100, 5):
        assert f"{pct}%" in button_texts, f"Missing {pct}% button"

    # Verify callback data
    btn_25 = next(b for b in buttons if b.text == "25%")
    assert btn_25.callback_data == "charge:set_start:25"


@pytest.mark.asyncio
async def test_step2_target_pct_menu_relative(bot_instance):
    # If starting at 25%, targets must start at 30%
    text, markup = await bot_instance._build_step2_target_pct_menu(start_pct=25.0)
    assert "Current level selected: <b>25%</b>" in text

    buttons = [btn for row in markup.inline_keyboard for btn in row]
    button_texts = [b.text for b in buttons]

    assert "30%" in button_texts
    assert "35%" in button_texts
    assert "100%" in button_texts
    assert "20%" not in button_texts  # must not show percentages <= start_pct
    assert "25%" not in button_texts


def test_progress_bar_rendering(bot_instance):
    bar_0 = bot_instance._render_progress_bar(0.0)
    assert bar_0 == "[░░░░░░░░░░]"

    bar_50 = bot_instance._render_progress_bar(50.0)
    assert bar_50 == "[█████░░░░░]"

    bar_100 = bot_instance._render_progress_bar(100.0)
    assert bar_100 == "[██████████]"


def test_summary_card_formatting(bot_instance):
    summary = {
        "status": "TARGET_REACHED",
        "duration_seconds": 7200,  # 2 hours
        "charged_kwh": 6.94,
        "battery_pct_added": 50.0,
        "rolling_window_hours": 18.0,
        "rolling_window_kwh": 10.5,
        "cost": 1.04,
        "plug_turned_off": True,
    }
    card = bot_instance._format_summary_card(summary)
    assert "2h 00m" in card
    assert "6.94 kWh" in card
    assert "~+50.0% battery" in card
    assert "Last 18h Total:   <b>10.50 kWh</b>" in card
    assert "Target Limit Reached" in card
    assert "Turned OFF automatically" in card
    assert "1.04 €" in card


@pytest.mark.asyncio
async def test_step3_confirmation_card_has_immediate_and_deferred(bot_instance):
    text, markup = await bot_instance._build_step3_confirmation_card(start_pct=15.0, target_pct=80.0)
    assert "Charging Plan Summary" in text
    assert "15%" in text
    assert "80%" in text

    buttons = [btn for row in markup.inline_keyboard for btn in row]
    callbacks = [b.callback_data for b in buttons]
    assert "charge:confirm" in callbacks
    assert "charge:arm" in callbacks
    assert "charge:cancel" in callbacks


def test_main_menu_keyboard_with_armed_plan(bot_instance):
    status_idle = {"plug_is_on": False, "is_charging": False, "armed_plan": None}
    kb_idle = bot_instance._build_main_menu_keyboard(status_idle)
    callbacks_idle = [b.callback_data for row in kb_idle.inline_keyboard for b in row]
    assert "charge:start_armed" not in callbacks_idle

    status_armed = {
        "plug_is_on": False,
        "is_charging": False,
        "armed_plan": {"target_kwh": 9.02, "start_battery_pct": 15.0, "target_battery_pct": 80.0},
    }
    kb_armed = bot_instance._build_main_menu_keyboard(status_armed)
    callbacks_armed = [b.callback_data for row in kb_armed.inline_keyboard for b in row]
    assert "charge:start_armed" in callbacks_armed
    assert "charge:cancel_armed" in callbacks_armed


def test_format_status_dashboard_includes_last_updated(bot_instance):
    status = {
        "plug_is_on": False,
        "is_charging": False,
        "current_power_w": 0.0,
        "session_charged_kwh": 0.0,
        "rolling_window_hours": 18.0,
        "rolling_window_kwh": 8.99,
        "rolling_pct_added": 64.8,
        "target_kwh": None,
        "armed_plan": None,
    }
    dashboard = bot_instance._format_status_dashboard(status)
    assert "Tapo Charging Assistant" in dashboard
    assert "OFF" in dashboard
    assert "0.0 W" in dashboard
    assert "Last Updated:" in dashboard


@pytest.mark.asyncio
async def test_safe_edit_message_ignores_message_not_modified(bot_instance):
    from unittest.mock import AsyncMock
    from telegram.error import BadRequest

    mock_query = AsyncMock()
    mock_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified: specified new message content and reply markup are exactly the same"
    )

    # Should not raise exception
    await bot_instance._safe_edit_message(mock_query, "same text")
    mock_query.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_on_error_ignores_message_not_modified(bot_instance):
    from unittest.mock import AsyncMock, MagicMock
    from telegram.error import BadRequest

    update = MagicMock()
    update.effective_message = AsyncMock()
    context = MagicMock()
    context.error = BadRequest(
        "Message is not modified: specified new message content and reply markup are exactly the same"
    )

    await bot_instance._on_error(update, context)
    # effective_message.reply_text should NOT be called
    update.effective_message.reply_text.assert_not_called()

