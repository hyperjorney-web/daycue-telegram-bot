#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -----------------------------
# Config
# -----------------------------
VERSION = "0.9.0"
APP_NAME = "Daycue"
DEFAULT_TZ = os.getenv("DAYCUE_TZ", "Europe/Stockholm")  # set in Fly secrets/env if you want
TZ = ZoneInfo(DEFAULT_TZ)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN is missing")

# Fly volume-friendly path:
DATA_PATH = os.getenv("DAYCUE_DATA_PATH", "/data/daycue.json")
if not DATA_PATH.startswith("/"):
    # safety: keep it absolute
    DATA_PATH = "/data/daycue.json"

# fallback if /data not mounted
FALLBACK_DATA_PATH = "/tmp/daycue.json"

HEALTH_PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("daycue-bot")

# -----------------------------
# UI strings / menu buttons
# -----------------------------
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🧭 Forecast"
BTN_SETTINGS = "⚙️ Settings"
BTN_SEND_NOW = "🔔 Send now"

BTN_PAUSE = "⏸ Pause"
BTN_RESUME = "▶️ Resume"
BTN_RESET = "🧨 Reset"
BTN_BACK = "↩️ Back"

BTN_UPDATE_CYCLE = "🩸 Update cycle"
BTN_UPDATE_NOTIFY = "⏰ Update notify time"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_FORECAST], [BTN_SEND_NOW, BTN_SETTINGS]],
    resize_keyboard=True,
    is_persistent=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [[BTN_UPDATE_CYCLE, BTN_UPDATE_NOTIFY], [BTN_PAUSE, BTN_RESUME], [BTN_RESET, BTN_BACK]],
    resize_keyboard=True,
    is_persistent=True,
)

# -----------------------------
# Storage model
# -----------------------------
@dataclass
class Profile:
    chat_id: int
    partner_name: str
    partner_dob: Optional[str]  # YYYY-MM-DD or None
    period_start: str           # YYYY-MM-DD
    period_end: str             # YYYY-MM-DD
    cycle_length: int           # 21-35
    notify_time: str            # HH:MM (local TZ)
    paused: bool = False
    created_at: str = ""
    updated_at: str = ""

    def ensure_timestamps(self):
        now = datetime.now(TZ).isoformat(timespec="seconds")
        if not self.created_at:
            self.created_at = now
        self.updated_at = now


