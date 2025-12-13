#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Daycue Telegram Bot (single-file MVP)
- python-telegram-bot v20+
- No sqlite, no APScheduler, no PTB JobQueue
- Daily notifications via asyncio background loop
- JSON persistence (optional) to survive restarts if a volume is mounted

ENV:
- TELEGRAM_BOT_TOKEN   (required)
- DATA_DIR             (optional, default: ./data) -> stores users.json if writable
- TZ_NAME              (optional, default: Europe/Stockholm)
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

VERSION = "0.11.1"

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("daycue")

# ---------- Timezone ----------
TZ_NAME = os.getenv("TZ_NAME", "Europe/Stockholm")
TZ = ZoneInfo(TZ_NAME)

# ---------- Persistence ----------
DATA_DIR = os.getenv("DATA_DIR", "./data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")


def _ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write_json(path: str, payload: dict) -> None:
    try:
        _ensure_data_dir()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Persistence write failed: %s", e)


# ---------- UI (menu) ----------
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🧭 Forecast"
BTN_ABOUT = "📘 About phase"
BTN_SETTINGS = "⚙️ Settings"

SET_NAME = "✏️ Name"
SET_DOB = "🎂 DOB"
SET_PSTART = "🩸 Period start"
SET_PEND = "🧻 Period end"
SET_CYCLE = "🔁 Cycle length"
SET_TIME = "⏰ Daily ping"
SET_PAUSE = "⏸ Pause"
SET_RESUME = "▶️ Resume"
SET_RESET = "🗑 Reset"


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_TODAY, BTN_FORECAST], [BTN_ABOUT, BTN_SETTINGS]],
        resize_keyboard=True,
    )


def settings_kb(paused: bool) -> ReplyKeyboardMarkup:
    row1 = [SET_NAME, SET_DOB]
    row2 = [SET_PSTART, SET_PEND]
    row3 = [SET_CYCLE, SET_TIME]
    row4 = [SET_RESUME if paused else SET_PAUSE, SET_RESET]
    return ReplyKeyboardMarkup([row1, row2, row3, row4, ["⬅️ Back"]], resize_keyboard=True)


def is_menu_text(text: str) -> bool:
    return text in {BTN_TODAY, BTN_FORECAST, BTN_ABOUT, BTN_SETTINGS}


# ---------- Data model ----------
@dataclass
class Profile:
    chat_id: int
    partner_name: str
    partner_dob: str  # YYYY-MM-DD or ""
    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD
    cycle_length: int  # 21-35
    notify_hh: int
    notify_mm: int
    paused: bool = False
    last_sent: str = ""  # YYYY-MM-DD

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        return Profile(
            chat_id=int(d["chat_id"]),
            partner_name=str(d.get("partner_name", "")),
            partner_dob=str(d.get("partner_dob", "")),
            period_start=str(d.get("period_start", "")),
            period_end=str(d.get("period_end", "")),
            cycle_length=int(d.get("cycle_length", 28)),
            notify_hh=int(d.get("notify_hh", 9)),
            notify_mm=int(d.get("notify_mm", 0)),
            paused=bool(d.get("paused", False)),
            last_sent=str(d.get("last_sent", "")),
        )


PROFILES: dict[int, Profile] = {}


def load_profiles() -> None:
    global PROFILES
    payload = _load_json(USERS_FILE)
    items = payload.get("profiles", {})
    out: dict[int, Profile] = {}
    for k, v in items.items():
        try:
            out[int(k)] = Profile.from_dict(v)
        except Exception:
            continue
    PROFILES = out
    log.info("Loaded %d profiles", len(PROFILES))


def save_profiles() -> None:
    payload = {"profiles": {str(k): v.to_dict() for k, v in PROFILES.items()}}
    _atomic_write_json(USERS_FILE, payload)


def get_profile(chat_id: int) -> Profile | None:
    return PROFILES.get(chat_id)


def upsert_profile(p: Profile) -> None:
    PROFILES[p.chat_id] = p
    save_profiles()


