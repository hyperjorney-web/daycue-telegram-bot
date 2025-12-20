#!/usr/bin/env python3
"""
Daycue Telegram Bot (v1.2-beta-db)
- Always-on menu (Today / Forecast / Settings / About phase)
- Onboarding (6 steps) -> saves to Supabase -> shows TODAY instantly
- Daily notifications via internal asyncio loop (no PTB job-queue)
- Supabase Postgres persistence (users + period_log)
- Optional copy backend in DB (copy_strings) with safe fallbacks

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
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

VERSION = "1.2-beta-db"
LOG = logging.getLogger("daycue")

# ----------------------------
# UI / menu
# ----------------------------
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🔮 Forecast"
BTN_SETTINGS = "⚙️ Settings"
BTN_ABOUT = "📚 About phase"

MENU_TEXTS = {BTN_TODAY, BTN_FORECAST, BTN_SETTINGS, BTN_ABOUT}

MENU_KB = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_FORECAST], [BTN_SETTINGS, BTN_ABOUT]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Choose…",
)

# ----------------------------
# Data model
# ----------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

@dataclass
class UserProfile:
    chat_id: int
    partner_name: str
    partner_dob: Optional[str]      # YYYY-MM-DD or None
    period_start: str               # YYYY-MM-DD
    period_end: Optional[str]       # YYYY-MM-DD or None
    cycle_length: int               # 21-35
    notify_time: str                # HH:MM
    tz: str                         # IANA timezone
    paused: bool = False

# ----------------------------
# DB layer (asyncpg) - matches your Supabase tables
# ----------------------------
DB_POOL: Optional[asyncpg.Pool] = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  chat_id BIGINT PRIMARY KEY,
  partner_name TEXT NOT NULL,
  partner_dob DATE NULL,
  period_start DATE NOT NULL,
  period_end DATE NULL,
  cycle_length INT NOT NULL,
  notify_time TEXT NOT NULL,
  tz TEXT NOT NULL DEFAULT 'Europe/Stockholm',
  paused BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS period_log (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
  period_start DATE NOT NULL,
  period_end DATE NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Copy backend. If you want multiple locales/phases, do NOT keep key as PK in your existing DB.
-- We'll use a unique index instead.
CREATE TABLE IF NOT EXISTS copy_strings (
  id BIGSERIAL PRIMARY KEY,
  key TEXT NOT NULL,
  locale TEXT NOT NULL DEFAULT 'en',
  phase TEXT NULL,
  text TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS copy_strings_key_locale_phase
  ON copy_strings(key, locale, phase);
"""

async def db_init():
    global DB_POOL
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("DATABASE_URL is missing/invalid (must start with postgres:// or postgresql://)")
    DB_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)
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
            period_start=row["period_start"].isoformat(),
            period_end=row["period_end"].isoformat() if row["period_end"] else None,
            cycle_length=int(row["cycle_length"]),
            notify_time=row["notify_time"],
            tz=row["tz"],
            paused=bool(row["paused"]),
        )