PROFILES: Dict[int, Profile] = {}


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.error("Failed to read JSON %s: %s", path, e)
        return {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def storage_path() -> str:
    # Prefer /data if volume exists
    if os.path.isdir("/data"):
        return DATA_PATH
    return FALLBACK_DATA_PATH


def load_profiles() -> None:
    global PROFILES
    data = _read_json(storage_path())
    profiles_raw = data.get("profiles", {})
    out: Dict[int, Profile] = {}
    for k, v in profiles_raw.items():
        try:
            chat_id = int(k)
            out[chat_id] = Profile(**v)
        except Exception as e:
            logger.warning("Skipping bad profile %s: %s", k, e)
    PROFILES = out
    logger.info("Loaded %d profiles from %s", len(PROFILES), storage_path())


def save_profiles() -> None:
    payload = {
        "version": VERSION,
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "profiles": {str(cid): asdict(p) for cid, p in PROFILES.items()},
    }
    _write_json(storage_path(), payload)


def get_profile(chat_id: int) -> Optional[Profile]:
    return PROFILES.get(chat_id)


def set_profile(p: Profile) -> None:
    p.ensure_timestamps()
    PROFILES[p.chat_id] = p
    save_profiles()


def delete_profile(chat_id: int) -> None:
    if chat_id in PROFILES:
        del PROFILES[chat_id]
        save_profiles()

# -----------------------------
# Helpers: parsing / calculations
# -----------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def parse_date(s: str) -> Optional[date]:
    s = s.strip()
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def parse_time_hhmm(s: str) -> Optional[dtime]:
    s = s.strip()
    if not TIME_RE.match(s):
        return None
    try:
        hh, mm = map(int, s.split(":"))
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return None
        return dtime(hour=hh, minute=mm, tzinfo=TZ)  # tz-aware time (important)
    except Exception:
        return None


def clamp_cycle_len(n: int) -> Optional[int]:
    if 21 <= n <= 35:
        return n
    return None


def cycle_day_number(p: Profile, today: date) -> int:
    start = parse_date(p.period_start) or today
    # day 1 = start date
    delta = (today - start).days
    day = (delta % p.cycle_length) + 1
    return day


def phase_from_day(day: int, cycle_len: int) -> str:
    # Simple MVP heuristic:
    # 1-5: Menstrual
    # 6-13: Follicular
    # 14-16: Ovulation
    # 17-cycle_len: Luteal
    if day <= 5:
        return "Menstrual"
    if day <= 13:
        return "Follicular"
    if day <= 16:
        return "Ovulation"
    return "Luteal"


def stats_for_phase(phase: str) -> Dict[str, str]:
    # Gamified emoji stats. Keep it simple and readable.
    if phase == "Menstrual":
        return {
            "🧠 Mood stability": "Low",
            "🫂 Social drive": "Low",
            "💛 Emotional needs": "Comfort + patience",
            "😬 Anxiety": "Medium",
            "🌶 Irritability": "Medium",
            "🍫 Cravings": "High (warm + sweet)",
            "🔥 Sexual drive": "Low",
            "🎯 Cognitive focus": "Low/Medium",
            "⚡ Energy": "Low",
        }
    if phase == "Follicular":
        return {
            "🧠 Mood stability": "High",
            "🫂 Social drive": "High",
            "💛 Emotional needs": "Encouragement + fun",
            "😬 Anxiety": "Low",
            "🌶 Irritability": "Low",
            "🍓 Cravings": "Low/Medium",
            "🔥 Sexual drive": "Medium",
            "🎯 Cognitive focus": "High",
            "⚡ Energy": "High",
        }
    if phase == "Ovulation":
        return {
            "🧠 Mood stability": "High",
            "🫂 Social drive": "Very high",
            "💛 Emotional needs": "Attention + flirting",
            "😬 Anxiety": "Low",
            "🌶 Irritability": "Low",
            "🍓 Cravings": "Low",
            "🔥 Sexual drive": "High",
            "🎯 Cognitive focus": "High",
            "⚡ Energy": "High",
        }
    # Luteal
    return {
        "🧠 Mood stability": "Medium",
        "🫂 Social drive": "Medium/Low",
        "💛 Emotional needs": "Clarity + reassurance",
        "😬 Anxiety": "Medium",
        "🌶 Irritability": "Medium/High",
        "🍟 Cravings": "Medium/High (salty + sweet)",
        "🔥 Sexual drive": "Low/Medium",
        "🎯 Cognitive focus": "Medium",
        "⚡ Energy": "Medium/Low",
    }


def actions_for_phase(phase: str) -> list[str]:
    if phase == "Menstrual":
        return [
            "🫖 Keep plans light. Offer help + warmth.",
            "❓ Ask: comfort or space?",
            "🍲 Food: soup, tea, chocolate, cozy dinner.",
        ]
    if phase == "Follicular":
        return [
            "🚀 Plan active stuff: walks, projects, social time.",
            "🎉 Praise progress. Invite collaboration.",
            "🥗 Food: lighter meals, fresh flavors.",
        ]
    if phase == "Ovulation":
        return [
            "💃 Great for dates, social events, big conversations.",
            "🌟 Compliments land well today.",
            "🥑 Food: balanced + protein; keep hydration up.",
        ]
    return [
        "🧯 Reduce friction: fewer surprises, more clarity.",
        "🧡 Validate feelings, don’t “fix” too fast.",
        "😴 Prioritize sleep + calm evenings.",
    ]


def forecast_text(p: Profile, days_ahead: int = 7) -> str:
    today = datetime.now(TZ).date()
    lines = [f"🧭 Forecast for {p.partner_name} (next {days_ahead} days)\n"]
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        dayn = cycle_day_number(p, d)
        phase = phase_from_day(dayn, p.cycle_length)
        tag = {"Menstrual": "🩸", "Follicular": "🌿", "Ovulation": "✨", "Luteal": "🌙"}.get(phase, "📌")
        lines.append(f"{tag} {d.isoformat()} - Day {dayn}/{p.cycle_length} - {phase}")
    return "\n".join(lines)


def today_status_text(p: Profile) -> str:
    today = datetime.now(TZ).date()
    dayn = cycle_day_number(p, today)
    phase = phase_from_day(dayn, p.cycle_length)

    phase_emoji = {"Menstrual": "🩸", "Follicular": "🌿", "Ovulation": "✨", "Luteal": "🌙"}.get(phase, "📌")

    stats = stats_for_phase(phase)
    actions = actions_for_phase(phase)

    lines = [
        "✅ Welcome back.\n",
        f"👤 Partner: {p.partner_name}",
        f"{phase_emoji} Day {dayn}/{p.cycle_length} - {phase}",
        "",
        "📊 Stats",
    ]
    for k, v in stats.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "🎯 Recommended actions"]
    for a in actions:
        lines.append(f"- {a}")
    if p.paused:
        lines += ["", "⏸ Notifications: PAUSED"]
    else:
        lines += ["", f"🔔 Daily notify time: {p.notify_time} ({DEFAULT_TZ})"]
    return "\n".join(lines)

