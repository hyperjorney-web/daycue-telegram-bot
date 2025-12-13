import os
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
VERSION = "0.10.0"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TZ = ZoneInfo("Europe/Stockholm")
DATA_FILE = "data.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daycue")

# =========================
# UI / MENU
# =========================
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🧭 Forecast"
BTN_SETTINGS = "⚙️ Settings"

def menu_kb():
    return ReplyKeyboardMarkup(
        [[BTN_TODAY, BTN_FORECAST], [BTN_SETTINGS]],
        resize_keyboard=True
    )

async def reply(update: Update, text: str, parse_mode: str | None = None):
    await update.message.reply_text(text, reply_markup=menu_kb(), parse_mode=parse_mode)

async def plain(update: Update, text: str, parse_mode: str | None = None):
    # message without keyboard changes (used during onboarding)
    await update.message.reply_text(text, parse_mode=parse_mode)

# =========================
# STORAGE
# =========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

DATA = load_data()

def chat_id(update: Update) -> str:
    return str(update.effective_chat.id)

def get_profile(cid: str):
    return DATA.get(cid)

def set_profile(cid: str, profile: dict):
    DATA[cid] = profile
    save_data(DATA)

def delete_profile(cid: str):
    if cid in DATA:
        del DATA[cid]
        save_data(DATA)

# =========================
# PARSERS / VALIDATION
# =========================
def is_menu_text(text: str) -> bool:
    return text in {BTN_TODAY, BTN_FORECAST, BTN_SETTINGS}

def parse_date_yyyy_mm_dd(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except Exception:
        return None

def parse_time_hhmm(s: str) -> tuple[int, int] | None:
    try:
        parts = s.strip().split(":")
        if len(parts) != 2:
            return None
        hh = int(parts[0]); mm = int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh, mm
    except Exception:
        return None

# =========================
# CYCLE LOGIC (simple MVP)
# =========================
def cycle_day(profile: dict, now: datetime) -> int:
    start = datetime.fromisoformat(profile["period_start"])
    return max(1, (now.date() - start.date()).days + 1)

def phase_and_stats(day: int, cycle_len: int) -> dict:
    # Working theory (MVP): 4 phases mapped to cycle %
    pct = day / max(1, cycle_len)

    if pct <= 0.18:
        phase = "Menstrual 🩸"
        energy = "Low 😴"
        mood = "Sensitive 🫧"
        social = "Low 🧊"
        cravings = "High 🍫"
        tip = "Warmth + patience. Keep plans light."
    elif pct <= 0.45:
        phase = "Follicular 🌱"
        energy = "Rising ⚡"
        mood = "Optimistic ☀️"
        social = "Higher 🗣️"
        cravings = "Medium 🍓"
        tip = "Great time for planning + progress."
    elif pct <= 0.60:
        phase = "Ovulation 🔥"
        energy = "High 🚀"
        mood = "Confident 😎"
        social = "High 🎉"
        cravings = "Low/Med 🥗"
        tip = "Best time for dates + hard talks."
    else:
        phase = "Luteal 🌙"
        energy = "Medium ⛅"
        mood = "Irritable-ish 🌪️"
        social = "Medium 🫥"
        cravings = "High 🍟"
        tip = "Reduce friction. Offer help, avoid pressure."

    return {
        "phase": phase,
        "energy": energy,
        "mood": mood,
        "social": social,
        "cravings": cravings,
        "tip": tip,
    }

def today_card(profile: dict, now: datetime) -> str:
    day = cycle_day(profile, now)
    stats = phase_and_stats(day, profile["cycle_length"])

    return (
        f"📍 *Today*\n"
        f"👤 Partner: *{profile['partner_name']}*\n"
        f"📆 Cycle day: *{day}/{profile['cycle_length']}*\n"
        f"🧬 Phase: *{stats['phase']}*\n\n"
        f"🎮 *Stats*\n"
        f"😴 Energy: *{stats['energy']}*\n"
        f"🫧 Mood: *{stats['mood']}*\n"
        f"🗣️ Social: *{stats['social']}*\n"
        f"🍫 Cravings: *{stats['cravings']}*\n\n"
        f"🫶 *How to help*\n"
        f"• {stats['tip']}"
    )

def forecast_7(profile: dict, now: datetime) -> str:
    lines = ["🧭 *Forecast (7 days)*\n"]
    base_day = cycle_day(profile, now)
    for i in range(7):
        d = base_day + i
        # wrap
        d_wrapped = ((d - 1) % profile["cycle_length"]) + 1
        stats = phase_and_stats(d_wrapped, profile["cycle_length"])
        date_label = (now.date() + timedelta(days=i)).strftime("%a %d %b")
        lines.append(f"• {date_label} - Day {d_wrapped}: {stats['phase']} - {stats['energy']}")
    return "\n".join(lines)

# =========================
# ONBOARDING STATES
# =========================
(
    ST_NAME,
    ST_DOB,
    ST_START,
    ST_END,
    ST_CYCLE,
    ST_NOTIFY,
    ST_SETTINGS_MENU,
    ST_EDIT_FIELD,
    ST_ADD_PERIOD_START,
    ST_ADD_PERIOD_END,
) = range(10)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = chat_id(update)
    prof = get_profile(cid)

    if prof:
        now = datetime.now(TZ)
        await reply(update, f"👋 Welcome back!\n\n{today_card(prof, now)}", parse_mode="Markdown")
        return ConversationHandler.END

    await plain(update,
        "👋 Welcome. Quick onboarding.\n\n"
        "1/6 - Enter partner name (example: Anna)"
    )
    return ST_NAME

def _reject_menu_during_onboarding(text: str) -> bool:
    return is_menu_text(text)

async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text) or len(text) < 2:
        await plain(update, "⚠️ Please type a name (example: Anna)")
        return ST_NAME

    context.user_data["partner_name"] = text
    await plain(update, "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return ST_DOB

async def on_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text):
        await plain(update, "⚠️ Still onboarding. Please enter DOB or type 'skip'.")
        return ST_DOB

    if text.lower() == "skip":
        context.user_data["partner_dob"] = None
    else:
        dt = parse_date_yyyy_mm_dd(text)
        if not dt:
            await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD (example: 1987-08-16) or 'skip'")
            return ST_DOB
        context.user_data["partner_dob"] = dt.date().isoformat()

    await plain(update, "3/6 - Last period START date (YYYY-MM-DD)")
    return ST_START