# ---------- Parsing (FIXED: accept 2025-12-9 + 9:00) ----------
DATE_FLEX_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
TIME_FLEX_RE = re.compile(r"^\d{1,2}:\d{2}$")


def normalize_date(s: str) -> str | None:
    s = s.strip()
    if not DATE_FLEX_RE.match(s):
        return None
    try:
        y, m, d = s.split("-")
        y_i = int(y)
        m_i = int(m)
        d_i = int(d)
        dt = date(y_i, m_i, d_i)  # validates
        return dt.isoformat()      # always YYYY-MM-DD
    except Exception:
        return None


def normalize_time(s: str) -> tuple[int, int] | None:
    s = s.strip()
    if not TIME_FLEX_RE.match(s):
        return None
    try:
        hh, mm = s.split(":")
        hh_i = int(hh)
        mm_i = int(mm)
        if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
            return hh_i, mm_i
        return None
    except Exception:
        return None


def clamp(n: int, a: int, b: int) -> int:
    return max(a, min(b, n))


# ---------- Cycle logic ----------
def menstrual_length(profile: Profile) -> int:
    s = profile.period_start
    e = profile.period_end
    try:
        sd = date.fromisoformat(s)
        ed = date.fromisoformat(e)
    except Exception:
        return 5
    n = (ed - sd).days + 1
    return clamp(n, 3, 8)


def cycle_day(profile: Profile, now: datetime) -> int:
    try:
        s = date.fromisoformat(profile.period_start)
    except Exception:
        return 1
    delta = (now.date() - s).days
    return (delta % profile.cycle_length) + 1


def trend_arrow(progress01: float) -> str:
    if progress01 < 0.34:
        return "↘"
    if progress01 < 0.67:
        return "→"
    return "↗"


def phase_model(day: int, cycle_len: int, mlen: int) -> dict:
    cycle_len = clamp(cycle_len, 21, 35)
    mlen = clamp(mlen, 3, 8)
    ov_len = 2
    lut_len = 12
    fol_len = cycle_len - (mlen + ov_len + lut_len)
    if fol_len < 6:
        take = 6 - fol_len
        fol_len = 6
        lut_len = max(8, lut_len - take)

    if day <= mlen:
        phase = "Menstrual"
        emoji = "🩸"
        in_phase = day
        phase_len = mlen
        hormones = "Estrogen low, progesterone low. Recovery + reset mode."
        help_more = (
            "Warmth + patience. Keep plans light.\n"
            "Offer practical help (food, errands, calm space).\n"
            "Ask: comfort or space? Keep conflicts for later."
        )
        stats = dict(energy="Low", mood="Sensitive", social="Low", cravings="High")
    elif day <= mlen + fol_len:
        phase = "Follicular"
        emoji = "🌱"
        in_phase = day - mlen
        phase_len = fol_len
        hormones = "Estrogen rising. Motivation + optimism increase."
        help_more = (
            "Great time to plan + start things.\n"
            "Encourage movement, playful dates, momentum.\n"
            "Clear communication usually improves."
        )
        stats = dict(energy="Rising", mood="Bright", social="Higher", cravings="Medium")
    elif day <= mlen + fol_len + ov_len:
        phase = "Ovulation"
        emoji = "🔥"
        in_phase = day - (mlen + fol_len)
        phase_len = ov_len
        hormones = "Estrogen peaks. Confidence + social drive high."
        help_more = (
            "Best window for dates + honest talks.\n"
            "Be direct, warm, and appreciative.\n"
            "Plan something social if she wants it."
        )
        stats = dict(energy="High", mood="Confident", social="High", cravings="Low/Med")
    else:
        phase = "Luteal"
        emoji = "🌙"
        in_phase = day - (mlen + fol_len + ov_len)
        phase_len = lut_len
        hormones = "Progesterone higher, then drops near the end. Sensitivity may rise."
        help_more = (
            "Reduce stress + decision load.\n"
            "Be patient with irritability; don’t take it personally.\n"
            "Food + rest helps. Keep expectations realistic."
        )
        stats = dict(energy="Medium", mood="Touchy", social="Medium", cravings="High")

    progress01 = in_phase / max(1, phase_len)
    arrows = {k: trend_arrow(progress01) for k in stats.keys()}

    return dict(
        phase=phase,
        emoji=emoji,
        in_phase=in_phase,
        phase_len=phase_len,
        hormones=hormones,
        help_more=help_more,
        stats=stats,
        arrows=arrows,
    )


