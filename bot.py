#!/usr/bin/env python3
"""
Daycue Telegram Bot (v1.1-beta-db)
- Always-on menu (Today / Forecast / Settings / About)
- Onboarding (6 steps)
- Daily notifications via internal asyncio loop (no PTB job-queue dependency)
- Supabase Postgres persistence (users + periods)
- Optional "copy backend" in DB (copy_strings) with safe fallbacks

ENV:
  TELEGRAM_BOT_TOKEN   required
  DATABASE_URL         required (postgres://... or postgresql://...)
  TZ_DEFAULT           optional, default "Europe/Stockholm"
  COPY_CACHE_SECONDS   optional, default 300
"""
import asyncio
import datetime as dt
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

import asyncpg
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

VERSION = "1.1-beta-db"
LOG = logging.getLogger("daycue")

# ----------------------------
# UI / menu
# ----------------------------
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🔮 Forecast"
BTN_SETTINGS = "⚙️ Settings"
BTN_ABOUT = "📚 About phase"

MENU_KB = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_FORECAST], [BTN_SETTINGS, BTN_ABOUT]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Choose…",
)

def _menu_kwargs() -> Dict[str, Any]:
    return {"reply_markup": MENU_KB}

# ----------------------------
# Data model
# ----------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

@dataclass
class UserProfile:
    chat_id: int
    partner_name: str
    partner_dob: Optional[str]  # YYYY-MM-DD or None
    last_period_start: str      # YYYY-MM-DD
    last_period_end: Optional[str]  # YYYY-MM-DD or None
    cycle_length: int           # 21-35
    notify_time: str            # HH:MM
    timezone: str               # IANA