async def db_upsert_user(p: UserProfile) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users(chat_id, partner_name, partner_dob, period_start, period_end, cycle_length, notify_time, tz, paused)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT(chat_id) DO UPDATE SET
              partner_name=EXCLUDED.partner_name,
              partner_dob=EXCLUDED.partner_dob,
              period_start=EXCLUDED.period_start,
              period_end=EXCLUDED.period_end,
              cycle_length=EXCLUDED.cycle_length,
              notify_time=EXCLUDED.notify_time,
              tz=EXCLUDED.tz,
              paused=EXCLUDED.paused,
              updated_at=now()
            """,
            p.chat_id,
            p.partner_name,
            dt.date.fromisoformat(p.partner_dob) if p.partner_dob else None,
            dt.date.fromisoformat(p.period_start),
            dt.date.fromisoformat(p.period_end) if p.period_end else None,
            int(p.cycle_length),
            p.notify_time,
            p.tz,
            bool(p.paused),
        )

async def db_log_period(chat_id: int, start_date: str, end_date: Optional[str]) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO period_log(chat_id, period_start, period_end) VALUES($1,$2,$3)",
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
    "phase_desc_menstrual": "Menstrual: lower energy + higher sensitivity. Comfort, calm, warmth.",
    "phase_desc_follicular": "Follicular: energy rises, mood steadier. Great for plans and progress.",
    "phase_desc_ovulatory": "Ovulatory: peak social/sexual drive. Stronger communication and confidence.",
    "phase_desc_luteal": "Luteal: energy declines, irritability can rise. Reduce stress, keep predictable routines.",
    "help_menstrual": "Warmth + patience. Keep plans light. Offer tea/food and quiet support.",
    "help_follicular": "Encourage movement + ideas. Plan something fun. Celebrate momentum.",
    "help_ovulatory": "Connection + compliments. Great for dates, deeper talks, collaboration.",
    "help_luteal": "Reassure, don’t debate. Lower demands. Provide space + stability.",
}

async def copy_get(key: str, locale: str = "en", phase: Optional[str] = None) -> str:
    now = asyncio.get_running_loop().time()
    cache_key = f"{key}|{locale}|{phase or ''}"
    cached = _copy_cache.get(cache_key)
    if cached and (now - cached[0]) < COPY_CACHE_SECONDS:
        return cached[1]

    text = None
    if DB_POOL:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT text FROM copy_strings
                WHERE key=$1 AND locale=$2 AND enabled=TRUE
                  AND (phase IS NULL OR phase=$3)
                ORDER BY (phase IS NULL) ASC
                LIMIT 1
                """,
                key, locale, phase
            )
            if row:
                text = row["text"]

    if not text:
        text = FALLBACK_COPY.get(key, "")

    _copy_cache[cache_key] = (now, text)
    return text

# ----------------------------
# Cycle math (predictable MVP heuristic)
# ----------------------------
def _default_tz() -> str:
    return os.getenv("TZ_DEFAULT", "Europe/Stockholm")

def _norm(s: str) -> str:
    return (s or "").strip()

def _is_menu_press(text: str) -> bool:
    return _norm(text) in MENU_TEXTS

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
    ov_center = max(10, cycle_len - 14)  # rough ovulation center
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
        base = {"energy": 2, "mood": 2, "social": 2, "cravings": 4, "irritability": 3, "focus": 2}
    elif phase == "follicular":
        base = {"energy": 4, "mood": 4, "social": 4, "cravings": 2, "irritability": 2, "focus": 4}
    elif phase == "ovulatory":
        base = {"energy": 5, "mood": 5, "social": 5, "cravings": 2, "irritability": 1, "focus": 4}
    else:
        base = {"energy": 3, "mood": 3, "social": 3, "cravings": 4, "irritability": 4, "focus": 3}

    a, b = bounds[phase]
    span = max(1, b - a)
    t = (day - a) / span

    if phase == "follicular" and t > 0.6:
        base["energy"] = min(5, base["energy"] + 1)
    if phase == "luteal" and t > 0.6:
        base["mood"] = max(1, base["mood"] - 1)
        base["focus"] = max(1, base["focus"] - 1)
        base["irritability"] = min(5, base["irritability"] + 1)
    if phase == "menstrual" and t < 0.3:
        base["energy"] = max(1, base["energy"] - 1)

    return base

def _bar(level: int) -> str:
    level = max(1, min(5, level))
    return "▰" * level + "▱" * (5 - level)