def today_card(profile: Profile, now: datetime) -> str:
    day = cycle_day(profile, now)
    mlen = menstrual_length(profile)
    model = phase_model(day, profile.cycle_length, mlen)
    nh = f"{profile.notify_hh:02d}:{profile.notify_mm:02d}"

    # Format exactly like you asked (Alona + cycle day + phase + ping)
    return (
        f"*TODAY: {profile.partner_name}*\n"
        f"Cycle day: *{day}/{profile.cycle_length}*\n"
        f"Phase: *{model['phase']} ({model['in_phase']}/{model['phase_len']})* {model['emoji']}\n"
        f"Daily ping: *{nh}* (Stockholm)\n\n"
        f"*STATS:*\n"
        f"Energy: *{model['stats']['energy']}* {model['arrows']['energy']}\n"
        f"Mood: *{model['stats']['mood']}* {model['arrows']['mood']}\n"
        f"Social: *{model['stats']['social']}* {model['arrows']['social']}\n"
        f"Cravings: *{model['stats']['cravings']}* {model['arrows']['cravings']}\n\n"
        f"🫶 *How to help*\n"
        f"• {model['help_more']}\n"
    )


def about_phase_text(profile: Profile, now: datetime) -> str:
    day = cycle_day(profile, now)
    mlen = menstrual_length(profile)
    model = phase_model(day, profile.cycle_length, mlen)
    return (
        f"📘 *About phase: {model['phase']}* {model['emoji']}\n"
        f"Progress: *{model['in_phase']}/{model['phase_len']}*\n\n"
        f"🧪 *Hormones (MVP model)*\n"
        f"{model['hormones']}\n\n"
        f"🫶 *How to help*\n"
        f"{model['help_more']}"
    )


def forecast_7(profile: Profile, now: datetime) -> str:
    lines = ["*🧭 7-day forecast*"]
    base = now
    for i in range(7):
        d = (base + timedelta(days=i)).astimezone(TZ)
        tmp_now = datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ)
        cd = cycle_day(profile, tmp_now)
        model = phase_model(cd, profile.cycle_length, menstrual_length(profile))
        tag = "Today" if i == 0 else d.strftime("%a %d %b")
        lines.append(f"{tag}: *Day {cd}* - {model['phase']} {model['emoji']}")
    lines.append("\nTip: phase changes at *00:00 Stockholm*.")
    return "\n".join(lines)


# ---------- Conversations ----------
(
    ONB_NAME,
    ONB_DOB,
    ONB_PSTART,
    ONB_PEND,
    ONB_CYCLE,
    ONB_TIME,
    SET_MODE,
    SET_VALUE_NAME,
    SET_VALUE_DOB,
    SET_VALUE_PSTART,
    SET_VALUE_PEND,
    SET_VALUE_CYCLE,
    SET_VALUE_TIME,
) = range(13)


async def reply(update: Update, text: str, *, kb=None, remove_kb=False, md=True):
    if remove_kb:
        kb = ReplyKeyboardRemove()
    await update.message.reply_text(
        text,
        reply_markup=kb,
        parse_mode="Markdown" if md else None,
        disable_web_page_preview=True,
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, "🏓 Pong. I’m alive.", kb=menu_kb(), md=False)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"🧩 Daycue bot version: *v{VERSION}*", kb=menu_kb(), md=True)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_profile(update.effective_chat.id)
    if not p:
        await reply(update, "Type /start to set up.", remove_kb=True, md=False)
        return
    await reply(update, "✅ Menu ready.", kb=menu_kb(), md=False)