async def on_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text):
        await plain(update, "⚠️ Still onboarding. Enter period START date (YYYY-MM-DD).")
        return ST_START

    dt = parse_date_yyyy_mm_dd(text)
    if not dt:
        await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return ST_START

    context.user_data["period_start"] = dt.date().isoformat()
    await plain(update, "4/6 - Last period END date (YYYY-MM-DD)")
    return ST_END

async def on_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text):
        await plain(update, "⚠️ Still onboarding. Enter period END date (YYYY-MM-DD).")
        return ST_END

    dt = parse_date_yyyy_mm_dd(text)
    if not dt:
        await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return ST_END

    context.user_data["period_end"] = dt.date().isoformat()
    await plain(update, "5/6 - Cycle length in days (21-35). Example: 28")
    return ST_CYCLE

async def on_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text):
        await plain(update, "⚠️ Still onboarding. Enter cycle length (21-35).")
        return ST_CYCLE

    try:
        n = int(text)
        if not (21 <= n <= 35):
            raise ValueError
    except Exception:
        await plain(update, "⚠️ Invalid number. Enter cycle length 21-35.")
        return ST_CYCLE

    context.user_data["cycle_length"] = n
    await plain(update, "6/6 - Daily notification time (HH:MM). Example: 09:00")
    return ST_NOTIFY

async def on_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _reject_menu_during_onboarding(text):
        await plain(update, "⚠️ Still onboarding. Enter time as HH:MM (example: 09:00).")
        return ST_NOTIFY

    tm = parse_time_hhmm(text)
    if not tm:
        await plain(update, "⚠️ Invalid time. Use HH:MM (example: 09:00).")
        return ST_NOTIFY

    cid = chat_id(update)
    profile = {
        "partner_name": context.user_data["partner_name"],
        "partner_dob": context.user_data.get("partner_dob"),
        "period_start": context.user_data["period_start"],
        "period_end": context.user_data["period_end"],
        "cycle_length": context.user_data["cycle_length"],
        "notify_hh": tm[0],
        "notify_mm": tm[1],
        "paused": False,
        "last_sent": None,  # YYYY-MM-DD
    }
    set_profile(cid, profile)

    now = datetime.now(TZ)
    await reply(update, "✅ Setup complete. Here is your status right now:", parse_mode=None)
    await reply(update, today_card(profile, now), parse_mode="Markdown")
    return ConversationHandler.END

# =========================
# MENU HANDLERS
# =========================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = chat_id(update)
    prof = get_profile(cid)
    if not prof:
        await plain(update, "⚠️ Run /start first to onboard.")
        return

    now = datetime.now(TZ)
    text = update.message.text.strip()

    if text == BTN_TODAY:
        await reply(update, today_card(prof, now), parse_mode="Markdown")
    elif text == BTN_FORECAST:
        await reply(update, forecast_7(prof, now), parse_mode="Markdown")
    elif text == BTN_SETTINGS:
        await reply(update,
            "⚙️ *Settings*\n"
            "Type one of:\n"
            "• `name` - change partner name\n"
            "• `dob` - change partner DOB\n"
            "• `cycle` - change cycle length\n"
            "• `notify` - change notification time\n"
            "• `period` - add/correct period dates\n"
            "• `reset` - delete profile\n",
            parse_mode="Markdown"
        )
        context.user_data["settings_mode"] = True
    else:
        # Settings input handler (only if user is in settings_mode)
        if context.user_data.get("settings_mode"):
            await handle_settings_text(update, context)
        else:
            # Friendly fallback
            await reply(update, "Tap a button: 📍 Today / 🧭 Forecast / ⚙️ Settings")