# -----------------------------
# Jobs (notifications)
# -----------------------------
def job_name(chat_id: int) -> str:
    return f"daily:{chat_id}"


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=MAIN_MENU)


async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def remove_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=ReplyKeyboardRemove())


def upsert_daily_job(app: Application, p: Profile) -> None:
    for j in app.job_queue.get_jobs_by_name(job_name(p.chat_id)):
        j.schedule_removal()

    if p.paused:
        return

    t = parse_time_hhmm(p.notify_time)
    if not t:
        return

    app.job_queue.run_daily(
        callback=daily_notify_job,
        time=t,
        days=(0, 1, 2, 3, 4, 5, 6),
        name=job_name(p.chat_id),
        chat_id=p.chat_id,
    )
    logger.info("Scheduled daily job for chat_id=%s at %s (%s)", p.chat_id, p.notify_time, DEFAULT_TZ)


async def daily_notify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    p = get_profile(int(chat_id)) if chat_id is not None else None
    if not p or p.paused:
        return
    text = "🔔 Daily check-in\n\n" + today_status_text(p)
    await context.bot.send_message(chat_id=p.chat_id, text=text, reply_markup=MAIN_MENU)

# -----------------------------
# Conversation states
# -----------------------------
(
    ONB_NAME,
    ONB_DOB,
    ONB_START,
    ONB_END,
    ONB_LEN,
    ONB_TIME,
    SETTINGS_ROOT,
    SET_CYCLE_START,
    SET_CYCLE_END,
    SET_CYCLE_LEN,
    SET_NOTIFY_TIME,
) = range(11)

