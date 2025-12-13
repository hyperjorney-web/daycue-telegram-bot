import os
import json
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

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
VERSION = "0.10.1"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TZ = ZoneInfo("Europe/Stockholm")
DATA_FILE = "data.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daycue")

# =========================
# FLY HTTP "KEEPALIVE" SERVER (so Fly sees port 8080 open)
# =========================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"OK daycue {VERSION}\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # silence default http logs
        return

def start_http_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"🌐 Health server listening on 0.0.0.0:{port}")

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
    await update.message.reply_text(text, parse_mode=parse_mode)

# =========================
# STORAGE
# =========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

DATA = load_data()

def cid(update: Update) -> str:
    return str(update.effective_chat.id)

def get_profile(chat_id: str):
    return DATA.get(chat_id)

def set_profile(chat_id: str, profile: dict):
    DATA[chat_id] = profile
    save_data(DATA)

def delete_profile(chat_id: str):
    if chat_id in DATA:
        del DATA[chat_id]
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
        tip = "Good time for planning + momentum."
    elif pct <= 0.60:
        phase = "Ovulation 🔥"
        energy = "High 🚀"
        mood = "Confident 😎"
        social = "High 🎉"
        cravings = "Low/Med 🥗"
        tip = "Best time for dates + honest talks."
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
    nh = f"{profile['notify_hh']:02d}:{profile['notify_mm']:02d}"

    return (
        f"📍 *Today*\n"
        f"👤 Partner: *{profile['partner_name']}*\n"
        f"📆 Cycle day: *{day}/{profile['cycle_length']}*\n"
        f"🧬 Phase: *{stats['phase']}*\n"
        f"⏰ Daily ping: *{nh}* (Stockholm)\n\n"
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
        d_wrapped = ((d - 1) % profile["cycle_length"]) + 1
        stats = phase_and_stats(d_wrapped, profile["cycle_length"])
        date_label = (now.date() + timedelta(days=i)).strftime("%a %d %b")
        lines.append(f"• {date_label} - Day {d_wrapped}: {stats['phase']} - {stats['energy']}")
    return "\n".join(lines)

# =========================
# STATES
# =========================
ST_NAME, ST_DOB, ST_START, ST_END, ST_CYCLE, ST_NOTIFY = range(6)

# =========================
# COMMANDS
# =========================
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🧩 Daycue bot version: v{VERSION}", reply_markup=menu_kb())

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = cid(update)
    prof = get_profile(chat)

    # Always show menu immediately on /start (even if onboarding needed)
    if prof:
        now = datetime.now(TZ)
        await reply(update, f"👋 Welcome back!\n\n{today_card(prof, now)}", parse_mode="Markdown")
        return ConversationHandler.END

    # Start onboarding
    context.user_data.clear()
    await plain(update,
        "👋 Welcome. Quick onboarding.\n\n"
        "1/6 - Enter partner name (example: Anna)"
    )
    return ST_NAME

# =========================
# ONBOARDING STEPS
# =========================
async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_menu_text(text) or len(text) < 2:
        await plain(update, "⚠️ Please type a name (example: Anna)")
        return ST_NAME

    context.user_data["partner_name"] = text
    await plain(update, "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return ST_DOB

async def on_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_menu_text(text):
        await plain(update, "⚠️ Still onboarding. Enter DOB or type 'skip'.")
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
    if is_menu_text(text):
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
    if is_menu_text(text):
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
    if is_menu_text(text):
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
    if is_menu_text(text):
        await plain(update, "⚠️ Still onboarding. Enter time as HH:MM (example: 09:00).")
        return ST_NOTIFY

    tm = parse_time_hhmm(text)
    if not tm:
        await plain(update, "⚠️ Invalid time. Use HH:MM (example: 09:00).")
        return ST_NOTIFY

    chat = cid(update)
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
    set_profile(chat, profile)

    now = datetime.now(TZ)
    await reply(update, "✅ Setup complete. Here is your status right now:")
    await reply(update, today_card(profile, now), parse_mode="Markdown")
    return ConversationHandler.END

# =========================
# MENU + SETTINGS
# =========================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = cid(update)
    prof = get_profile(chat)

    if not prof:
        await plain(update, "⚠️ Run /start first to onboard.")
        return

    now = datetime.now(TZ)
    text = update.message.text.strip()

    # Settings mode: user types commands (name/dob/cycle/notify/period/reset/exit)
    if context.user_data.get("settings_mode") and not is_menu_text(text):
        await handle_settings_text(update, context)
        return

    if text == BTN_TODAY:
        await reply(update, today_card(prof, now), parse_mode="Markdown")
        return

    if text == BTN_FORECAST:
        await reply(update, forecast_7(prof, now), parse_mode="Markdown")
        return

    if text == BTN_SETTINGS:
        context.user_data["settings_mode"] = True
        context.user_data["edit"] = None
        await reply(update,
            "⚙️ *Settings*\n"
            "Type one of:\n"
            "• `name` - change partner name\n"
            "• `dob` - change partner DOB\n"
            "• `cycle` - change cycle length\n"
            "• `notify` - change notification time\n"
            "• `period` - add/correct period dates\n"
            "• `reset` - delete profile\n"
            "• `exit` - close settings\n",
            parse_mode="Markdown"
        )
        return

    # fallback
    await reply(update, "Tap a button: 📍 Today / 🧭 Forecast / ⚙️ Settings")

async def handle_settings_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = cid(update)
    prof = get_profile(chat)
    now = datetime.now(TZ)

    cmd = update.message.text.strip().lower()

    if cmd in ("exit", "close"):
        context.user_data["settings_mode"] = False
        context.user_data["edit"] = None
        await reply(update, "✅ Closed settings.")
        return

    if cmd == "reset":
        delete_profile(chat)
        context.user_data["settings_mode"] = False
        context.user_data["edit"] = None
        await plain(update, "♻️ Reset done. Type /start to onboard again.")
        return

    if cmd in ("name", "dob", "cycle", "notify", "period"):
        context.user_data["edit"] = cmd
        if cmd == "name":
            await plain(update, "Enter new partner name:")
        elif cmd == "dob":
            await plain(update, "Enter DOB YYYY-MM-DD or type 'skip':")
        elif cmd == "cycle":
            await plain(update, "Enter new cycle length (21-35):")
        elif cmd == "notify":
            await plain(update, "Enter new notification time HH:MM (example: 09:00):")
        elif cmd == "period":
            context.user_data["period_step"] = "start"
            await plain(update, "Enter NEW period START date YYYY-MM-DD:")
        return

    edit = context.user_data.get("edit")
    text = update.message.text.strip()

    if edit == "name":
        if len(text) < 2:
            await plain(update, "⚠️ Too short. Enter a real name.")
            return
        prof["partner_name"] = text
        set_profile(chat, prof)
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
        set_profile(chat, prof)
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
        set_profile(chat, prof)
        context.user_data["edit"] = None
        await reply(update, "✅ Updated cycle length.")
        return

    if edit == "notify":
        tm = parse_time_hhmm(text)
        if not tm:
            await plain(update, "⚠️ Invalid time. Use HH:MM.")
            return
        prof["notify_hh"], prof["notify_mm"] = tm
        set_profile(chat, prof)
        context.user_data["edit"] = None
        await reply(update, "✅ Updated notification time.")
        return

    if edit == "period":
        step = context.user_data.get("period_step", "start")
        d = parse_date_yyyy_mm_dd(text)
        if not d:
            await plain(update, "⚠️ Invalid date. Use YYYY-MM-DD.")
            return
        if step == "start":
            prof["period_start"] = d.date().isoformat()
            set_profile(chat, prof)
            context.user_data["period_step"] = "end"
            await plain(update, "Enter NEW period END date YYYY-MM-DD:")
            return
        else:
            prof["period_end"] = d.date().isoformat()
            set_profile(chat, prof)
            context.user_data["edit"] = None
            context.user_data["period_step"] = "start"
            await reply(update, "✅ Updated period dates.")
            await reply(update, today_card(prof, now), parse_mode="Markdown")
            return

    await reply(update, "⚠️ Unknown. Type: name / dob / cycle / notify / period / reset / exit")

# =========================
# NOTIFICATIONS LOOP (no JobQueue needed)
# =========================
async def notifications_loop(app: Application):
    await asyncio.sleep(3)
    log.info("🔔 Notifications loop started")

    while True:
        try:
            now = datetime.now(TZ)
            today = now.date().isoformat()

            for chat, prof in list(DATA.items()):
                if prof.get("paused"):
                    continue

                # send once/day at HH:MM Stockholm time
                if now.hour == prof.get("notify_hh") and now.minute == prof.get("notify_mm"):
                    if prof.get("last_sent") == today:
                        continue

                    try:
                        await app.bot.send_message(
                            chat_id=int(chat),
                            text=today_card(prof, now),
                            parse_mode="Markdown",
                            reply_markup=menu_kb(),
                        )
                        prof["last_sent"] = today
                        set_profile(chat, prof)
                    except Exception as e:
                        log.exception(f"Notify failed for {chat}: {e}")

        except Exception as e:
            log.exception(f"Loop error: {e}")

        await asyncio.sleep(30)

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

    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(onboarding)

    # menu/messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    return app

def main():
    log.info(f"🚀 Daycue boot v{VERSION}")

    # Fly expects a port -> keepalive server
    start_http_server()

    app = build_app()

    # Start notification loop without JobQueue
    async def _post_init(application: Application):
        application.create_task(notifications_loop(application))

    app.post_init = _post_init

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