# ----------------------------
# Rendering
# ----------------------------
async def render_today(profile: UserProfile) -> str:
    tz = profile.tz
    today = _today_in_tz(tz)

    start = dt.date.fromisoformat(profile.period_start)
    period_len = _compute_period_length(profile.period_start, profile.period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)

    day = _cycle_day_for(today, start, profile.cycle_length)
    phase = _phase_for_cycle_day(day, bounds)
    pa, pb = bounds[phase]
    phase_pos = day - pa + 1
    phase_total = pb - pa + 1

    yday = today - dt.timedelta(days=1)
    yday_num = _cycle_day_for(yday, start, profile.cycle_length)
    now_stats = _phase_stats(day, bounds)
    prev_stats = _phase_stats(yday_num, bounds)

    def stat_line(label: str, emoji: str, key: str):
        return f"{emoji} {label}: {_bar(now_stats[key])} {_arrow(now_stats[key], prev_stats[key])}"

    help_text = await copy_get(f"help_{phase}", phase=phase)

    # Next phase change within current cycle
    next_change = None
    next_phase = None
    for ph, (a, b) in bounds.items():
        if ph == phase:
            if b < profile.cycle_length:
                delta = (b + 1) - day
                next_change = today + dt.timedelta(days=delta)
                next_phase = _phase_for_cycle_day(b + 1, bounds)
            break

    change_txt = ""
    if next_change and next_phase and next_phase != phase:
        change_txt = f"\n\n⏭ Next change: <b>{next_change.isoformat()}</b> - {PHASE_NAME[next_phase]} {PHASE_EMOJI[next_phase]}"

    return (
        f"<b>TODAY: {profile.partner_name}</b>\n"
        f"Cycle day: <b>{day}/{profile.cycle_length}</b>\n"
        f"Phase: <b>{PHASE_NAME[phase]}</b> ({phase_pos}/{phase_total}) {PHASE_EMOJI[phase]}\n"
        f"Daily ping: <b>{profile.notify_time}</b> ({tz})\n\n"
        f"<b>STATS</b>\n"
        f"{stat_line('Energy', '⚡', 'energy')}\n"
        f"{stat_line('Mood', '🎭', 'mood')}\n"
        f"{stat_line('Social', '🗣️', 'social')}\n"
        f"{stat_line('Cravings', '🍫', 'cravings')}\n"
        f"{stat_line('Irritability', '💢', 'irritability')}\n"
        f"{stat_line('Focus', '🧠', 'focus')}\n\n"
        f"<b>🫶 How to help</b>\n"
        f"• {help_text}"
        f"{change_txt}"
    )

async def render_about_phase(profile: UserProfile) -> str:
    tz = profile.tz
    today = _today_in_tz(tz)

    start = dt.date.fromisoformat(profile.period_start)
    period_len = _compute_period_length(profile.period_start, profile.period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)
    day = _cycle_day_for(today, start, profile.cycle_length)
    phase = _phase_for_cycle_day(day, bounds)

    desc = await copy_get(f"phase_desc_{phase}", phase=phase)
    return f"<b>About phase: {PHASE_NAME[phase]} {PHASE_EMOJI[phase]}</b>\n\n{desc}"

async def render_forecast(profile: UserProfile, days: int = 7) -> str:
    tz = profile.tz
    today = _today_in_tz(tz)

    start = dt.date.fromisoformat(profile.period_start)
    period_len = _compute_period_length(profile.period_start, profile.period_end)
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
        elif ph != last_phase:
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
# Telegram send helper
# ----------------------------
async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MENU_KB)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML, reply_markup=MENU_KB)

# ----------------------------
# Onboarding (menu always visible + menu presses don't break steps)
# ----------------------------
(O_NICK, O_DOB, O_START, O_END, O_CYCLE, O_TIME) = range(6)

async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send(update, context,
        "Welcome 👋\n\n"
        "<b>Quick onboarding</b>\n\n"
        "1/6 - Enter partner nickname (example: Anna)"
    )
    return O_NICK

async def o_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n1/6 - Enter partner nickname (example: Anna)")
        return O_NICK
    nick = _norm(update.message.text)
    if len(nick) < 2:
        await _send(update, context, "Nickname too short.\n\n1/6 - Enter partner nickname (2+ letters)")
        return O_NICK
    context.user_data["partner_name"] = nick
    await _send(update, context, "2/6 - Partner DOB (YYYY-MM-DD) or type <b>skip</b>")
    return O_DOB

async def o_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n2/6 - Partner DOB (YYYY-MM-DD) or type <b>skip</b>")
        return O_DOB
    t = _norm(update.message.text).lower()
    if t == "skip":
        context.user_data["partner_dob"] = None
    else:
        if not DATE_RE.match(t):
            await _send(update, context, "Invalid date.\n\n2/6 - Partner DOB (YYYY-MM-DD) or type <b>skip</b>")
            return O_DOB
        dt.date.fromisoformat(t)
        context.user_data["partner_dob"] = t
    await _send(update, context, "3/6 - Last period START date (YYYY-MM-DD)")
    return O_START

async def o_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n3/6 - Last period START date (YYYY-MM-DD)")
        return O_START
    t = _norm(update.message.text)
    if not DATE_RE.match(t):
        await _send(update, context, "Invalid date.\n\n3/6 - Last period START date (YYYY-MM-DD)")
        return O_START
    dt.date.fromisoformat(t)
    context.user_data["period_start"] = t
    await _send(update, context, "4/6 - Last period END date (YYYY-MM-DD) or type <b>skip</b>")
    return O_END