# ----- Onboarding -----
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)

    if p:
        now = datetime.now(TZ)
        await reply(update, today_card(p, now), kb=menu_kb(), md=True)
        return ConversationHandler.END

    context.user_data["onb"] = {}
    await reply(
        update,
        "Welcome. Quick onboarding.\n\n"
        "1/5 - Enter partner nickname (example: *Alona*)",
        remove_kb=True,
        md=True,
    )
    return ONB_NAME


async def onb_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\n1/5 - Enter partner nickname (example: *Alona*)", md=True)
        return ONB_NAME
    if len(text) < 2:
        await reply(update, "Nickname too short. Try again (example: *Alona*).", md=True)
        return ONB_NAME

    context.user_data["onb"]["partner_name"] = text
    await reply(update, "2/5 - Partner DOB (YYYY-MM-DD) or type *skip*", md=True)
    return ONB_DOB


async def onb_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\n2/5 - Partner DOB (YYYY-MM-DD) or type *skip*", md=True)
        return ONB_DOB

    if text.lower() == "skip":
        context.user_data["onb"]["partner_dob"] = ""
        await reply(update, "3/5 - Last period START date (YYYY-MM-DD)", md=True)
        return ONB_PSTART

    nd = normalize_date(text)
    if not nd:
        await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D). Example: 1987-08-16", md=True)
        return ONB_DOB

    context.user_data["onb"]["partner_dob"] = nd
    await reply(update, "3/5 - Last period START date (YYYY-MM-DD)", md=True)
    return ONB_PSTART


async def onb_pstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\n3/5 - Last period START date (YYYY-MM-DD)", md=True)
        return ONB_PSTART

    nd = normalize_date(text)
    if not nd:
        await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D).", md=True)
        return ONB_PSTART

    context.user_data["onb"]["period_start"] = nd
    await reply(update, "4/5 - Last period END date (YYYY-MM-DD)", md=True)
    return ONB_PEND


async def onb_pend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\n4/5 - Last period END date (YYYY-MM-DD)", md=True)
        return ONB_PEND

    ne = normalize_date(text)
    if not ne:
        await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D).", md=True)
        return ONB_PEND

    s = date.fromisoformat(context.user_data["onb"]["period_start"])
    e = date.fromisoformat(ne)
    if e < s:
        await reply(update, "⚠️ Period END can’t be before START. Try again.", md=True)
        return ONB_PEND

    context.user_data["onb"]["period_end"] = ne
    await reply(update, "5/5 - Cycle length in days (21-35). Example: *28*", md=True)
    return ONB_CYCLE


async def onb_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\n5/5 - Cycle length in days (21-35). Example: *28*", md=True)
        return ONB_CYCLE
    if not text.isdigit():
        await reply(update, "⚠️ Enter a number (21-35).", md=True)
        return ONB_CYCLE

    n = int(text)
    if not (21 <= n <= 35):
        await reply(update, "⚠️ Cycle length should be 21-35.", md=True)
        return ONB_CYCLE

    context.user_data["onb"]["cycle_length"] = n
    await reply(update, "✅ Set daily notification time (H:MM or HH:MM). Example: *9:00* or *09:00*", md=True)
    return ONB_TIME


async def onb_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_menu_text(text):
        await reply(update, "Finish onboarding first 🙂\n\nSet daily notification time (H:MM or HH:MM).", md=True)
        return ONB_TIME

    hm = normalize_time(text)
    if not hm:
        await reply(update, "⚠️ Invalid time. Use H:MM or HH:MM (example: 9:00).", md=True)
        return ONB_TIME

    hh, mm = hm
    data = context.user_data["onb"]
    chat_id = update.effective_chat.id

    p = Profile(
        chat_id=chat_id,
        partner_name=data["partner_name"],
        partner_dob=data.get("partner_dob", ""),
        period_start=data["period_start"],
        period_end=data["period_end"],
        cycle_length=int(data["cycle_length"]),
        notify_hh=hh,
        notify_mm=mm,
        paused=False,
        last_sent="",
    )
    upsert_profile(p)

    now = datetime.now(TZ)
    # IMPORTANT: show menu immediately and keep it
    await reply(update, "✅ Setup complete. Menu is ready 👇", kb=menu_kb(), md=False)
    await reply(update, today_card(p, now), kb=menu_kb(), md=True)
    return ConversationHandler.END


