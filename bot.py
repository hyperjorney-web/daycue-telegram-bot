#!/usr/bin/env python3
# Daycue v1.0-beta-local (non-DB baseline)
# - Always-on menu (Today / Prognos / About / Settings)
# - 5-step onboarding
# - Daily notification by time (simple async loop, no APScheduler, no PTB JobQueue)
# - In-memory storage (will be replaced by DB later)

import os
import re
import sys
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

VERSION = "1.0.0-beta-local"
TZ = ZoneInfo("Europe/Stockholm")
LOG = logging.getLogger("daycue")

COPY = {
    "welcome": "Welcome to Daycue.\n\nQuick onboarding (5 steps).",
    "q1_name": "1/5 - Enter partner nickname (example: Alona)",
    "q2_start": "2/5 - Last period START date (YYYY-MM-DD). Example: 2025-12-09",
    "q3_end": "3/5 - Last period END date (YYYY-MM-DD). Example: 2025-12-13",
    "q4_cycle": "4/5 - Cycle length in days (21-35). Example: 31",
    "q5_time": "5/5 - Daily notification time (HH:MM). Example: 09:00",
    "onboarding_done": "✅ Setup saved.\n\nHere’s your status for today:",
    "menu_hint": "Use the menu anytime: Today • Prognos • About • Settings",
    "settings_intro": "Settings:\n• Update dates\n• Update cycle length\n• Update notification time\n• Pause/resume notifications\n• Reset onboarding",
    "settings_choose": "Type one:\n- dates\n- cycle\n- time\n- pause\n- resume\n- reset\n- back",
    "invalid": "That doesn’t look right. Try again.",
}

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📍 Today"), KeyboardButton("🔮 Prognos")],
        [KeyboardButton("ℹ️ About"), KeyboardButton("⚙️ Settings")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

@dataclass
class Profile:
    chat_id: int
    partner_name: str
    period_start: str
    period_end: str
    cycle_length: int
    notify_time: str
    paused: bool = False
    last_sent_local_date: Optional[str] = None

PROFILES: Dict[int, Profile] = {}
SETTINGS_MODE: Dict[int, str] = {}

def _today_local() -> date:
    return datetime.now(TZ).date()

def _now_local() -> datetime:
    return datetime.now(TZ)

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def parse_hm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def cycle_day(profile: Profile, on_date: Optional[date] = None) -> int:
    if on_date is None:
        on_date = _today_local()
    start = parse_ymd(profile.period_start)
    delta = (on_date - start).days
    return (delta % profile.cycle_length) + 1

def period_length(profile: Profile) -> int:
    s = parse_ymd(profile.period_start)
    e = parse_ymd(profile.period_end)
    return max(1, (e - s).days + 1)

def phase_for_day(profile: Profile, cd: int) -> Tuple[str, int, int, str]:
    cl = profile.cycle_length
    pl = clamp(period_length(profile), 1, 10)
    ov = clamp(cl - 14, 10, cl - 5)
    ovu_len = 3
    ovu_start = clamp(ov - 1, pl + 1, cl - 2)
    ovu_end = ovu_start + ovu_len - 1

    if cd <= pl:
        return ("Menstrual", cd, pl, "🩸")
    if cd < ovu_start:
        fl_start = pl + 1
        fl_len = max(1, (ovu_start - 1) - fl_start + 1)
        return ("Follicular", cd - fl_start + 1, fl_len, "🌱")
    if ovu_start <= cd <= ovu_end:
        return ("Ovulatory", cd - ovu_start + 1, ovu_len, "🔥")
    lu_start = ovu_end + 1
    lu_len = max(1, cl - lu_start + 1)
    return ("Luteal", cd - lu_start + 1, lu_len, "🌙")

def hormone_hint(phase: str) -> str:
    if phase == "Menstrual":
        return "Hormones: estrogen ↘, progesterone ↘ (low baseline)"
    if phase == "Follicular":
        return "Hormones: estrogen ↗, progesterone low"
    if phase == "Ovulatory":
        return "Hormones: estrogen peak, LH surge, testosterone slight ↗"
    return "Hormones: progesterone ↗ then ↘, estrogen moderate then ↘"

def trend_arrow(phase: str, metric: str, phase_progress: float) -> str:
    if phase == "Menstrual":
        return {"Energy":"↘","Mood":"↘","Social":"↘","Cravings":"↗"}.get(metric,"→")
    if phase == "Follicular":
        return {"Energy":"↗","Mood":"↗","Social":"↗","Cravings":"↘"}.get(metric,"→")
    if phase == "Ovulatory":
        return {"Energy":"↗","Mood":"↗","Social":"↗","Cravings":"→"}.get(metric,"→")
    if metric in ("Energy","Mood","Social"):
        return "→" if phase_progress < 0.5 else "↘"
    if metric == "Cravings":
        return "→" if phase_progress < 0.5 else "↗"
    return "→"

def level_for_metric(phase: str, metric: str, phase_progress: float) -> str:
    if phase == "Menstrual":
        return {"Energy":"Low","Mood":"Sensitive","Social":"Low","Cravings":"High"}.get(metric,"—")
    if phase == "Follicular":
        return {"Energy":"Rising","Mood":"Bright","Social":"High","Cravings":"Low"}.get(metric,"—")
    if phase == "Ovulatory":
        return {"Energy":"Peak","Mood":"Confident","Social":"Very high","Cravings":"Medium"}.get(metric,"—")
    if phase_progress < 0.5:
        levels = {"Energy":"Stable","Mood":"Balanced","Social":"Medium","Cravings":"Medium"}
    else:
        levels = {"Energy":"Lowering","Mood":"Sensitive","Social":"Low","Cravings":"High"}
    return levels.get(metric,"—")

def help_text(phase: str) -> str:
    if phase == "Menstrual":
        return "• Warmth + patience\n• Keep plans light\n• Offer practical care (tea, cozy food)\n• Don’t push for big talks unless she initiates"
    if phase == "Follicular":
        return "• Encourage plans + movement\n• Do something new together\n• Celebrate momentum (small wins)\n• Be playful and present"
    if phase == "Ovulatory":
        return "• Compliments land well\n• Prioritize closeness + connection\n• Great time for date night\n• Deeper conversations often feel easier"
    return "• Be steady and reassuring\n• Lower friction: fewer surprises\n• Give space if she asks\n• Comfort rituals (walks, calm dinner)"

def phase_about(phase: str) -> str:
    if phase == "Menstrual":
        return "🩸 *Menstrual phase*\n- Often lower energy and more sensitivity\n- Body is resetting; comfort matters\n- Good moves: calm support, help with logistics"
    if phase == "Follicular":
        return "🌱 *Follicular phase*\n- Energy and motivation tend to rise\n- Great for planning, social life, new experiences\n- Good moves: encourage, join activities"
    if phase == "Ovulatory":
        return "🔥 *Ovulatory phase*\n- Peak energy/social drive for many\n- Often more open to connection + intimacy\n- Good moves: quality time, compliments, presence"
    return "🌙 *Luteal phase*\n- Can shift toward lower tolerance for stress\n- Comfort needs can increase; PMS can appear\n- Good moves: stability, kindness, reduce noise"

def today_card(profile: Profile, on_date: Optional[date] = None) -> str:
    if on_date is None:
        on_date = _today_local()
    cd = cycle_day(profile, on_date)
    phase, pd, plen, emo = phase_for_day(profile, cd)
    prog = 0.0 if plen <= 1 else (pd - 1) / (plen - 1)

    metrics = [("Energy","⚡"),("Mood","🎭"),("Social","🗣️"),("Cravings","🍩")]
    lines = [
        f"*TODAY: {profile.partner_name}*",
        f"Cycle day: *{cd}/{profile.cycle_length}*",
        f"Phase: *{phase} ({pd}/{plen})* {emo}",
        f"Daily ping: *{profile.notify_time}* (Stockholm)",
        "",
        "*STATS:*",
    ]
    for m, icon in metrics:
        lines.append(f"{icon} {m}: *{level_for_metric(phase,m,prog)}* {trend_arrow(phase,m,prog)}")

    lines += [
        "",
        "🫶 *How to help*",
        help_text(phase),
        "",
        f"🧬 _{hormone_hint(phase)}_",
        f"📅 _Local date: {on_date.isoformat()}_",
    ]
    return "\n".join(lines)

def forecast_week(profile: Profile) -> str:
    start = _today_local()
    items = []
    for i in range(7):
        d = start + timedelta(days=i)
        cd = cycle_day(profile, d)
        phase, _, _, emo = phase_for_day(profile, cd)
        items.append(f"{d.strftime('%a %d %b')}: {phase} {emo} (day {cd}/{profile.cycle_length})")
    return "*7-day forecast*\n" + "\n".join(items)

async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if update.message:
        await update.message.reply_text(text, reply_markup=MENU, parse_mode=ParseMode.MARKDOWN)

def get_profile(chat_id: int) -> Optional[Profile]:
    return PROFILES.get(chat_id)

def upsert_profile(p: Profile):
    PROFILES[p.chat_id] = p

(ON_NAME, ON_START, ON_END, ON_CYCLE, ON_TIME) = range(5)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if p:
        await send_text(update, context, today_card(p) + "\n\n" + COPY["menu_hint"])
        return ConversationHandler.END
    await send_text(update, context, COPY["welcome"] + "\n\n" + COPY["q1_name"])
    return ON_NAME

async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name or len(name) > 32:
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q1_name"])
        return ON_NAME
    context.user_data["partner_name"] = name
    await send_text(update, context, COPY["q2_start"])
    return ON_START

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = (update.message.text or "").strip()
    if not DATE_RE.match(s):
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q2_start"])
        return ON_START
    try:
        parse_ymd(s)
    except Exception:
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q2_start"])
        return ON_START
    context.user_data["period_start"] = s
    await send_text(update, context, COPY["q3_end"])
    return ON_END

async def on_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = (update.message.text or "").strip()
    if not DATE_RE.match(s):
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q3_end"])
        return ON_END
    try:
        end = parse_ymd(s)
        start = parse_ymd(context.user_data["period_start"])
        if end < start:
            raise ValueError("end < start")
    except Exception:
        await send_text(update, (context), "End date must be the same or after start date.\n\n" + COPY["q3_end"])
        return ON_END
    context.user_data["period_end"] = s
    await send_text(update, context, COPY["q4_cycle"])
    return ON_CYCLE

async def on_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = (update.message.text or "").strip()
    if not s.isdigit():
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q4_cycle"])
        return ON_CYCLE
    cl = int(s)
    if cl < 21 or cl > 35:
        await send_text(update, context, "Cycle length must be 21-35.\n\n" + COPY["q4_cycle"])
        return ON_CYCLE
    context.user_data["cycle_length"] = cl
    await send_text(update, context, COPY["q5_time"])
    return ON_TIME

async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = (update.message.text or "").strip()
    if not TIME_RE.match(s):
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q5_time"])
        return ON_TIME
    try:
        parse_hm(s)
    except Exception:
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["q5_time"])
        return ON_TIME

    chat_id = update.effective_chat.id
    p = Profile(
        chat_id=chat_id,
        partner_name=context.user_data["partner_name"],
        period_start=context.user_data["period_start"],
        period_end=context.user_data["period_end"],
        cycle_length=context.user_data["cycle_length"],
        notify_time=s,
        paused=False,
        last_sent_local_date=None,
    )
    upsert_profile(p)
    await send_text(update, context, COPY["onboarding_done"] + "\n\n" + today_card(p) + "\n\n" + COPY["menu_hint"])
    return ConversationHandler.END

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    PROFILES.pop(chat_id, None)
    SETTINGS_MODE.pop(chat_id, None)
    context.user_data.clear()
    await send_text(update, context, "Reset done. Type /start to onboard again.")