async def o_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n4/6 - Last period END date (YYYY-MM-DD) or type <b>skip</b>")
        return O_END
    t = _norm(update.message.text).lower()
    if t == "skip":
        context.user_data["period_end"] = None
    else:
        if not DATE_RE.match(t):
            await _send(update, context, "Invalid date.\n\n4/6 - Last period END date (YYYY-MM-DD) or type <b>skip</b>")
            return O_END
        end = dt.date.fromisoformat(t)
        start = dt.date.fromisoformat(context.user_data["period_start"])
        if end < start:
            await _send(update, context, "End date can't be before start date.\n\n4/6 - Try again (YYYY-MM-DD)")
            return O_END
        context.user_data["period_end"] = t
    await _send(update, context, "5/6 - Cycle length in days (21-35). Example: 28")
    return O_CYCLE

async def o_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n5/6 - Cycle length in days (21-35). Example: 28")
        return O_CYCLE
    t = _norm(update.message.text)
    if not t.isdigit():
        await _send(update, context, "Enter a number 21-35.\n\n5/6 - Cycle length in days (21-35).")
        return O_CYCLE
    n = int(t)
    if n < 21 or n > 35:
        await _send(update, context, "Enter a number 21-35.\n\n5/6 - Cycle length in days (21-35).")
        return O_CYCLE
    context.user_data["cycle_length"] = n
    await _send(update, context, "6/6 - Daily notification time (HH:MM). Example: 09:00")
    return O_TIME

async def o_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, "Finish onboarding first 🙂\n\n6/6 - Daily notification time (HH:MM). Example: 09:00")
        return O_TIME
    t = _norm(update.message.text)
    if not TIME_RE.match(t):
        await _send(update, context, "Time format should be HH:MM (24h).\n\n6/6 - Daily notification time (HH:MM).")
        return O_TIME
    _parse_time_hhmm(t)

    chat_id = update.effective_chat.id
    profile = UserProfile(
        chat_id=chat_id,
        partner_name=context.user_data["partner_name"],
        partner_dob=context.user_data.get("partner_dob"),
        period_start=context.user_data["period_start"],
        period_end=context.user_data.get("period_end"),
        cycle_length=int(context.user_data["cycle_length"]),
        notify_time=t,
        tz=_default_tz(),
        paused=False,
    )

    await db_upsert_user(profile)
    await db_log_period(chat_id, profile.period_start, profile.period_end)
    context.user_data.clear()

    # ✅ Critical: show TODAY immediately with menu
    await _send(update, context, "✅ Saved.\n\n" + await render_today(profile))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await _send(update, context, "Onboarding cancelled.")
    return ConversationHandler.END

# ----------------------------
# Commands + menu
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_today(profile))

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
        f"Partner: <b>{profile.partner_name}</b>\n"
        f"Period: <b>{profile.period_start}</b> → <b>{profile.period_end or 'unknown'}</b>\n"
        f"Cycle: <b>{profile.cycle_length}</b>\n"
        f"Notify: <b>{profile.notify_time}</b> ({profile.tz})\n"
        f"Paused: <b>{'yes' if profile.paused else 'no'}</b>\n\n"
        "Commands:\n"
        "• /update_period START [END]\n"
        "• /set_time HH:MM\n"
        "• /set_cycle 21-35\n"
        "• /pause or /resume\n"
        "• /re_onboard"
    )

async def cmd_re_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_onboarding(update, context)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.paused = True
    await db_upsert_user(profile)
    await _send(update, context, "⏸ Paused daily pings.\n\n" + await render_today(profile))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.paused = False
    await db_upsert_user(profile)
    await _send(update, context, "▶️ Resumed daily pings.\n\n" + await render_today(profile))

async def cmd_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not TIME_RE.match(parts[1]):
        return await _send(update, context, "Usage: /set_time HH:MM")
    _parse_time_hhmm(parts[1])
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
    s = dt.date.fromisoformat(start_s)
    if end_s:
        e = dt.date.fromisoformat(end_s)
        if e < s:
            return await _send(update, context, "END cannot be before START.")

    profile.period_start = start_s
    profile.period_end = end_s
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
                rows = await conn.fetch(
                    "SELECT chat_id, notify_time, tz, paused FROM users"
                )

            now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

            for r in rows:
                if bool(r["paused"]):
                    continue

                chat_id = int(r["chat_id"])
                notify_time = r["notify_time"]
                tz = r["tz"]

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
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    return app

def main():
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