async def handle_settings_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = chat_id(update)
    prof = get_profile(cid)
    now = datetime.now(TZ)

    cmd = update.message.text.strip().lower()

    if cmd in ("exit", "close"):
        context.user_data["settings_mode"] = False
        await reply(update, "✅ Closed settings.")
        return

    if cmd == "name":
        context.user_data["edit"] = "name"
        await plain(update, "Enter new partner name:")
        return

    if cmd == "dob":
        context.user_data["edit"] = "dob"
        await plain(update, "Enter DOB YYYY-MM-DD or type 'skip':")
        return

    if cmd == "cycle":
        context.user_data["edit"] = "cycle"
        await plain(update, "Enter new cycle length (21-35):")
        return

    if cmd == "notify":
        context.user_data["edit"] = "notify"
        await plain(update, "Enter new notification time HH:MM (example: 09:00):")
        return

    if cmd == "period":
        context.user_data["edit"] = "period_start"
        await plain(update, "Enter NEW period START date YYYY-MM-DD:")
        return

    if cmd == "reset":
        delete_profile(cid)
        context.user_data["settings_mode"] = False
        await plain(update, "♻️ Reset done. Type /start to onboard again.")
        return

    # If editing a field
    edit = context.user_data.get("edit")
    if edit:
        text = update.message.text.strip()

        if edit == "name":
            if len(text) < 2:
                await plain(update, "⚠️ Too short. Enter a real name.")
                return
            prof["partner_name"] = text
            set_profile(cid, prof)
            context.user_data["edit"] = None
            await reply(update, "✅ Updated name.")
            await reply(update, today_card(prof, now), parse_mode="Markdown")
            return

        if edit == "dob":
            if text.lower() == "skip":
                prof["partner_dob"] = None
            else:
                d = parse_date_yyyy_mm_dd(text)
                if not d:
                    await plain(update, "⚠️ Invalid. Use YYYY-MM-DD or 'skip'.")
                    return
                prof["partner_dob"] = d.date().isoformat()
            set_profile(cid, prof)
            context.user_data["edit"] = None
            await reply(update, "✅ Updated DOB.")
            return

        if edit == "cycle":
            try:
                n = int(text)
                if not (21 <= n <= 35):
                    raise ValueError
            except Exception:
                await plain(update, "⚠️ Invalid. Enter 21-35.")
                return
            prof["cycle_length"] = n
            set_profile(cid, prof)
            context.user_data["edit"] = None
            await reply(update, "✅ Updated cycle length.")
            return

        if edit == "notify":
            tm = parse_time_hhmm(text)
            if not tm:
                await plain(update, "⚠️ Invalid time. Use HH:MM.")
                return
            prof["notify_hh"], prof["notify_mm"] = tm
            set_profile(cid, prof)
            context.user_data["edit"] = None
            await reply(update, "✅ Updated notification time.")
            return

        if edit == "period_start":
            d = parse_date_yyyy_mm_dd(text)
            if not d:
                await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD.")
                return
            prof["period_start"] = d.date().isoformat()
            context.user_data["edit"] = "period_end"
            set_profile(cid, prof)
            await plain(update, "Enter NEW period END date YYYY-MM-DD:")
            return

        if edit == "period_end":
            d = parse_date_yyyy_mm_dd(text)
            if not d:
                await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD.")
                return
            prof["period_end"] = d.date().isoformat()
            set_profile(cid, prof)
            context.user_data["edit"] = None
            await reply(update, "✅ Updated period dates.")
            await reply(update, today_card(prof, now), parse_mode="Markdown")
            return

    # Unknown setting command
    await reply(update, "⚠️ Unknown. Type: name / dob / cycle / notify / period / reset (or exit)")

# =========================
# NOTIFICATIONS (minute tick)
# =========================
async def tick(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    today = now.date().isoformat()

    for cid, prof in list(DATA.items()):
        if prof.get("paused"):
            continue

        # send at exact HH:MM (Stockholm time), once per day
        if now.hour == prof.get("notify_hh") and now.minute == prof.get("notify_mm"):
            if prof.get("last_sent") == today:
                continue

            try:
                await context.bot.send_message(
                    chat_id=int(cid),
                    text=today_card(prof, now),
                    parse_mode="Markdown",
                    reply_markup=menu_kb(),
                )
                prof["last_sent"] = today
                set_profile(cid, prof)
            except Exception as e:
                log.exception(f"Notify failed for {cid}: {e}")

# =========================
# MAIN
# =========================
def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_name)],
            ST_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_dob)],
            ST_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_period_start)],
            ST_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_period_end)],
            ST_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_cycle)],
            ST_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_notify)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(onboarding)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    # minute tick for notifications
    app.job_queue.run_repeating(tick, interval=60, first=5)

    return app

def main():
    log.info(f"🚀 Daycue boot v{VERSION}")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