async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_profile(update.effective_chat.id)
    if not p:
        await send_text(update, context, "Type /start to set up.")
        return
    await send_text(update, context, today_card(p))

async def show_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_profile(update.effective_chat.id)
    if not p:
        await send_text(update, context, "Type /start to set up.")
        return
    await send_text(update, context, forecast_week(p))

async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_profile(update.effective_chat.id)
    if not p:
        await send_text(update, context, "Type /start to set up.")
        return
    cd = cycle_day(p)
    phase, _, _, _ = phase_for_day(p, cd)
    await send_text(update, context, phase_about(phase))

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await send_text(update, context, "Type /start to set up.")
        return
    SETTINGS_MODE[chat_id] = "choose"
    await send_text(update, context, COPY["settings_intro"] + "\n\n" + COPY["settings_choose"])

async def handle_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await send_text(update, context, "Type /start to set up.")
        return

    mode = SETTINGS_MODE.get(chat_id)
    text = (update.message.text or "").strip().lower()

    if mode == "choose":
        if text in ("back", "menu"):
            SETTINGS_MODE.pop(chat_id, None)
            await send_text(update, context, "Back to menu.")
            return
        if text == "dates":
            SETTINGS_MODE[chat_id] = "dates_start"
            await send_text(update, context, "Enter new period START date (YYYY-MM-DD)")
            return
        if text == "cycle":
            SETTINGS_MODE[chat_id] = "cycle"
            await send_text(update, context, "Enter new cycle length (21-35)")
            return
        if text == "time":
            SETTINGS_MODE[chat_id] = "time"
            await send_text(update, context, "Enter new daily notification time (HH:MM)")
            return
        if text == "pause":
            p.paused = True
            upsert_profile(p)
            await send_text(update, context, "Notifications paused.")
            return
        if text == "resume":
            p.paused = False
            upsert_profile(p)
            await send_text(update, context, "Notifications resumed.")
            return
        if text == "reset":
            await cmd_reset(update, context)
            return
        await send_text(update, context, COPY["invalid"] + "\n\n" + COPY["settings_choose"])
        return

    if mode == "dates_start":
        if not DATE_RE.match(text):
            await send_text(update, context, COPY["invalid"] + "\nEnter new period START date (YYYY-MM-DD)")
            return
        try:
            parse_ymd(text)
        except Exception:
            await send_text(update, context, COPY["invalid"] + "\nEnter new period START date (YYYY-MM-DD)")
            return
        context.user_data["tmp_start"] = text
        SETTINGS_MODE[chat_id] = "dates_end"
        await send_text(update, context, "Enter new period END date (YYYY-MM-DD)")
        return

    if mode == "dates_end":
        if not DATE_RE.match(text):
            await send_text(update, context, COPY["invalid"] + "\nEnter new period END date (YYYY-MM-DD)")
            return
        try:
            end = parse_ymd(text)
            start = parse_ymd(context.user_data["tmp_start"])
            if end < start:
                raise ValueError("end < start")
        except Exception:
            await send_text(update, context, "End date must be same or after start.\nEnter new period END date (YYYY-MM-DD)")
            return
        p.period_start = context.user_data["tmp_start"]
        p.period_end = text
        p.last_sent_local_date = None
        upsert_profile(p)
        SETTINGS_MODE[chat_id] = "choose"
        await send_text(update, context, "✅ Dates updated.\n\n" + COPY["settings_choose"])
        return

    if mode == "cycle":
        if not text.isdigit():
            await send_text(update, context, COPY["invalid"] + "\nEnter new cycle length (21-35)")
            return
        cl = int(text)
        if cl < 21 or cl > 35:
            await send_text(update, context, "Cycle length must be 21-35.\nEnter new cycle length (21-35)")
            return
        p.cycle_length = cl
        p.last_sent_local_date = None
        upsert_profile(p)
        SETTINGS_MODE[chat_id] = "choose"
        await send_text(update, context, "✅ Cycle length updated.\n\n" + COPY["settings_choose"])
        return

    if mode == "time":
        if not TIME_RE.match(text):
            await send_text(update, context, COPY["invalid"] + "\nEnter new daily notification time (HH:MM)")
            return
        try:
            parse_hm(text)
        except Exception:
            await send_text(update, context, COPY["invalid"] + "\nEnter new daily notification time (HH:MM)")
            return
        p.notify_time = text
        p.last_sent_local_date = None
        upsert_profile(p)
        SETTINGS_MODE[chat_id] = "choose"
        await send_text(update, context, "✅ Notification time updated.\n\n" + COPY["settings_choose"])
        return

    SETTINGS_MODE[chat_id] = "choose"
    await send_text(update, context, COPY["settings_choose"])