# -----------------------------
# Handlers: commands
# -----------------------------
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(update, context, "🏓 Pong! Bot is alive.", reply_markup=MAIN_MENU)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(update, context, f"🧩 {APP_NAME} bot version: v{VERSION}", reply_markup=MAIN_MENU)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = get_profile(update.effective_chat.id) if update.effective_chat else None
    if p:
        await send_text(update, context, "📱 Menu ready.", reply_markup=MAIN_MENU)
    else:
        await send_text(update, context, "📱 Menu ready. Run /start to onboard.", reply_markup=MAIN_MENU)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)

    if p:
        upsert_daily_job(context.application, p)
        await send_text(update, context, today_status_text(p), reply_markup=MAIN_MENU)
        return ConversationHandler.END

    await remove_keyboard(update, context, "Welcome. Quick onboarding.\n\n1/6 - Enter partner nickname (example: Anna)")
    context.user_data["onb"] = {}
    return ONB_NAME


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    delete_profile(chat_id)
    for j in context.application.job_queue.get_jobs_by_name(job_name(chat_id)):
        j.schedule_removal()
    await send_text(update, context, "🧨 Reset done. Run /start to onboard again.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# -----------------------------
# Onboarding flow
# -----------------------------
async def onb_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await send_text(update, context, "⚠️ Nickname too short. Try again (example: Anna).")
        return ONB_NAME

    context.user_data["onb"]["partner_name"] = name
    await send_text(update, context, "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return ONB_DOB


async def onb_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip().lower()
    if raw == "skip":
        context.user_data["onb"]["partner_dob"] = None
        await send_text(update, context, "3/6 - Last period START date (YYYY-MM-DD)")
        return ONB_START

    d = parse_date(raw)
    if not d:
        await send_text(update, context, "⚠️ Invalid date. Use YYYY-MM-DD or type 'skip'.")
        return ONB_DOB

    context.user_data["onb"]["partner_dob"] = d.isoformat()
    await send_text(update, context, "3/6 - Last period START date (YYYY-MM-DD)")
    return ONB_START


async def onb_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await send_text(update, context, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return ONB_START

    context.user_data["onb"]["period_start"] = d.isoformat()
    await send_text(update, context, "4/6 - Last period END date (YYYY-MM-DD)")
    return ONB_END


async def onb_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await send_text(update, context, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return ONB_END

    start = parse_date(context.user_data["onb"]["period_start"]) or d
    if d < start:
        await send_text(update, context, "⚠️ End date can’t be before start date. Enter again (YYYY-MM-DD).")
        return ONB_END

    context.user_data["onb"]["period_end"] = d.isoformat()
    await send_text(update, context, "5/6 - Cycle length in days (21-35). Example: 28")
    return ONB_LEN


async def onb_len(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await send_text(update, context, "⚠️ Please enter a number between 21 and 35.")
        return ONB_LEN

    n = clamp_cycle_len(int(raw))
    if not n:
        await send_text(update, context, "⚠️ Cycle length must be 21-35. Try again.")
        return ONB_LEN

    context.user_data["onb"]["cycle_length"] = n
    await send_text(update, context, "6/6 - Daily notification time (HH:MM). Example: 09:00")
    return ONB_TIME


async def onb_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_time_hhmm(update.message.text or "")
    if not t:
        await send_text(update, context, "⚠️ Invalid time. Use HH:MM (example: 09:00).")
        return ONB_TIME

    chat_id = update.effective_chat.id
    onb = context.user_data.get("onb", {})

    p = Profile(
        chat_id=chat_id,
        partner_name=onb["partner_name"],
        partner_dob=onb.get("partner_dob"),
        period_start=onb["period_start"],
        period_end=onb["period_end"],
        cycle_length=int(onb["cycle_length"]),
        notify_time=t.strftime("%H:%M"),
        paused=False,
    )
    set_profile(p)
    upsert_daily_job(context.application, p)

    await send_text(
        update,
        context,
        "✅ Setup complete!\n\nYour menu is ready below. Use 🔔 Send now to test instantly.",
        reply_markup=MAIN_MENU,
    )
    await send_text(update, context, today_status_text(p), reply_markup=MAIN_MENU)
    return ConversationHandler.END

# -----------------------------
# Menu (button) routing
# -----------------------------
async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)

    if not p:
        await send_text(update, context, "👋 Run /start to onboard first.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_TODAY:
        await send_text(update, context, today_status_text(p), reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_FORECAST:
        await send_text(update, context, forecast_text(p, 7), reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_SEND_NOW:
        if p.paused:
            await send_text(update, context, "⏸ Notifications are paused. Tap ▶️ Resume in Settings.", reply_markup=MAIN_MENU)
            return ConversationHandler.END

        await send_text(update, context, "🔔 Sending now...\n\n" + today_status_text(p), reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_SETTINGS:
        await send_text(update, context, "⚙️ Settings", reply_markup=SETTINGS_MENU)
        return SETTINGS_ROOT

    if text == BTN_PAUSE:
        p.paused = True
        set_profile(p)
        upsert_daily_job(context.application, p)
        await send_text(update, context, "⏸ Paused. No daily notifications will be sent.", reply_markup=SETTINGS_MENU)
        return SETTINGS_ROOT

    if text == BTN_RESUME:
        p.paused = False
        set_profile(p)
        upsert_daily_job(context.application, p)
        await send_text(update, context, "▶️ Resumed. Daily notifications are ON.", reply_markup=SETTINGS_MENU)
        return SETTINGS_ROOT

    if text == BTN_RESET:
        delete_profile(chat_id)
        for j in context.application.job_queue.get_jobs_by_name(job_name(chat_id)):
            j.schedule_removal()
        await send_text(update, context, "🧨 Reset done. Run /start to onboard again.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_BACK:
        await send_text(update, context, "📍 Back to main menu.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == BTN_UPDATE_CYCLE:
        await send_text(update, context, "🩸 Update cycle\n\nEnter last period START date (YYYY-MM-DD)", reply_markup=ReplyKeyboardRemove())
        return SET_CYCLE_START

    if text == BTN_UPDATE_NOTIFY:
        await send_text(update, context, "⏰ Update notify time\n\nEnter time (HH:MM)", reply_markup=ReplyKeyboardRemove())
        return SET_NOTIFY_TIME

    await send_text(update, context, "🤖 Use the buttons below.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# -----------------------------
# Settings edit flows
# -----------------------------
async def set_cycle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await send_text(update, context, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return SET_CYCLE_START
    context.user_data["set_cycle_start"] = d.isoformat()
    await send_text(update, context, "Enter last period END date (YYYY-MM-DD)")
    return SET_CYCLE_END


async def set_cycle_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await send_text(update, context, "⚠️ Invalid date. Use YYYY-MM-DD.")
        return SET_CYCLE_END

    start = parse_date(context.user_data.get("set_cycle_start", "")) or d
    if d < start:
        await send_text(update, context, "⚠️ End date can’t be before start date. Enter again.")
        return SET_CYCLE_END

    context.user_data["set_cycle_end"] = d.isoformat()
    await send_text(update, context, "Enter cycle length (21-35)")
    return SET_CYCLE_LEN


async def set_cycle_len(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await send_text(update, context, "⚠️ Please enter a number 21-35.")
        return SET_CYCLE_LEN

    n = clamp_cycle_len(int(raw))
    if not n:
        await send_text(update, context, "⚠️ Cycle length must be 21-35.")
        return SET_CYCLE_LEN

    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await send_text(update, context, "Run /start first.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    p.period_start = context.user_data.get("set_cycle_start", p.period_start)
    p.period_end = context.user_data.get("set_cycle_end", p.period_end)
    p.cycle_length = n
    set_profile(p)

    await send_text(update, context, "✅ Cycle updated.", reply_markup=SETTINGS_MENU)
    return SETTINGS_ROOT


async def set_notify_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_time_hhmm(update.message.text or "")
    if not t:
        await send_text(update, context, "⚠️ Invalid time. Use HH:MM (example 09:00).")
        return SET_NOTIFY_TIME

    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await send_text(update, context, "Run /start first.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    p.notify_time = t.strftime("%H:%M")
    set_profile(p)
    upsert_daily_job(context.application, p)

    await send_text(update, context, f"✅ Notify time updated to {p.notify_time} ({DEFAULT_TZ}).", reply_markup=SETTINGS_MENU)
    return SETTINGS_ROOT


async def cancel_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_text(update, context, "👌 Cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# -----------------------------
# Error handler
# -----------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)

# -----------------------------
# Tiny health server for Fly
# -----------------------------
async def health_server() -> None:
    import asyncio

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            _ = await reader.read(1024)
            body = b"ok"
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(resp)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host="0.0.0.0", port=HEALTH_PORT)
    logger.info("Health server listening on 0.0.0.0:%s", HEALTH_PORT)
    async with server:
        await server.serve_forever()

# -----------------------------
# Main
# -----------------------------
def build_app() -> Application:
    load_profiles()

    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(on_error)

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ONB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_name)],
            ONB_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_dob)],
            ONB_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_start)],
            ONB_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_end)],
            ONB_LEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_len)],
            ONB_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_time)],

            SETTINGS_ROOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons)],
            SET_CYCLE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cycle_start)],
            SET_CYCLE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cycle_end)],
            SET_CYCLE_LEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cycle_len)],
            SET_NOTIFY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_notify_time)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_any),
            CommandHandler("reset", cmd_reset),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    for p in PROFILES.values():
        upsert_daily_job(app, p)

    return app


def main() -> None:
    logger.info("BOOT: starting bot.py")
    app = build_app()

    enable_health = os.getenv("DAYCUE_ENABLE_HEALTH", "1") == "1"
    if enable_health:
        async def _post_init(application: Application):
            import asyncio
            asyncio.create_task(health_server())
        app.post_init = _post_init

    app.run_polling(
        close_loop=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