# ----------------------------
# DB layer (asyncpg)
# ----------------------------
DB_POOL: Optional[asyncpg.Pool] = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  chat_id BIGINT PRIMARY KEY,
  partner_name TEXT NOT NULL,
  partner_dob DATE NULL,
  last_period_start DATE NOT NULL,
  last_period_end DATE NULL,
  cycle_length INT NOT NULL,
  notify_time TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'Europe/Stockholm',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS periods (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
  start_date DATE NOT NULL,
  end_date DATE NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS copy_strings (
  key TEXT PRIMARY KEY,
  locale TEXT NOT NULL DEFAULT 'en',
  phase TEXT NULL,
  text TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

async def db_init():
    global DB_POOL
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is missing")
    DB_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    async with DB_POOL.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    LOG.info("✅ DB connected + schema ensured")

async def db_fetch_user(chat_id: int) -> Optional[UserProfile]:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE chat_id=$1", chat_id)
        if not row:
            return None
        return UserProfile(
            chat_id=int(row["chat_id"]),
            partner_name=row["partner_name"],
            partner_dob=row["partner_dob"].isoformat() if row["partner_dob"] else None,
            last_period_start=row["last_period_start"].isoformat(),
            last_period_end=row["last_period_end"].isoformat() if row["last_period_end"] else None,
            cycle_length=int(row["cycle_length"]),
            notify_time=row["notify_time"],
            timezone=row["timezone"],
        )

async def db_upsert_user(p: UserProfile) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users(chat_id, partner_name, partner_dob, last_period_start, last_period_end, cycle_length, notify_time, timezone)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(chat_id) DO UPDATE SET
              partner_name=EXCLUDED.partner_name,
              partner_dob=EXCLUDED.partner_dob,
              last_period_start=EXCLUDED.last_period_start,
              last_period_end=EXCLUDED.last_period_end,
              cycle_length=EXCLUDED.cycle_length,
              notify_time=EXCLUDED.notify_time,
              timezone=EXCLUDED.timezone,
              updated_at=now()
            """,
            p.chat_id,
            p.partner_name,
            dt.date.fromisoformat(p.partner_dob) if p.partner_dob else None,
            dt.date.fromisoformat(p.last_period_start),
            dt.date.fromisoformat(p.last_period_end) if p.last_period_end else None,
            int(p.cycle_length),
            p.notify_time,
            p.timezone,
        )

async def db_log_period(chat_id: int, start_date: str, end_date: Optional[str]) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO periods(chat_id, start_date, end_date) VALUES($1,$2,$3)",
            chat_id,
            dt.date.fromisoformat(start_date),
            dt.date.fromisoformat(end_date) if end_date else None,
        )

# ----------------------------
# Copy backend (DB) with caching + fallbacks
# ----------------------------
COPY_CACHE_SECONDS = int(os.getenv("COPY_CACHE_SECONDS", "300"))
_copy_cache: Dict[str, Tuple[float, str]] = {}

FALLBACK_COPY: Dict[str, str] = {
    "phase_desc_menstrual": "Menstrual phase: lower energy, more sensitivity. Comfort + calm help most.",
    "phase_desc_follicular": "Follicular phase: energy rises, mood often steadier. Great time for plans and progress.",
    "phase_desc_ovulatory": "Ovulatory phase: peak social/sexual drive, confidence and communication often stronger.",
    "phase_desc_luteal": "Luteal phase: energy declines, irritability can rise. Reduce stress, keep things predictable.",
    "help_menstrual": "Warmth + patience. Keep plans light. Offer food/tea and quiet support.",
    "help_follicular": "Encourage ideas + movement. Plan something fun. Celebrate momentum.",
    "help_ovulatory": "Compliments + connection. Great for dates, deeper talks, and teamwork.",
    "help_luteal": "Reassure, don’t debate. Lower demands. Provide space + stability.",
}

async def copy_get(key: str) -> str:
    now = asyncio.get_event_loop().time()
    cached = _copy_cache.get(key)
    if cached and (now - cached[0]) < COPY_CACHE_SECONDS:
        return cached[1]

    text = None
    if DB_POOL:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text FROM copy_strings WHERE key=$1 AND enabled=TRUE",
                key,
            )
            if row:
                text = row["text"]

    if not text:
        text = FALLBACK_COPY.get(key, "")

    _copy_cache[key] = (now, text)
    return text

# ----------------------------
# Cycle math (predictable MVP heuristic)
# ----------------------------
def _today_in_tz(tz_name: str) -> dt.date:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(tz_name)).date()

def _parse_time_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))

def _compute_period_length(start: str, end: Optional[str]) -> int:
    if not end:
        return 5
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    return max(1, (e - s).days + 1)

def _cycle_day_for(date_: dt.date, cycle_start: dt.date, cycle_len: int) -> int:
    delta = (date_ - cycle_start).days
    return (delta % cycle_len) + 1

def _phase_boundaries(cycle_len: int, period_len: int) -> Dict[str, Tuple[int, int]]:
    period_len = min(max(period_len, 3), 8)
    ov_center = max(10, cycle_len - 14)
    ov_start = max(period_len + 1, ov_center - 1)
    ov_end = min(cycle_len, ov_center + 1)
    fol_start = period_len + 1
    fol_end = max(fol_start, ov_start - 1)
    lut_start = ov_end + 1
    lut_end = cycle_len
    return {
        "menstrual": (1, period_len),
        "follicular": (fol_start, fol_end),
        "ovulatory": (ov_start, ov_end),
        "luteal": (lut_start, lut_end),
    }

def _phase_for_cycle_day(day: int, bounds: Dict[str, Tuple[int,int]]) -> str:
    for phase, (a, b) in bounds.items():
        if a <= day <= b:
            return phase
    return "luteal"

PHASE_NAME = {"menstrual": "Menstrual", "follicular": "Follicular", "ovulatory": "Ovulatory", "luteal": "Luteal"}
PHASE_EMOJI = {"menstrual": "🩸", "follicular": "🌱", "ovulatory": "🔥", "luteal": "🌙"}

def _arrow(cur: int, prev: int) -> str:
    if cur > prev: return "↗"
    if cur < prev: return "↘"
    return "→"

def _phase_stats(day: int, bounds: Dict[str, Tuple[int,int]]) -> Dict[str, int]:
    phase = _phase_for_cycle_day(day, bounds)
    if phase == "menstrual":
        base = {"energy": 2, "mood": 2, "social": 2, "cravings": 4, "irritability": 3, "focus": 2, "libido": 2, "anxiety": 3}
    elif phase == "follicular":
        base = {"energy": 4, "mood": 4, "social": 4, "cravings": 2, "irritability": 2, "focus": 4, "libido": 3, "anxiety": 2}
    elif phase == "ovulatory":
        base = {"energy": 5, "mood": 5, "social": 5, "cravings": 2, "irritability": 1, "focus": 4, "libido": 5, "anxiety": 1}
    else:
        base = {"energy": 3, "mood": 3, "social": 3, "cravings": 4, "irritability": 4, "focus": 3, "libido": 3, "anxiety": 3}

    a, b = bounds[phase]
    span = max(1, b - a)
    t = (day - a) / span
    if phase == "follicular":
        base["energy"] = min(5, base["energy"] + (1 if t > 0.6 else 0))
        base["libido"] = min(5, base["libido"] + (1 if t > 0.7 else 0))
    if phase == "luteal":
        base["mood"] = max(1, base["mood"] - (1 if t > 0.6 else 0))
        base["focus"] = max(1, base["focus"] - (1 if t > 0.6 else 0))
        base["irritability"] = min(5, base["irritability"] + (1 if t > 0.7 else 0))
    if phase == "menstrual":
        base["energy"] = max(1, base["energy"] - (1 if t < 0.3 else 0))
    return base

def _bar(level: int) -> str:
    level = max(1, min(5, level))
    return "▰" * level + "▱" * (5 - level)

# ----------------------------
# Rendering
# ----------------------------
async def render_today(profile: UserProfile) -> str:
    tz = profile.timezone
    today = _today_in_tz(tz)
    start = dt.date.fromisoformat(profile.last_period_start)
    period_len = _compute_period_length(profile.last_period_start, profile.last_period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)

    day = _cycle_day_for(today, start, profile.cycle_length)
    phase = _phase_for_cycle_day(day, bounds)

    pa, pb = bounds[phase]
    phase_pos = day - pa + 1
    phase_total = pb - pa + 1

    yday = today - dt.timedelta(days=1)
    yday_num = _cycle_day_for(yday, start, profile.cycle_length)
    stats_now = _phase_stats(day, bounds)
    stats_prev = _phase_stats(yday_num, bounds)

    def stat_line(label: str, emoji: str, key: str):
        return f"{emoji} {label}: {_bar(stats_now[key])} {_arrow(stats_now[key], stats_prev[key])}"

    help_text = await copy_get(f"help_{phase}")

    # next phase change date (within current cycle window)
    next_change_date = None
    next_phase = None
    for ph, (a, b) in bounds.items():
        if ph == phase:
            if b < profile.cycle_length:
                delta = (b + 1) - day
                next_change_date = today + dt.timedelta(days=delta)
                next_phase = _phase_for_cycle_day(b + 1, bounds)
            break
    change_txt = ""
    if next_change_date and next_phase and next_phase != phase:
        change_txt = f"\n\n⏭ Next change: {next_change_date.isoformat()} - {PHASE_NAME[next_phase]} {PHASE_EMOJI[next_phase]}"

    return (
        f"<b>TODAY: {profile.partner_name}</b>\n"
        f"Cycle day: <b>{day}/{profile.cycle_length}</b>\n"
        f"Phase: <b>{PHASE_NAME[phase]}</b> ({phase_pos}/{phase_total}) {PHASE_EMOJI[phase]}\n"
        f"Daily ping: <b>{profile.notify_time}</b> ({tz.replace('_',' ')})\n\n"
        f"<b>STATS</b>\n"
        f"{stat_line('Energy', '⚡', 'energy')}\n"
        f"{stat_line('Mood', '🎭', 'mood')}\n"
        f"{stat_line('Social', '🗣️', 'social')}\n"
        f"{stat_line('Cravings', '🍫', 'cravings')}\n"
        f"{stat_line('Irritability', '💢', 'irritability')}\n"
        f"{stat_line('Focus', '🧠', 'focus')}\n"
        f"{stat_line('Libido', '💕', 'libido')}\n"
        f"{stat_line('Anxiety', '🔥', 'anxiety')}\n\n"
        f"<b>🫶 How to help</b>\n"
        f"• {help_text}"
        f"{change_txt}"
    )

async def render_about_phase(profile: UserProfile) -> str:
    tz = profile.timezone
    today = _today_in_tz(tz)
    start = dt.date.fromisoformat(profile.last_period_start)
    period_len = _compute_period_length(profile.last_period_start, profile.last_period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)
    day = _cycle_day_for(today, start, profile.cycle_length)
    phase = _phase_for_cycle_day(day, bounds)
    desc = await copy_get(f"phase_desc_{phase}")
    return (
        f"<b>About phase: {PHASE_NAME[phase]} {PHASE_EMOJI[phase]}</b>\n\n"
        f"{desc}"
    )

async def render_forecast(profile: UserProfile, days: int = 7) -> str:
    tz = profile.timezone
    today = _today_in_tz(tz)
    start = dt.date.fromisoformat(profile.last_period_start)
    period_len = _compute_period_length(profile.last_period_start, profile.last_period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)

    lines = [f"<b>Forecast: next {days} days</b> ({profile.partner_name})\n"]
    last_phase = None
    change_points: List[str] = []

    for i in range(days):
        d = today + dt.timedelta(days=i)
        cd = _cycle_day_for(d, start, profile.cycle_length)
        ph = _phase_for_cycle_day(cd, bounds)
        if last_phase is None:
            last_phase = ph
        if ph != last_phase:
            change_points.append(f"• {d.isoformat()} - switches to {PHASE_NAME[ph]} {PHASE_EMOJI[ph]}")
            last_phase = ph

        st = _phase_stats(cd, bounds)
        lines.append(
            f"{d.isoformat()} · Day {cd}/{profile.cycle_length} · {PHASE_NAME[ph]} {PHASE_EMOJI[ph]} "
            f"⚡{st['energy']} 🎭{st['mood']} 🗣️{st['social']} 🍫{st['cravings']}"
        )

    lines.append("\n<b>Important change points</b>")
    lines.append("\n".join(change_points) if change_points else "• No phase switch within this window.")

    return "\n".join(lines)

# ----------------------------
# Helpers
# ----------------------------
async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *, remove_kb: bool = False):
    kwargs = {"parse_mode": ParseMode.HTML}
    kwargs["reply_markup"] = ReplyKeyboardRemove() if remove_kb else MENU_KB
    if update.message:
        await update.message.reply_text(text, **kwargs)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, **kwargs)

def _norm(s: str) -> str:
    return (s or "").strip()

def _default_tz() -> str:
    return os.getenv("TZ_DEFAULT", "Europe/Stockholm")

# ----------------------------
# Onboarding
# ----------------------------
(O_NICK, O_DOB, O_START, O_END, O_CYCLE, O_TIME) = range(6)

async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send(update, context,
        "Welcome 👋\n\n"
        "<b>Quick onboarding</b>\n\n"
        "1/6 - Enter partner nickname (example: Anna)",
        remove_kb=True,
    )
    return O_NICK

async def o_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nick = _norm(update.message.text)
    if len(nick) < 2:
        await _send(update, context, "Please enter a nickname (2+ letters).", remove_kb=True)
        return O_NICK
    context.user_data["partner_name"] = nick
    await _send(update, context, "2/6 - Partner DOB (YYYY-MM-DD) or type <b>skip</b>", remove_kb=True)
    return O_DOB

async def o_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text).lower()
    if t == "skip":
        context.user_data["partner_dob"] = None
    else:
        if not DATE_RE.match(t):
            await _send(update, context, "Format should be YYYY-MM-DD or type 'skip'.", remove_kb=True)
            return O_DOB
        try:
            dt.date.fromisoformat(t)
        except Exception:
            await _send(update, context, "That date doesn't look valid. Try again.", remove_kb=True)
            return O_DOB
        context.user_data["partner_dob"] = t

    await _send(update, context, "3/6 - Last period START date (YYYY-MM-DD)", remove_kb=True)
    return O_START

async def o_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if not DATE_RE.match(t):
        await _send(update, context, "Format should be YYYY-MM-DD.", remove_kb=True)
        return O_START
    try:
        dt.date.fromisoformat(t)
    except Exception:
        await _send(update, context, "That date doesn't look valid. Try again.", remove_kb=True)
        return O_START
    context.user_data["last_period_start"] = t
    await _send(update, context, "4/6 - Last period END date (YYYY-MM-DD) or type <b>skip</b>", remove_kb=True)
    return O_END

async def o_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text).lower()
    if t == "skip":
        context.user_data["last_period_end"] = None
    else:
        if not DATE_RE.match(t):
            await _send(update, context, "Format should be YYYY-MM-DD or type 'skip'.", remove_kb=True)
            return O_END
        try:
            end = dt.date.fromisoformat(t)
            start = dt.date.fromisoformat(context.user_data["last_period_start"])
            if end < start:
                await _send(update, context, "End date can't be before start date. Try again.", remove_kb=True)
                return O_END
        except Exception:
            await _send(update, context, "That date doesn't look valid. Try again.", remove_kb=True)
            return O_END
        context.user_data["last_period_end"] = t

    await _send(update, context, "5/6 - Cycle length in days (21-35). Example: 28", remove_kb=True)
    return O_CYCLE

async def o_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if not t.isdigit():
        await _send(update, context, "Enter a number between 21 and 35.", remove_kb=True)
        return O_CYCLE
    n = int(t)
    if n < 21 or n > 35:
        await _send(update, context, "Enter a number between 21 and 35.", remove_kb=True)
        return O_CYCLE
    context.user_data["cycle_length"] = n
    await _send(update, context, "6/6 - Daily notification time (HH:MM). Example: 09:00", remove_kb=True)
    return O_TIME

async def o_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if not TIME_RE.match(t):
        await _send(update, context, "Time format should be HH:MM (24h).", remove_kb=True)
        return O_TIME
    try:
        _parse_time_hhmm(t)
    except Exception:
        await _send(update, context, "That time doesn't look valid. Try again.", remove_kb=True)
        return O_TIME

    chat_id = update.effective_chat.id
    profile = UserProfile(
        chat_id=chat_id,
        partner_name=context.user_data["partner_name"],
        partner_dob=context.user_data.get("partner_dob"),
        last_period_start=context.user_data["last_period_start"],
        last_period_end=context.user_data.get("last_period_end"),
        cycle_length=int(context.user_data["cycle_length"]),
        notify_time=t,
        timezone=_default_tz(),
    )
    await db_upsert_user(profile)
    await db_log_period(chat_id, profile.last_period_start, profile.last_period_end)

    context.user_data.clear()

    await _send(update, context, "✅ Saved.\n\n" + await render_today(profile), remove_kb=False)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await _send(update, context, "Onboarding cancelled.", remove_kb=False)
    return ConversationHandler.END

# ----------------------------
# Commands + menu
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_today(profile), remove_kb=False)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_today(profile))

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_forecast(profile, 7))

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_about_phase(profile))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(
        update, context,
        "<b>Settings</b>\n\n"
        "• /update_period START [END]\n"
        "• /set_time HH:MM\n"
        "• /set_cycle 21-35\n"
        "• /re_onboard",
    )

async def cmd_re_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_onboarding(update, context)

async def cmd_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not TIME_RE.match(parts[1]):
        return await _send(update, context, "Usage: /set_time HH:MM")
    try:
        _parse_time_hhmm(parts[1])
    except Exception:
        return await _send(update, context, "Time should be HH:MM (24h).")
    profile.notify_time = parts[1]
    await db_upsert_user(profile)
    await _send(update, context, "✅ Updated.\n\n" + await render_today(profile))

async def cmd_set_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await _send(update, context, "Usage: /set_cycle 21-35")
    n = int(parts[1])
    if n < 21 or n > 35:
        return await _send(update, context, "Cycle length should be 21-35.")
    profile.cycle_length = n
    await db_upsert_user(profile)
    await _send(update, context, "✅ Updated.\n\n" + await render_today(profile))

async def cmd_update_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    parts = (update.message.text or "").split()
    if len(parts) not in (2, 3):
        return await _send(update, context, "Usage: /update_period START [END]")
    start_s = parts[1]
    end_s = parts[2] if len(parts) == 3 else None
    if not DATE_RE.match(start_s) or (end_s and not DATE_RE.match(end_s)):
        return await _send(update, context, "Dates must be YYYY-MM-DD.")
    try:
        s = dt.date.fromisoformat(start_s)
        if end_s:
            e = dt.date.fromisoformat(end_s)
            if e < s:
                return await _send(update, context, "END cannot be before START.")
    except Exception:
        return await _send(update, context, "Invalid date(s).")
    profile.last_period_start = start_s
    profile.last_period_end = end_s
    await db_upsert_user(profile)
    await db_log_period(profile.chat_id, start_s, end_s)
    await _send(update, context, "✅ Period updated.\n\n" + await render_today(profile))

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if t == BTN_TODAY:
        return await cmd_today(update, context)
    if t == BTN_FORECAST:
        return await cmd_forecast(update, context)
    if t == BTN_SETTINGS:
        return await cmd_settings(update, context)
    if t == BTN_ABOUT:
        return await cmd_about(update, context)
    await _send(update, context, "Use the menu buttons, or type /start.")

# ----------------------------
# Notifications loop (no job-queue)
# ----------------------------
async def _send_daily_ping(app: Application, profile: UserProfile):
    try:
        await app.bot.send_message(
            chat_id=profile.chat_id,
            text=await render_today(profile),
            parse_mode=ParseMode.HTML,
            reply_markup=MENU_KB,
        )
    except Exception:
        LOG.exception("Failed sending ping to chat_id=%s", profile.chat_id)

async def notification_loop(app: Application):
    sent_today: Dict[int, str] = {}
    from zoneinfo import ZoneInfo
    while True:
        try:
            if not DB_POOL:
                await asyncio.sleep(2)
                continue
            async with DB_POOL.acquire() as conn:
                rows = await conn.fetch("SELECT chat_id, notify_time, timezone FROM users")
            now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
            for r in rows:
                chat_id = int(r["chat_id"])
                notify_time = r["notify_time"]
                tz = r["timezone"]
                local_now = now_utc.astimezone(ZoneInfo(tz))
                local_date = local_now.date().isoformat()
                hhmm = f"{local_now.hour:02d}:{local_now.minute:02d}"
                if hhmm == notify_time and sent_today.get(chat_id) != local_date:
                    profile = await db_fetch_user(chat_id)
                    if profile:
                        await _send_daily_ping(app, profile)
                        sent_today[chat_id] = local_date
            await asyncio.sleep(30)
        except Exception:
            LOG.exception("notification_loop tick failed")
            await asyncio.sleep(5)

# ----------------------------
# Boot
# ----------------------------
async def post_init(app: Application):
    await db_init()
    app.create_task(notification_loop(app))
    LOG.info("🚀 Daycue boot %s", VERSION)

def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(token).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            O_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_nick)],
            O_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_dob)],
            O_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_start)],
            O_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_end)],
            O_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_cycle)],
            O_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("re_onboard", cmd_re_onboard))
    app.add_handler(CommandHandler("set_time", cmd_set_time))
    app.add_handler(CommandHandler("set_cycle", cmd_set_cycle))
    app.add_handler(CommandHandler("update_period", cmd_update_period))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    return app

def main():
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