async def notification_loop(app: Application):
    LOG.info("🔔 notification_loop started")
    while True:
        try:
            now = _now_local()
            now_hm = now.strftime("%H:%M")
            today_str = now.date().isoformat()

            for chat_id, p in list(PROFILES.items()):
                if p.paused:
                    continue
                if p.notify_time != now_hm:
                    continue
                if p.last_sent_local_date == today_str:
                    continue

                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=today_card(p, now.date()),
                        reply_markup=MENU,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    p.last_sent_local_date = today_str
                    upsert_profile(p)
                except Exception as e:
                    LOG.exception("Failed to send notification to %s: %s", chat_id, e)

        except Exception as loop_err:
            LOG.exception("notification_loop error: %s", loop_err)

        await asyncio.sleep(30)

async def http_health_server():
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8080"))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(1024)
            body = b"OK"
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(resp)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port)
    LOG.info("🌐 health server listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()

async def route_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    if SETTINGS_MODE.get(chat_id):
        await handle_settings_input(update, context)
        return

    if text.startswith("📍") or text.lower() == "today":
        await show_today(update, context); return
    if text.startswith("🔮") or text.lower() in ("prognos", "forecast"):
        await show_forecast(update, context); return
    if text.startswith("ℹ️") or text.lower() == "about":
        await show_about(update, context); return
    if text.startswith("⚙️") or text.lower() == "settings":
        await show_settings(update, context); return

    if get_profile(chat_id):
        await send_text(update, context, "Use the menu: Today • Prognos • About • Settings")
    else:
        await send_text(update, context, "Type /start to set up.")

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(update, context, f"🧩 Daycue bot version: v{VERSION}")

def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing")
        sys.exit(1)

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_name)],
            ON_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_start)],
            ON_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_end)],
            ON_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_cycle)],
            ON_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_time)],
        },
        fallbacks=[CommandHandler("reset", cmd_reset)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_menu))
    return app

async def post_init(app: Application):
    app.create_task(notification_loop(app))
    app.create_task(http_health_server())

def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    LOG.info("🚀 Daycue boot v%s", VERSION)
    app = build_app()
    app.post_init = post_init
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