# ----- Menu handling -----
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await reply(update, "Type /start to set up.", remove_kb=True, md=False)
        return ConversationHandler.END

    now = datetime.now(TZ)

    if text == BTN_TODAY:
        await reply(update, today_card(p, now), kb=menu_kb(), md=True)
        return ConversationHandler.END

    if text == BTN_FORECAST:
        await reply(update, forecast_7(p, now), kb=menu_kb(), md=True)
        return ConversationHandler.END

    if text == BTN_ABOUT:
        await reply(update, about_phase_text(p, now), kb=menu_kb(), md=True)
        return ConversationHandler.END

    if text == BTN_SETTINGS:
        await reply(update, "⚙️ Settings", kb=settings_kb(p.paused), md=False)
        return SET_MODE

    # Default: keep menu visible
    await reply(update, "Use the menu 👇", kb=menu_kb(), md=False)
    return ConversationHandler.END


# ----- Settings -----
async def settings_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    p = get_profile(chat_id)
    if not p:
        await reply(update, "Type /start to set up.", md=False)
        return ConversationHandler.END

    if text == "⬅️ Back":
        await reply(update, "✅ Back to menu.", kb=menu_kb(), md=False)
        return ConversationHandler.END

    if text == SET_NAME:
        await reply(update, "Enter partner name (example: Alona)", remove_kb=True, md=False)
        return SET_VALUE_NAME

    if text == SET_DOB:
        await reply(update, "Enter DOB (YYYY-MM-DD) or type 'skip'", remove_kb=True, md=False)
        return SET_VALUE_DOB

    if text == SET_PSTART:
        await reply(update, "Enter period START (YYYY-MM-DD)", remove_kb=True, md=False)
        return SET_VALUE_PSTART

    if text == SET_PEND:
        await reply(update, "Enter period END (YYYY-MM-DD)", remove_kb=True, md=False)
        return SET_VALUE_PEND

    if text == SET_CYCLE:
        await reply(update, "Enter cycle length (21-35)", remove_kb=True, md=False)
        return SET_VALUE_CYCLE

    if text == SET_TIME:
        await reply(update, "Enter daily ping time (H:MM or HH:MM)", remove_kb=True, md=False)
        return SET_VALUE_TIME

    if text == SET_PAUSE:
        p.paused = True
        upsert_profile(p)
        await reply(update, "⏸ Paused. No daily pings.", kb=settings_kb(p.paused), md=False)
        return SET_MODE

    if text == SET_RESUME:
        p.paused = False
        upsert_profile(p)
        await reply(update, "▶️ Resumed. Daily pings are active.", kb=settings_kb(p.paused), md=False)
        return SET_MODE

    if text == SET_RESET:
        PROFILES.pop(chat_id, None)
        save_profiles()
        await reply(update, "🗑 Reset done. Type /start to set up again.", remove_kb=True, md=False)
        return ConversationHandler.END

    await reply(update, "Pick an option from Settings.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    if len(text) < 2:
        await reply(update, "Name too short. Try again.", md=False)
        return SET_VALUE_NAME
    p.partner_name = text
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    if text.lower() == "skip":
        p.partner_dob = ""
    else:
        nd = normalize_date(text)
        if not nd:
            await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D) or type 'skip'.", md=False)
            return SET_VALUE_DOB
        p.partner_dob = nd
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_pstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    nd = normalize_date(text)
    if not nd:
        await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D).", md=False)
        return SET_VALUE_PSTART
    try:
        e = date.fromisoformat(p.period_end) if p.period_end else None
        d = date.fromisoformat(nd)
        if e and d > e:
            await reply(update, "⚠️ START can’t be after END. Update END first.", md=False)
            return SET_VALUE_PSTART
    except Exception:
        pass
    p.period_start = nd
    p.last_sent = ""
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_pend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    nd = normalize_date(text)
    if not nd:
        await reply(update, "⚠️ Invalid date. Use YYYY-MM-DD (or YYYY-M-D).", md=False)
        return SET_VALUE_PEND
    try:
        s = date.fromisoformat(p.period_start) if p.period_start else None
        d = date.fromisoformat(nd)
        if s and d < s:
            await reply(update, "⚠️ END can’t be before START.", md=False)
            return SET_VALUE_PEND
    except Exception:
        pass
    p.period_end = nd
    p.last_sent = ""
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    if not text.isdigit():
        await reply(update, "⚠️ Enter a number (21-35).", md=False)
        return SET_VALUE_CYCLE
    n = int(text)
    if not (21 <= n <= 35):
        await reply(update, "⚠️ Must be 21-35.", md=False)
        return SET_VALUE_CYCLE
    p.cycle_length = n
    p.last_sent = ""
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


