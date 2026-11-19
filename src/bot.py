"""Telegram bot interface with interactive buttons, 5% steps, and configuration wizard."""
from __future__ import annotations

import datetime
import html
import logging
from typing import Optional
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from src.charging_manager import ChargingManager
from src.config import AppConfig
from src.database import Database

logger = logging.getLogger(__name__)


class TelegramChargingBot:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        charging_manager: ChargingManager,
    ):
        self.config = config
        self.db = database
        self.manager = charging_manager
        self.app: Optional[Application] = None
        # User in-memory wizard state for multi-step callbacks: user_id -> dict
        self._user_state: dict[int, dict] = {}
        # Remember last active chat ID for spontaneous completion notifications
        self._primary_chat_id: Optional[int] = None

    def build_application(self) -> Application:
        """Construct and wire up python-telegram-bot application."""
        builder = Application.builder().token(self.config.telegram_bot_token)
        app = builder.build()

        # Command handlers
        app.add_handler(CommandHandler(["start", "menu"], self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("charge", self.cmd_charge))
        app.add_handler(CommandHandler(["stop", "off"], self.cmd_stop))
        app.add_handler(CommandHandler("on", self.cmd_on))
        app.add_handler(CommandHandler("configure", self.cmd_configure))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("help", self.cmd_help))

        # Callback query handler for all inline buttons
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Register charging manager callbacks for notifications
        self.manager.on_charge_started = self._on_charge_started_notify
        self.manager.on_charge_stopped = self._on_charge_stopped_notify

        # Global error handler
        app.add_error_handler(self._on_error)

        self.app = app
        return app

    def _check_allowed(self, user_id: int) -> bool:
        return self.config.is_user_allowed(user_id)

    # ------------------ COMMAND HANDLERS ------------------ #

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            await update.effective_message.reply_text("⛔ Unauthorized. You do not have permission to control this bot.")
            return

        self._primary_chat_id = update.effective_chat.id
        status = await self.manager.get_status_data()
        text = self._format_status_dashboard(status)
        keyboard = self._build_main_menu_keyboard(status)
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        self._primary_chat_id = update.effective_chat.id
        status = await self.manager.get_status_data()
        text = self._format_status_dashboard(status)
        keyboard = self._build_main_menu_keyboard(status)
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )

    async def cmd_charge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        self._primary_chat_id = update.effective_chat.id
        args = context.args or []

        # Check if CLI parameters provided: e.g. /charge 20 80 or /charge 5kwh
        if len(args) == 2 and args[0].replace(".", "", 1).isdigit() and args[1].replace(".", "", 1).isdigit():
            start_p = float(args[0])
            target_p = float(args[1])
            plan = await self.manager.set_charge_plan(start_pct=start_p, target_pct=target_p)
            await update.effective_message.reply_text(
                f"🚀 <b>Charging plan active!</b>\n"
                f"• Target: {start_p:.0f}% ➔ {target_p:.0f}%\n"
                f"• Required wall energy: <b>{plan['target_kwh']:.2f} kWh</b>\n"
                f"• Plug turned ON. Auto-stop when reached.",
                parse_mode=ParseMode.HTML,
            )
            return

        if len(args) == 1 and args[0].lower().endswith("kwh"):
            raw_kwh = args[0].lower().replace("kwh", "")
            if raw_kwh.replace(".", "", 1).isdigit():
                kwh = float(raw_kwh)
                plan = await self.manager.set_charge_plan(target_kwh=kwh)
                await update.effective_message.reply_text(
                    f"🚀 <b>Charging target set!</b>\n"
                    f"• Target energy: <b>{kwh:.2f} kWh</b>\n"
                    f"• Plug turned ON. Auto-stop when reached.",
                    parse_mode=ParseMode.HTML,
                )
                return

        # Otherwise launch interactive Step 1 (Current Battery %)
        text, markup = self._build_step1_current_pct_menu(user.id)
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        summary = await self.manager.manual_stop()
        if summary:
            summary_text = self._format_summary_card(summary)
            await update.effective_message.reply_text(
                f"🛑 <b>Charging stopped manually.</b> Plug turned OFF.\n\n{summary_text}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text("🛑 Plug turned OFF. No active charging session was recorded.")

    async def cmd_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        await self.manager.manual_turn_on()
        await update.effective_message.reply_text("💡 Plug turned ON.", parse_mode=ParseMode.HTML)

    async def cmd_configure(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        self._primary_chat_id = update.effective_chat.id
        text, markup = await self._build_configure_menu()
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        sessions = await self.db.get_recent_sessions(limit=5)
        if not sessions:
            await update.effective_message.reply_text("📜 No past charging sessions recorded yet.")
            return

        lines = ["<b>📜 Recent Charging Sessions:</b>\n"]
        for s in sessions:
            st = s["status"]
            icon = "🎯" if st == "TARGET_REACHED" else ("⚡" if st == "NATURAL_STOP" else "🛑")
            kwh = s.get("charged_kwh") or 0.0
            start_str = s["start_time"][:16].replace("T", " ")
            cost = s.get("cost") or 0.0
            lines.append(
                f"{icon} <b>Session #{s['id']}</b> ({start_str})\n"
                f"   • Energy: <b>{kwh:.2f} kWh</b> | Status: {st}\n"
                f"   • Est. Cost: {cost:.2f} €\n"
            )

        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._check_allowed(user.id):
            return

        help_text = (
            "<b>🤖 Tapo Charging Bot Commands:</b>\n\n"
            "• /start or /menu - Open main control dashboard with buttons\n"
            "• /status - View real-time charging status and power draw\n"
            "• /charge - Interactive target setup (5% steps)\n"
            "• /charge &lt;start%&gt; &lt;target%&gt; - Quick charge (e.g. <code>/charge 20 80</code>)\n"
            "• /charge &lt;kwh&gt;kwh - Quick kWh target (e.g. <code>/charge 5kwh</code>)\n"
            "• /stop or /off - Turn off plug and stop session\n"
            "• /on - Turn on plug manually\n"
            "• /configure - Interactive configuration panel\n"
            "• /history - View recent charging sessions\n"
        )
        await update.effective_message.reply_text(help_text, parse_mode=ParseMode.HTML)

    # ------------------ CALLBACK QUERY DISPATCHER ------------------ #

    async def _safe_edit_message(
        self,
        query,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = ParseMode.HTML,
    ) -> None:
        """Edit message text safely, ignoring Telegram BadRequest when content hasn't changed."""
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                logger.debug("Message content unchanged on edit; ignored.")
                return
            raise

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not user or not self._check_allowed(user.id):
            if query:
                await query.answer("Unauthorized", show_alert=True)
            return

        self._primary_chat_id = update.effective_chat.id
        data = query.data or ""
        await query.answer()

        parts = data.split(":")
        category = parts[0] if parts else ""

        if category == "menu":
            action = parts[1] if len(parts) > 1 else ""
            if action == "refresh":
                status = await self.manager.get_status_data()
                text = self._format_status_dashboard(status)
                keyboard = self._build_main_menu_keyboard(status)
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif action == "charge_setup":
                text, markup = self._build_step1_current_pct_menu(user.id)
                await self._safe_edit_message(query, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif action == "configure":
                text, markup = await self._build_configure_menu()
                await self._safe_edit_message(query, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif action == "turn_on":
                await self.manager.manual_turn_on()
                status = await self.manager.get_status_data()
                text = self._format_status_dashboard(status)
                keyboard = self._build_main_menu_keyboard(status)
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif action == "turn_off":
                summary = await self.manager.manual_stop()
                status = await self.manager.get_status_data()
                text = self._format_status_dashboard(status)
                if summary:
                    text += "\n\n" + self._format_summary_card(summary)
                keyboard = self._build_main_menu_keyboard(status)
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif action == "history":
                sessions = await self.db.get_recent_sessions(limit=5)
                lines = ["<b>📜 Recent Charging Sessions:</b>\n"]
                if not sessions:
                    lines.append("No past charging sessions recorded yet.")
                else:
                    for s in sessions:
                        st = s["status"]
                        icon = "🎯" if st == "TARGET_REACHED" else ("⚡" if st == "NATURAL_STOP" else "🛑")
                        kwh = s.get("charged_kwh") or 0.0
                        start_str = s["start_time"][:16].replace("T", " ")
                        lines.append(f"{icon} <b>Session #{s['id']}</b> ({start_str}): <b>{kwh:.2f} kWh</b> ({st})")
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:refresh")]])
                await self._safe_edit_message(query, "\n".join(lines), reply_markup=markup, parse_mode=ParseMode.HTML)

        elif category == "charge":
            step = parts[1] if len(parts) > 1 else ""
            if step == "set_start":
                start_pct = float(parts[2])
                self._user_state[user.id] = {"start_pct": start_pct}
                text, markup = await self._build_step2_target_pct_menu(start_pct)
                await self._safe_edit_message(query, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif step == "set_target":
                target_pct = float(parts[2])
                state = self._user_state.get(user.id, {})
                start_pct = state.get("start_pct", 20.0)
                state["target_pct"] = target_pct
                self._user_state[user.id] = state
                text, markup = await self._build_step3_confirmation_card(start_pct, target_pct)
                await self._safe_edit_message(query, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif step == "confirm":
                state = self._user_state.pop(user.id, {})
                start_pct = state.get("start_pct", 20.0)
                target_pct = state.get("target_pct", 80.0)
                plan = await self.manager.set_charge_plan(start_pct=start_pct, target_pct=target_pct)
                status = await self.manager.get_status_data()
                text = (
                    f"🚀 <b>Charging Started Immediately!</b>\n\n"
                    f"• Target Plan: <b>{start_pct:.0f}% ➔ {target_pct:.0f}%</b> (+{target_pct - start_pct:.0f}%)\n"
                    f"• Wall Target Energy: <b>{plan['target_kwh']:.2f} kWh</b>\n"
                    f"• Plug State: <b>🟢 ON</b>\n\n"
                    f"The plug will automatically switch OFF when {plan['target_kwh']:.2f} kWh is reached.\n"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Live Progress", callback_data="menu:refresh")],
                    [InlineKeyboardButton("🛑 Stop & Turn Off", callback_data="menu:turn_off")],
                ])
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif step == "arm":
                state = self._user_state.pop(user.id, {})
                start_pct = state.get("start_pct", 20.0)
                target_pct = state.get("target_pct", 80.0)
                plan = await self.manager.arm_charge_plan(start_pct=start_pct, target_pct=target_pct)
                text = (
                    f"⏳ <b>Deferred Charging Armed!</b>\n\n"
                    f"• Target Plan: <b>{start_pct:.0f}% ➔ {target_pct:.0f}%</b> (+{target_pct - start_pct:.0f}%)\n"
                    f"• Wall Target Energy: <b>{plan['target_kwh']:.2f} kWh</b>\n"
                    f"• Plug State: <b>🔴 OFF (Standby)</b>\n\n"
                    f"The plug is currently OFF and waiting. When the plug turns ON (via your Tapo app schedule or timer) "
                    f"and drawing power begins, charging will be monitored and the plug will turn OFF automatically "
                    f"upon reaching <b>{plan['target_kwh']:.2f} kWh</b>."
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"🚀 Start Immediately Now", callback_data="charge:start_armed")],
                    [InlineKeyboardButton("❌ Cancel Armed Target", callback_data="charge:cancel_armed")],
                    [InlineKeyboardButton("📊 Dashboard", callback_data="menu:refresh")],
                ])
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif step == "cancel_armed":
                await self.manager.cancel_armed_plan()
                status = await self.manager.get_status_data()
                text = "❌ <b>Armed deferred charge cancelled.</b>\n\n" + self._format_status_dashboard(status)
                keyboard = self._build_main_menu_keyboard(status)
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif step == "start_armed":
                armed = self.manager.armed_plan or {}
                start_pct = armed.get("start_battery_pct")
                target_pct = armed.get("target_battery_pct")
                target_kwh = armed.get("target_kwh")
                plan = await self.manager.set_charge_plan(start_pct=start_pct, target_pct=target_pct, target_kwh=target_kwh)
                text = (
                    f"🚀 <b>Charging Started Immediately!</b>\n\n"
                    f"• Wall Target Energy: <b>{plan['target_kwh']:.2f} kWh</b>\n"
                    f"• Plug State: <b>🟢 ON</b>\n\n"
                    f"The plug will automatically switch OFF when {plan['target_kwh']:.2f} kWh is reached.\n"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Live Progress", callback_data="menu:refresh")],
                    [InlineKeyboardButton("🛑 Stop & Turn Off", callback_data="menu:turn_off")],
                ])
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            elif step == "cancel":
                self._user_state.pop(user.id, None)
                status = await self.manager.get_status_data()
                text = self._format_status_dashboard(status)
                keyboard = self._build_main_menu_keyboard(status)
                await self._safe_edit_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

        elif category == "cfg":
            field = parts[1] if len(parts) > 1 else ""
            if field == "menu":
                text, markup = await self._build_configure_menu()
                await self._safe_edit_message(query, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif field == "battery":
                val = parts[2] if len(parts) > 2 else ""
                if val:
                    await self.db.set_setting("battery_capacity_kwh", float(val))
                    text, markup = await self._build_configure_menu()
                    await self._safe_edit_message(query, f"✅ Battery capacity set to {val} kWh!\n\n" + text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("11.8 kWh", callback_data="cfg:battery:11.8"), InlineKeyboardButton("13.0 kWh", callback_data="cfg:battery:13.0")],
                        [InlineKeyboardButton("15.0 kWh", callback_data="cfg:battery:15.0"), InlineKeyboardButton("18.0 kWh", callback_data="cfg:battery:18.0")],
                        [InlineKeyboardButton("50.0 kWh", callback_data="cfg:battery:50.0"), InlineKeyboardButton("77.0 kWh", callback_data="cfg:battery:77.0")],
                        [InlineKeyboardButton("🔙 Back to Config", callback_data="cfg:menu")],
                    ])
                    await self._safe_edit_message(query, "🔋 <b>Select Usable Battery Capacity:</b>", reply_markup=markup, parse_mode=ParseMode.HTML)

            elif field == "eff":
                val = parts[2] if len(parts) > 2 else ""
                if val:
                    await self.db.set_setting("charging_efficiency", float(val) / 100.0)
                    text, markup = await self._build_configure_menu()
                    await self._safe_edit_message(query, f"✅ Efficiency set to {val}%!\n\n" + text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("80%", callback_data="cfg:eff:80"), InlineKeyboardButton("85%", callback_data="cfg:eff:85")],
                        [InlineKeyboardButton("90%", callback_data="cfg:eff:90"), InlineKeyboardButton("95%", callback_data="cfg:eff:95")],
                        [InlineKeyboardButton("100%", callback_data="cfg:eff:100")],
                        [InlineKeyboardButton("🔙 Back to Config", callback_data="cfg:menu")],
                    ])
                    await self._safe_edit_message(query, "⚡ <b>Select Charging Efficiency:</b>\n<i>(Accounts for onboard charger and thermal losses)</i>", reply_markup=markup, parse_mode=ParseMode.HTML)

            elif field == "window":
                val = parts[2] if len(parts) > 2 else ""
                if val:
                    await self.db.set_setting("rolling_window_hours", float(val))
                    text, markup = await self._build_configure_menu()
                    await self._safe_edit_message(query, f"✅ Rolling lookback window set to {val}h!\n\n" + text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("12 Hours", callback_data="cfg:window:12"), InlineKeyboardButton("18 Hours", callback_data="cfg:window:18")],
                        [InlineKeyboardButton("24 Hours", callback_data="cfg:window:24"), InlineKeyboardButton("48 Hours", callback_data="cfg:window:48")],
                        [InlineKeyboardButton("🔙 Back to Config", callback_data="cfg:menu")],
                    ])
                    await self._safe_edit_message(query, "⏱ <b>Select Rolling Window Lookback:</b>", reply_markup=markup, parse_mode=ParseMode.HTML)

            elif field == "auto_off":
                current = await self.manager.get_auto_off_on_full()
                new_val = not current
                await self.db.set_setting("auto_off_on_full", new_val)
                text, markup = await self._build_configure_menu()
                state_str = "Enabled (turns OFF on full)" if new_val else "Disabled (keeps ON after full)"
                await self._safe_edit_message(query, f"✅ Auto-Off on full charge: <b>{state_str}</b>\n\n" + text, reply_markup=markup, parse_mode=ParseMode.HTML)

            elif field == "cost":
                val = parts[2] if len(parts) > 2 else ""
                if val:
                    await self.db.set_setting("electricity_cost_per_kwh", float(val))
                    text, markup = await self._build_configure_menu()
                    await self._safe_edit_message(query, f"✅ Electricity cost set to {val} €/kWh!\n\n" + text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("0.10 €", callback_data="cfg:cost:0.10"), InlineKeyboardButton("0.15 €", callback_data="cfg:cost:0.15")],
                        [InlineKeyboardButton("0.20 €", callback_data="cfg:cost:0.20"), InlineKeyboardButton("0.25 €", callback_data="cfg:cost:0.25")],
                        [InlineKeyboardButton("0.30 €", callback_data="cfg:cost:0.30")],
                        [InlineKeyboardButton("🔙 Back to Config", callback_data="cfg:menu")],
                    ])
                    await self._safe_edit_message(query, "💶 <b>Select Electricity Cost (€/kWh):</b>", reply_markup=markup, parse_mode=ParseMode.HTML)

    # ------------------ UI BUILDERS & FORMATTERS ------------------ #

    def _format_status_dashboard(self, s: dict) -> str:
        plug_icon = "🟢" if s["plug_is_on"] else "🔴"
        plug_state = "ON" if s["plug_is_on"] else "OFF"
        charge_status = "⚡ Charging" if s["is_charging"] else "Idle"

        power_w = s["current_power_w"]
        sess_kwh = s["session_charged_kwh"]
        target_kwh = s.get("target_kwh")
        rolling_h = s["rolling_window_hours"]
        rolling_kwh = s["rolling_window_kwh"]
        rolling_pct = s.get("rolling_pct_added", 0.0)

        lines = [
            f"<b>🔌 Tapo Charging Assistant</b>",
            f"──────────────────────────",
            f"• Plug State:       {plug_icon} <b>{plug_state}</b> ({charge_status})",
            f"• Current Power:    <b>{power_w:,.1f} W</b>",
        ]

        if s["is_charging"] or sess_kwh > 0:
            dur_m = int(s["session_duration_s"] // 60)
            dur_h = dur_m // 60
            dur_m_rem = dur_m % 60
            dur_str = f"{dur_h}h {dur_m_rem:02d}m" if dur_h > 0 else f"{dur_m}m"
            pct_added = s.get("battery_pct_added", 0.0)
            lines.append(f"• Active Session:   <b>{sess_kwh:.2f} kWh</b> (~+{pct_added:.1f}%) [⏱ {dur_str}]")

        if target_kwh is not None and target_kwh > 0:
            pct_reached = min(100.0, (sess_kwh / target_kwh) * 100.0)
            bar = self._render_progress_bar(pct_reached)
            lines.append(f"• Target Limit:     <b>{target_kwh:.2f} kWh</b> ({pct_reached:.1f}% reached)")
            lines.append(f"  {bar} {pct_reached:.0f}%")
        elif s.get("armed_plan") and not s["is_charging"]:
            armed = s["armed_plan"]
            start_p = armed.get("start_battery_pct")
            target_p = armed.get("target_battery_pct")
            pct_str = f" ({start_p:.0f}% ➔ {target_p:.0f}%)" if start_p is not None and target_p is not None else ""
            lines.append(f"• ⏳ Armed Deferred: <b>{armed['target_kwh']:.2f} kWh</b>{pct_str} (Waiting for plug ON)")

        lines.append(f"• Rolling ({rolling_h:.0f}h):    <b>{rolling_kwh:.2f} kWh</b> (~+{rolling_pct:.1f}% battery)")
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        lines.append(f"• Last Updated:     <b>{now_str}</b>")
        return "\n".join(lines)

    def _build_main_menu_keyboard(self, s: dict) -> InlineKeyboardMarkup:
        rows = []
        if s.get("armed_plan") and not s["is_charging"]:
            rows.append([
                InlineKeyboardButton("🚀 Start Armed Now", callback_data="charge:start_armed"),
                InlineKeyboardButton("❌ Cancel Armed", callback_data="charge:cancel_armed"),
            ])

        rows.extend([
            [
                InlineKeyboardButton("⚡ Set Charge Target", callback_data="menu:charge_setup"),
                InlineKeyboardButton("📊 Refresh Status", callback_data="menu:refresh"),
            ],
            [
                InlineKeyboardButton("⚙️ Configure", callback_data="menu:configure"),
                InlineKeyboardButton("📜 Session History", callback_data="menu:history"),
            ],
        ])
        if s["plug_is_on"]:
            rows.append([InlineKeyboardButton("🛑 Stop & Turn OFF", callback_data="menu:turn_off")])
        else:
            rows.append([InlineKeyboardButton("💡 Turn Plug ON", callback_data="menu:turn_on")])

        return InlineKeyboardMarkup(rows)

    def _build_step1_current_pct_menu(self, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        """Step 1/2: Battery selector with 5% increments from 0% to 95%."""
        text = (
            "🔋 <b>Step 1/2: Current Battery Level</b>\n\n"
            "What is your car's <b>current</b> battery percentage?"
        )
        # Create 5 buttons per row in 5% steps
        rows = []
        row = []
        for pct in range(0, 100, 5):
            row.append(InlineKeyboardButton(f"{pct}%", callback_data=f"charge:set_start:{pct}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append([InlineKeyboardButton("🔙 Cancel", callback_data="charge:cancel")])
        return text, InlineKeyboardMarkup(rows)

    async def _build_step2_target_pct_menu(self, start_pct: float) -> tuple[str, InlineKeyboardMarkup]:
        """Step 2/2: Target battery level in 5% steps strictly above start_pct."""
        text = (
            f"🎯 <b>Step 2/2: Target Battery Level</b>\n\n"
            f"Current level selected: <b>{start_pct:.0f}%</b>\n"
            f"Select what percentage you want to charge to:"
        )
        rows = []
        row = []
        start_int = int(start_pct)

        # Show target percentages from (start_int + 5) up to 100% in 5% steps
        for pct in range(start_int + 5, 105, 5):
            row.append(InlineKeyboardButton(f"{pct}%", callback_data=f"charge:set_target:{pct}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        # Add relative increment shortcuts (+10%, +20%, +30%) if applicable
        shortcuts = []
        for inc in [10, 20, 30]:
            target = start_int + inc
            if target <= 100:
                shortcuts.append(InlineKeyboardButton(f"+{inc}% ({target}%)", callback_data=f"charge:set_target:{target}"))
        if shortcuts:
            rows.append(shortcuts)

        rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu:charge_setup")])
        return text, InlineKeyboardMarkup(rows)

    async def _build_step3_confirmation_card(self, start_pct: float, target_pct: float) -> tuple[str, InlineKeyboardMarkup]:
        capacity = await self.manager.get_battery_capacity()
        efficiency = await self.manager.get_charging_efficiency()
        delta_pct = target_pct - start_pct
        battery_added_kwh = round((delta_pct / 100.0) * capacity, 2)
        wall_req_kwh = self.manager.calculate_kwh_from_percentage(start_pct, target_pct, capacity, efficiency)

        # Approximate charging time assuming 2.3 kW wall charging
        est_hours = wall_req_kwh / 2.3
        est_m = int(est_hours * 60)
        est_h = est_m // 60
        est_rem_m = est_m % 60
        est_time_str = f"{est_h}h {est_rem_m:02d}m" if est_h > 0 else f"{est_m}m"

        cost_rate = await self.manager.get_electricity_cost_rate()
        est_cost = round(wall_req_kwh * cost_rate, 2)

        text = (
            f"⚡ <b>Charging Plan Summary:</b>\n"
            f"────────────────────────\n"
            f"• Current Battery:   <b>{start_pct:.0f}%</b>\n"
            f"• Target Battery:    <b>{target_pct:.0f}%</b> (+{delta_pct:.0f}%)\n"
            f"• Battery Energy:    <b>{battery_added_kwh:.2f} kWh</b> (of {capacity:.1f} kWh total)\n"
            f"• Wall Draw Req:     <b>{wall_req_kwh:.2f} kWh</b> (at {int(efficiency*100)}% efficiency)\n"
            f"• Est. Time:         <b>~{est_time_str}</b> (at ~2.3 kW / 10A)\n"
            f"• Est. Cost:         <b>{est_cost:.2f} €</b>\n\n"
            f"The plug will automatically turn OFF when reaching <b>{wall_req_kwh:.2f} kWh</b>.\n\n"
            f"• <b>Start Now:</b> Turns plug ON immediately.\n"
            f"• <b>Arm Deferred:</b> Keeps plug OFF, waiting for your scheduled Tapo turn-on."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🚀 Start Charging Now ({wall_req_kwh:.2f} kWh)", callback_data="charge:confirm")],
            [InlineKeyboardButton(f"⏳ Arm Deferred Charge ({wall_req_kwh:.2f} kWh)", callback_data="charge:arm")],
            [InlineKeyboardButton("🔄 Change", callback_data="menu:charge_setup"), InlineKeyboardButton("❌ Cancel", callback_data="charge:cancel")],
        ])
        return text, markup

    async def _build_configure_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        capacity = await self.manager.get_battery_capacity()
        efficiency = await self.manager.get_charging_efficiency()
        lookback = await self.manager.get_rolling_window_hours()
        idle_w = await self.manager.get_idle_threshold_watts()
        auto_off = await self.manager.get_auto_off_on_full()
        cost_rate = await self.manager.get_electricity_cost_rate()

        auto_off_str = "Enabled (turns OFF on full)" if auto_off else "Disabled (keeps ON after full)"

        text = (
            f"⚙️ <b>Tapo Bot Configuration</b>\n"
            f"──────────────────────────\n"
            f"• 🔋 Battery Capacity:    <b>{capacity:.1f} kWh</b>\n"
            f"• ⚡ Charging Efficiency: <b>{int(efficiency * 100)}%</b> (~{capacity / efficiency:.1f} kWh wall for 100%)\n"
            f"• ⏱ Rolling Lookback:    <b>{lookback:.0f} hours</b>\n"
            f"• 🔌 Idle Threshold:      <b>{idle_w:.0f} Watts</b>\n"
            f"• 🛑 Auto-Off on Full:    <b>{auto_off_str}</b>\n"
            f"• 💶 Electricity Rate:    <b>{cost_rate:.2f} €/kWh</b>\n\n"
            f"Tap an option below to adjust settings:"
        )

        rows = [
            [
                InlineKeyboardButton("🔋 Battery Capacity", callback_data="cfg:battery:"),
                InlineKeyboardButton("⚡ Efficiency", callback_data="cfg:eff:"),
            ],
            [
                InlineKeyboardButton("⏱ Lookback Window", callback_data="cfg:window:"),
                InlineKeyboardButton("🛑 Auto-Off Toggle", callback_data="cfg:auto_off"),
            ],
            [
                InlineKeyboardButton("💶 Electricity Cost", callback_data="cfg:cost:"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:refresh"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    def _format_summary_card(self, summary: dict) -> str:
        dur_m = int(summary["duration_seconds"] // 60)
        dur_h = dur_m // 60
        dur_rem = dur_m % 60
        dur_str = f"{dur_h}h {dur_rem:02d}m" if dur_h > 0 else f"{dur_m}m"

        reason_map = {
            "TARGET_REACHED": "🎯 Target Limit Reached",
            "NATURAL_STOP": "⚡ Vehicle Completed Charge (Idle)",
            "MANUAL_STOP": "🛑 Stopped Manually",
        }
        reason_str = reason_map.get(summary["status"], summary["status"])
        plug_status_str = "Turned OFF automatically" if summary["plug_turned_off"] else "Left ON"

        pct_added = summary.get("battery_pct_added", 0.0)
        charged_kwh = summary.get("charged_kwh", 0.0)
        rolling_h = summary.get("rolling_window_hours", 18.0)
        rolling_kwh = summary.get("rolling_window_kwh", 0.0)
        cost = summary.get("cost", 0.0)

        return (
            f"⚡ <b>Charging Session Completed!</b>\n"
            f"──────────────────────────\n"
            f"• ⏱ Duration:         <b>{dur_str}</b>\n"
            f"• 🔌 Session Charged:  <b>{charged_kwh:.2f} kWh</b> (~+{pct_added:.1f}% battery)\n"
            f"• 🕒 Last {rolling_h:.0f}h Total:   <b>{rolling_kwh:.2f} kWh</b>\n"
            f"• 🛑 Stop Reason:      <b>{reason_str}</b>\n"
            f"• 🔌 Plug Status:      <b>{plug_status_str}</b>\n"
            f"• 💰 Est. Cost:        <b>{cost:.2f} €</b>"
        )

    def _render_progress_bar(self, percentage: float, length: int = 10) -> str:
        percentage = max(0.0, min(100.0, percentage))
        filled = int(round((percentage / 100.0) * length))
        empty = length - filled
        return f"[{'█' * filled}{'░' * empty}]"

    # ------------------ ASYNC NOTIFICATION CALLBACK ------------------ #

    async def _on_charge_stopped_notify(self, summary: dict) -> None:
        """Called automatically by ChargingManager when charging stops."""
        if not self.app:
            return

        summary_text = self._format_summary_card(summary)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ New Charge Target", callback_data="menu:charge_setup")],
            [InlineKeyboardButton("📊 View Dashboard", callback_data="menu:refresh")],
        ])

        # Send to primary active chat or allowed users
        recipients = set()
        if self._primary_chat_id:
            recipients.add(self._primary_chat_id)
        for uid in self.config.allowed_user_ids:
            recipients.add(uid)

        for chat_id in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=summary_text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error("Failed to send notification to chat_id %s: %s", chat_id, e)

    async def _on_charge_started_notify(self, s: dict) -> None:
        """Called automatically by ChargingManager when charging starts."""
        if not self.app:
            return

        target_kwh = s.get("target_kwh")
        was_deferred = s.get("was_deferred", False)
        target_str = f"<b>{target_kwh:.2f} kWh</b>" if target_kwh else "<i>No limit (Full charge)</i>"
        header = "⚡ <b>Scheduled/Deferred Charge Started!</b>" if was_deferred else "⚡ <b>Charging Started!</b>"

        text = (
            f"{header}\n"
            f"──────────────────────────\n"
            f"• Current Draw: <b>{s.get('current_power_w', 0.0):.0f} W</b>\n"
            f"• Target Limit: {target_str}\n\n"
        )
        if target_kwh:
            text += f"The plug will automatically turn OFF once {target_kwh:.2f} kWh is reached."
        else:
            text += "The plug will charge until the battery is full or manually stopped."

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Dashboard", callback_data="menu:refresh")],
            [InlineKeyboardButton("🛑 Stop & Turn Off", callback_data="menu:turn_off")],
        ])

        recipients = set()
        if self._primary_chat_id:
            recipients.add(self._primary_chat_id)
        for uid in self.config.allowed_user_ids:
            recipients.add(uid)

        for chat_id in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error("Failed to send start notification to chat_id %s: %s", chat_id, e)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log errors caused by updates and notify user gracefully."""
        if isinstance(context.error, BadRequest) and "message is not modified" in str(context.error).lower():
            logger.debug("Ignored BadRequest in error handler: %s", context.error)
            return

        logger.error("Exception while handling an update: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ An unexpected error occurred while processing your request. Please try again."
                )
            except Exception:
                pass