async def set_value_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    p = get_profile(update.effective_chat.id)
    if not p:
        return ConversationHandler.END
    hm = normalize_time(text)
    if not hm:
        await reply(update, "⚠️ Invalid time. Use H:MM or HH:MM (example: 9:00).", md=False)
        return SET_VALUE_TIME
    p.notify_hh, p.notify_mm = hm
    p.last_sent = ""
    upsert_profile(p)
    await reply(update, "✅ Updated.", kb=settings_kb(p.paused), md=False)
    return SET_MODE


# ---------- Notifications (background loop) ----------
async def notifications_loop(app: Application):
    await asyncio.sleep(2)
    log.info("🔔 Notifications loop started (%s)", TZ_NAME)

    while True:
        try:
            now = datetime.now(TZ)
            today_str = now.date().isoformat()

            for chat_id, p in list(PROFILES.items()):
                if p.paused:
                    continue

                if now.hour == p.notify_hh and now.minute == p.notify_mm:
                    if p.last_sent != today_str:
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=today_card(p, now),
                                parse_mode="Markdown",
                                reply_markup=menu_kb(),
                                disable_web_page_preview=True,
                            )
                            p.last_sent = today_str
                            upsert_profile(p)
                            log.info("Sent daily ping to %s", chat_id)
                        except Exception as e:
                            log.warning("Send failed to %s: %s", chat_id, e)

        except Exception as e:
            log.exception("Notifications loop error: %s", e)

        await asyncio.sleep(20)


# ---------- Commands shortcuts ----------
async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_profile(update.effective_chat.id)
    if not p:
        await reply(update, "Type /start to set up.", md=False)
        return
    await reply(update, "⚙️ Settings -> update period dates", kb=settings_kb(p.paused), md=False)


# ---------- App wiring ----------
def require_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN is missing")
    return token


def build_app() -> Application:
    token = require_token()
    load_profiles()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("period", cmd_period))

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ONB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_name)],
            ONB_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_dob)],
            ONB_PSTART: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_pstart)],
            ONB_PEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_pend)],
            ONB_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_cycle)],
            ONB_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_time)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        name="onboarding",
        persistent=False,
    )
    app.add_handler(onboarding)

    settings_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(f"^{re.escape(BTN_SETTINGS)}$"), handle_menu)],
        states={
            SET_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_mode)],
            SET_VALUE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_name)],
            SET_VALUE_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_dob)],
            SET_VALUE_PSTART: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_pstart)],
            SET_VALUE_PEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_pend)],
            SET_VALUE_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_cycle)],
            SET_VALUE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value_time)],
        },
        fallbacks=[CommandHandler("menu", cmd_menu), CommandHandler("start", cmd_start)],
        name="settings",
        persistent=False,
    )
    app.add_handler(settings_conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    async def _post_init(application: Application):
        application.create_task(notifications_loop(application))

    app.post_init = _post_init

    return app


def main():
    log.info("🚀 Daycue boot v%s", VERSION)
    app = build_app()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
