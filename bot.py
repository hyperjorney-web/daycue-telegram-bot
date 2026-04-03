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
BTN_STATS = "📊 Stats"
BTN_INSIGHTS = "🧾 Insights"
BTN_SETTINGS = "⚙️ Settings"
BTN_ABOUT = "📚 About phase"
BTN_HELPFUL = "👍 Helpful"
BTN_NOT_HELPFUL = "👎 Not helpful"

MENU_TEXTS = {
    BTN_TODAY,
    BTN_FORECAST,
    BTN_STATS,
    BTN_INSIGHTS,
    BTN_SETTINGS,
    BTN_ABOUT,
    BTN_HELPFUL,
    BTN_NOT_HELPFUL,
}

MENU_KB = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_FORECAST], [BTN_STATS, BTN_INSIGHTS], [BTN_SETTINGS, BTN_ABOUT], [BTN_HELPFUL, BTN_NOT_HELPFUL]],
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

CREATE TABLE IF NOT EXISTS daily_feedback (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
  cue_date DATE NOT NULL,
  cycle_day INT NOT NULL,
  phase TEXT NOT NULL,
  helpful BOOLEAN NOT NULL,
  cue_headline TEXT NOT NULL,
  energy INT NOT NULL,
  mood INT NOT NULL,
  irritability INT NOT NULL,
  connection_openness INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS daily_feedback_chat_id_cue_date
  ON daily_feedback(chat_id, cue_date);

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


async def db_upsert_feedback(
    chat_id: int,
    cue_date: str,
    cycle_day: int,
    phase: str,
    helpful: bool,
    cue_headline: str,
    stats: Dict[str, int],
) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_feedback(
              chat_id, cue_date, cycle_day, phase, helpful, cue_headline,
              energy, mood, irritability, connection_openness
            )
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT(chat_id, cue_date) DO UPDATE SET
              cycle_day=EXCLUDED.cycle_day,
              phase=EXCLUDED.phase,
              helpful=EXCLUDED.helpful,
              cue_headline=EXCLUDED.cue_headline,
              energy=EXCLUDED.energy,
              mood=EXCLUDED.mood,
              irritability=EXCLUDED.irritability,
              connection_openness=EXCLUDED.connection_openness,
              created_at=now()
            """,
            chat_id,
            dt.date.fromisoformat(cue_date),
            cycle_day,
            phase,
            helpful,
            cue_headline,
            stats["energy"],
            stats["mood"],
            stats["irritability"],
            stats["connection_openness"],
        )


async def db_feedback_summary(chat_id: int, days: int = 14) -> Dict[str, Any]:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        recent = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN helpful THEN 1 ELSE 0 END), 0) AS helpful_count,
              ROUND(AVG(energy)::numeric, 2) AS avg_energy,
              ROUND(AVG(irritability)::numeric, 2) AS avg_irritability,
              ROUND(AVG(connection_openness)::numeric, 2) AS avg_connection
            FROM daily_feedback
            WHERE chat_id=$1
              AND cue_date >= CURRENT_DATE - ($2::int - 1)
            """,
            chat_id,
            days,
        )
        by_phase = await conn.fetch(
            """
            SELECT
              phase,
              COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN helpful THEN 1 ELSE 0 END), 0) AS helpful_count
            FROM daily_feedback
            WHERE chat_id=$1
            GROUP BY phase
            ORDER BY COUNT(*) DESC, phase ASC
            """,
            chat_id,
        )
        latest = await conn.fetch(
            """
            SELECT cue_date, phase, helpful, cue_headline
            FROM daily_feedback
            WHERE chat_id=$1
            ORDER BY cue_date DESC
            LIMIT 5
            """,
            chat_id,
        )

    total = int(recent["total"] or 0)
    helpful_count = int(recent["helpful_count"] or 0)
    helpful_rate = round((helpful_count / total) * 100) if total else 0
    return {
        "days": days,
        "total": total,
        "helpful_count": helpful_count,
        "helpful_rate": helpful_rate,
        "avg_energy": recent["avg_energy"],
        "avg_irritability": recent["avg_irritability"],
        "avg_connection": recent["avg_connection"],
        "by_phase": by_phase,
        "latest": latest,
    }

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
PHASE_ORDER = ["menstrual", "follicular", "ovulatory", "luteal"]

def _arrow(cur: int, prev: int) -> str:
    if cur > prev: return "↗"
    if cur < prev: return "↘"
    return "→"

def _phase_progress(day: int, bounds: Dict[str, Tuple[int, int]]) -> Tuple[str, float]:
    phase = _phase_for_cycle_day(day, bounds)
    a, b = bounds[phase]
    span = max(1, b - a)
    return phase, (day - a) / span


def _clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def _score_1_to_5(v: float) -> int:
    return int(round(_clamp(v, 1, 5)))

def _bar(level: int) -> str:
    level = max(1, min(5, level))
    return "▰" * level + "▱" * (5 - level)


def _spark(levels: List[int]) -> str:
    chars = "▁▂▃▄▅▆▇█"
    parts = []
    for level in levels:
        clamped = max(1, min(5, level))
        idx = round((clamped - 1) * (len(chars) - 1) / 4)
        parts.append(chars[idx])
    return "".join(parts)


def _level_word(level: int, *, positive: bool = True) -> str:
    if positive:
        if level <= 2:
            return "low"
        if level == 3:
            return "steady"
        return "high"
    if level <= 2:
        return "low"
    if level == 3:
        return "moderate"
    return "high"


def _phase_summary_text(phase: str, stats: Dict[str, int]) -> str:
    if stats["physical_comfort"] <= 2 and stats["energy"] <= 2:
        return "Body comfort is lower today and energy is limited. Softer pacing, warmth, and fewer demands will help most."
    if stats["connection_openness"] >= 4 and stats["social"] >= 4:
        return "Connection looks easier today. Shared time, warmth, and a more open tone are likely to land well."
    if stats["need_for_space"] >= 4 and stats["sensitivity"] >= 4:
        return "Sensitivity is elevated today. Support matters, but it should come with more room and less pressure."
    if phase == "menstrual":
        return "Lower energy and higher sensitivity today. Calm, warmth, and fewer demands will land best."
    if phase == "follicular":
        return "Energy is building. Lighter plans, movement, and positive momentum usually work well."
    if phase == "ovulatory":
        return "Connection is easier today. Confidence, warmth, and shared time tend to land well."
    if stats["irritability"] >= 4:
        return "Emotional load may be higher today. Keep things low-pressure, steady, and practical."
    return "A steadier, lower-friction day. Predictability and gentle support will help more than pressure."


def _estimated_hormones(day: int, cycle_len: int, bounds: Dict[str, Tuple[int, int]]) -> Dict[str, int]:
    phase, t = _phase_progress(day, bounds)

    if phase == "menstrual":
        estrogen = 16 + round(18 * t)
        progesterone = 6 + round(2 * t)
        lh = 8 + round(4 * t)
        fsh = 36 - round(9 * t)
    elif phase == "follicular":
        estrogen = 34 + round(42 * t)
        progesterone = 8 + round(7 * t)
        lh = 12 + round(18 * t)
        fsh = 25 - round(5 * t)
    elif phase == "ovulatory":
        estrogen = 82 - round(12 * t)
        progesterone = 20 + round(20 * t)
        lh = 100 - round(34 * t)
        fsh = 30 - round(9 * t)
    else:
        estrogen = 64 - round(38 * t)
        progesterone = 84 - round(52 * t)
        lh = 14 - round(7 * t)
        fsh = 17 - round(4 * t)

    return {
        "estrogen": int(_clamp(estrogen, 0, 100)),
        "progesterone": int(_clamp(progesterone, 0, 100)),
        "lh": int(_clamp(lh, 0, 100)),
        "fsh": int(_clamp(fsh, 0, 100)),
    }


def _phase_stats(day: int, bounds: Dict[str, Tuple[int,int]]) -> Dict[str, int]:
    phase, t = _phase_progress(day, bounds)
    hormones = _estimated_hormones(day, max(b for _, b in bounds.values()), bounds)

    estrogen = hormones["estrogen"]
    progesterone = hormones["progesterone"]
    lh = hormones["lh"]
    fsh = hormones["fsh"]

    physical_comfort = 4.2 - 1.8 * (1 - t) if phase == "menstrual" else (
        4.0 + 0.4 * t if phase == "follicular" else (
            4.8 - 0.2 * t if phase == "ovulatory" else 4.0 - 1.0 * t
        )
    )
    energy = 1.4 + 0.026 * estrogen - 0.010 * progesterone + (0.22 if phase == "ovulatory" else 0.0)
    mood = 1.8 + 0.018 * estrogen + 0.015 * physical_comfort - 0.012 * progesterone
    social = 1.4 + 0.018 * estrogen + 0.016 * lh - 0.010 * progesterone
    cravings = 1.6 + 0.028 * progesterone + 0.012 * fsh - 0.010 * estrogen
    focus = 1.5 + 0.020 * estrogen + 0.010 * physical_comfort - 0.014 * progesterone
    sensitivity = 1.4 + 0.020 * progesterone + 0.018 * (5 - physical_comfort) + (0.35 if phase == "menstrual" else 0.0)
    irritability = 1.1 + 0.017 * progesterone + 0.015 * sensitivity + 0.010 * cravings - 0.012 * energy

    # Resolve the interdependence between need for space and connection openness in two passes.
    connection_seed = 1.5 + 0.018 * estrogen + 0.014 * lh - 0.010 * progesterone
    need_for_space = 1.1 + 0.42 * irritability + 0.20 * sensitivity - 0.16 * energy
    connection_openness = connection_seed - 0.20 * need_for_space + 0.10 * social

    if phase == "menstrual":
        energy -= 0.35 * (1 - t)
        focus -= 0.25 * (1 - t)
        need_for_space += 0.25
    elif phase == "follicular":
        energy += 0.35 * t
        social += 0.25 * t
        connection_openness += 0.20 * t
    elif phase == "ovulatory":
        mood += 0.20
        social += 0.35
        connection_openness += 0.35
        focus -= 0.15 * t
    else:
        mood -= 0.25 * t
        focus -= 0.28 * t
        cravings += 0.18 * t
        irritability += 0.30 * t
        sensitivity += 0.25 * t
        need_for_space += 0.20 * t

    return {
        "energy": _score_1_to_5(energy),
        "mood": _score_1_to_5(mood),
        "social": _score_1_to_5(social),
        "cravings": _score_1_to_5(cravings),
        "irritability": _score_1_to_5(irritability),
        "focus": _score_1_to_5(focus),
        "sensitivity": _score_1_to_5(sensitivity),
        "physical_comfort": _score_1_to_5(physical_comfort),
        "need_for_space": _score_1_to_5(need_for_space),
        "connection_openness": _score_1_to_5(connection_openness),
    }


def _phase_segment_label(phase: str, start: int, end: int) -> str:
    return f"{PHASE_EMOJI[phase]}{start}-{end}"


def _cycle_path(day: int, bounds: Dict[str, Tuple[int, int]], cycle_len: int) -> str:
    parts = []
    for phase in PHASE_ORDER:
        start, end = bounds[phase]
        if start > end:
            continue
        marker = "●" if start <= day <= end else "○"
        parts.append(f"{marker}{_phase_segment_label(phase, start, end)}")
    return " → ".join(parts) + f"\nToday marker: <b>day {day}/{cycle_len}</b>"


def _phase_window_values(
    center_day: int,
    cycle_len: int,
    bounds: Dict[str, Tuple[int, int]],
    key: str,
    *,
    radius: int = 3,
) -> List[int]:
    values: List[int] = []
    for offset in range(-radius, radius + 1):
        d = ((center_day - 1 + offset) % cycle_len) + 1
        values.append(_phase_stats(d, bounds)[key])
    return values


def _pick_by_day(day: int, options: List[str]) -> str:
    return options[(day - 1) % len(options)]


def _cue_pack(phase: str, day: int, stats: Dict[str, int]) -> Dict[str, str]:
    pack: Dict[str, str]
    if phase == "menstrual":
        pack = {
            "headline": _pick_by_day(day, [
                "Lower the load",
                "Lead with comfort",
                "Make the day easier",
            ]),
            "do": _pick_by_day(day, [
                "Take one practical thing off her plate without making her decide.",
                "Keep plans light and create a quieter evening than usual.",
                "Offer comfort first: food, warmth, rest, and less pressure.",
            ]),
            "say": _pick_by_day(day, [
                "I can make tonight easier for you.",
                "No pressure today, we can keep things simple.",
                "Tell me what would feel most comfortable right now.",
            ]),
            "avoid": _pick_by_day(day, [
                "Avoid piling on decisions or last-minute plans.",
                "Don't turn low energy into a debate about attitude.",
                "Avoid expecting the same pace as a high-energy day.",
            ]),
        }
    elif phase == "follicular":
        pack = {
            "headline": _pick_by_day(day, [
                "Use the lighter energy",
                "Say yes to momentum",
                "Keep it fresh and easy",
            ]),
            "do": _pick_by_day(day, [
                "Suggest something light and energizing: a walk, coffee, or a simple plan.",
                "Match the growing momentum with a positive, low-friction invitation.",
                "Use this window for plans, progress, or something playful together.",
            ]),
            "say": _pick_by_day(day, [
                "Want to do something light and fun later?",
                "You seem to have more energy today, let's use it well.",
                "This feels like a good day for something simple and nice together.",
            ]),
            "avoid": _pick_by_day(day, [
                "Avoid overcomplicating an easy day with too much structure.",
                "Don't stay passive if she seems open to movement or plans.",
                "Avoid treating today like a low-energy day if the vibe is clearly better.",
            ]),
        }
    elif phase == "ovulatory":
        pack = {
            "headline": _pick_by_day(day, [
                "Lean into connection",
                "Show up with warmth",
                "Make space for closeness",
            ]),
            "do": _pick_by_day(day, [
                "Plan a date, a thoughtful compliment, or quality time with your full attention.",
                "Be present, confident, and generous with affection or appreciation.",
                "Use today for shared time, open conversation, or something a bit special.",
            ]),
            "say": _pick_by_day(day, [
                "You look amazing today.",
                "Let's make time for us later.",
                "I love being around you when things feel this easy.",
            ]),
            "avoid": _pick_by_day(day, [
                "Avoid being distracted or half-present if she's clearly open to connection.",
                "Don't waste a good connection window on logistical stress.",
                "Avoid coldness when warmth would go a long way today.",
            ]),
        }
    else:
        pack = {
            "headline": _pick_by_day(day, [
                "Keep it low-pressure",
                "Lead with steadiness",
                "Make the day simpler",
            ]),
            "do": _pick_by_day(day, [
                "Reduce friction: fewer demands, simpler plans, and more practical support.",
                "Take initiative on something small so she doesn't have to manage it.",
                "Aim for steadiness and predictability rather than intensity or problem-solving.",
            ]),
            "say": _pick_by_day(day, [
                "I can handle this part, you don't need to carry it.",
                "Let's keep tonight simple.",
                "I'm on your side, we don't need to force anything today.",
            ]),
            "avoid": _pick_by_day(day, [
                "Avoid pushing for decisions or heavy conversations late in the day.",
                "Don't argue with the mood; lower the pressure instead.",
                "Avoid reading sensitivity as rejection or disinterest.",
            ]),
        }

    if stats["need_for_space"] >= 4:
        pack["headline"] = "Support gently, with room"
        pack["do"] = "Offer one concrete form of help, then give her more breathing room instead of staying on top of it."
        pack["say"] = "I'm here if you want me, and I can also give you some space."
        pack["avoid"] = "Avoid turning care into too many check-ins, follow-up questions, or emotional pressure."
    elif stats["energy"] <= 2 and stats["physical_comfort"] <= 2:
        pack["headline"] = "Protect her energy"
        pack["do"] = "Make the day physically easier: simplify plans, lower demands, and help her rest without needing to ask."
        pack["say"] = "Let's make today lighter. I'll take care of the practical part."
        pack["avoid"] = "Avoid expecting output, enthusiasm, or long conversations from a depleted day."
    elif stats["connection_openness"] >= 4 and stats["social"] >= 4:
        pack["headline"] = "Use the connection window"
        pack["do"] = "Choose a warm, active moment together: a date, a walk, flirting, or a slightly more open conversation."
        pack["say"] = "You feel especially easy to be close to today. Let's make time for us."
        pack["avoid"] = "Avoid wasting a naturally open day on pure logistics or being emotionally half-present."
    elif stats["irritability"] >= 4 and stats["focus"] <= 2:
        pack["headline"] = "Keep things simple and clear"
        pack["do"] = "Use short, practical communication and reduce the number of decisions that need attention today."
        pack["say"] = "We don't need to solve everything today. Let's keep this simple."
        pack["avoid"] = "Avoid layered conversations, debates, or asking her to process too much at once."

    return pack


def _extra_support_lines(phase: str, stats: Dict[str, int], hormones: Dict[str, int], day: int) -> Dict[str, str]:
    body = []
    relationship = []
    regulate = []

    if stats["physical_comfort"] <= 2:
        body.append("Prioritize physical comfort: warmth, rest, lighter plans, and easier food.")
    if stats["cravings"] >= 4:
        body.append("Keep snacks or an easy meal around so basic needs do not become extra friction.")
    if stats["energy"] <= 2:
        body.append("Aim for recovery over productivity today: fewer errands, less rushing, more margin.")
    if hormones["progesterone"] >= 65:
        regulate.append("Higher progesterone often pairs better with steadiness, sleep, and reduced stimulation.")
    if hormones["estrogen"] >= 70 and phase in ("follicular", "ovulatory"):
        relationship.append("This is a better window for plans, social time, shared momentum, or a date.")
    if stats["need_for_space"] >= 4:
        relationship.append("Support without hovering. Offer presence, then leave breathing room.")
    if stats["connection_openness"] >= 4:
        relationship.append("Warmth and active attention are more likely to land well today.")
    if stats["irritability"] >= 4 or stats["sensitivity"] >= 4:
        regulate.append("Keep your tone softer than usual and make fewer things urgent.")
    if stats["focus"] <= 2:
        regulate.append("Use short, concrete communication instead of layered conversations.")
    if phase == "ovulatory" and day % 2 == 0:
        relationship.append("Good day for appreciation, flirtation, or a more expressive check-in.")
    if phase == "menstrual":
        body.append("Simple comfort beats optimization today.")
    if phase == "luteal" and day % 2 == 1:
        regulate.append("Lower expectations on timing and avoid treating delays as a problem.")

    def choose(lines: List[str], fallback: str) -> str:
        return lines[0] if lines else fallback

    return {
        "body": choose(body, "Keep the body side easy today: food, rest, and less friction."),
        "relationship": choose(relationship, "Stay present and supportive without making the day heavier."),
        "regulate": choose(regulate, "The best move today is reducing pressure and keeping things simple."),
    }


def _cycle_snapshot(profile: UserProfile) -> Dict[str, Any]:
    today = _today_in_tz(profile.tz)
    start = dt.date.fromisoformat(profile.period_start)
    period_len = _compute_period_length(profile.period_start, profile.period_end)
    bounds = _phase_boundaries(profile.cycle_length, period_len)
    day = _cycle_day_for(today, start, profile.cycle_length)
    phase = _phase_for_cycle_day(day, bounds)
    stats = _phase_stats(day, bounds)
    hormones = _estimated_hormones(day, profile.cycle_length, bounds)

    next_change = None
    next_phase = None
    for ph, (_, b) in bounds.items():
        if ph != phase:
            continue
        if b < profile.cycle_length:
            delta = (b + 1) - day
            next_change = today + dt.timedelta(days=delta)
            next_phase = _phase_for_cycle_day(b + 1, bounds)
        break

    return {
        "today": today,
        "start": start,
        "period_len": period_len,
        "bounds": bounds,
        "day": day,
        "phase": phase,
        "stats": stats,
        "hormones": hormones,
        "next_change": next_change,
        "next_phase": next_phase,
    }


def _settings_stat_line(label: str, emoji: str, level: int, *, positive: bool = True) -> str:
    return f"{emoji} {label}: <b>{_level_word(level, positive=positive)}</b> {_bar(level)}"


def _feedback_ack(helpful: bool, phase: str, stats: Dict[str, int]) -> str:
    if helpful:
        if stats["connection_openness"] >= 4:
            return "Saved. This helps us learn what works in higher-connection windows."
        if stats["need_for_space"] >= 4:
            return "Saved. This helps us learn how to support better on more sensitive, space-needing days."
        return "Saved. This helps us learn which types of daily cues land best."
    if phase == "luteal" or stats["irritability"] >= 4:
        return "Saved. This tells us we should soften or simplify support on heavier days."
    if stats["physical_comfort"] <= 2:
        return "Saved. This tells us we should adjust more toward comfort and recovery."
    return "Saved. This helps us refine the model when the advice misses the moment."

# ----------------------------
# Rendering
# ----------------------------
async def render_today(profile: UserProfile) -> str:
    snap = _cycle_snapshot(profile)
    today = snap["today"]
    bounds = snap["bounds"]
    day = snap["day"]
    phase = snap["phase"]

    yday = today - dt.timedelta(days=1)
    yday_num = _cycle_day_for(yday, snap["start"], profile.cycle_length)
    now_stats = snap["stats"]
    hormones = snap["hormones"]
    prev_stats = _phase_stats(yday_num, bounds)
    cue = _cue_pack(phase, day, now_stats)
    support = _extra_support_lines(phase, now_stats, hormones, day)

    def stat_line(label: str, emoji: str, key: str):
        return f"{emoji} {label}: {_bar(now_stats[key])} {_arrow(now_stats[key], prev_stats[key])}"

    # Next phase change within current cycle
    change_txt = ""
    if snap["next_change"] and snap["next_phase"] and snap["next_phase"] != phase:
        change_txt = (
            f"\n\n⏭ Next shift: <b>{snap['next_change'].isoformat()}</b> → "
            f"{PHASE_NAME[snap['next_phase']]} {PHASE_EMOJI[snap['next_phase']]}"
        )

    return (
        f"<b>Today for {profile.partner_name}</b>\n"
        f"Day <b>{day}/{profile.cycle_length}</b> · <b>{PHASE_NAME[phase]}</b> {PHASE_EMOJI[phase]}\n"
        f"{_phase_summary_text(phase, now_stats)}\n\n"
        f"<b>Today’s cue</b>\n"
        f"<b>{cue['headline']}</b>\n"
        f"{cue['do']}\n\n"
        f"<b>Try saying</b>\n"
        f"“{cue['say']}”\n\n"
        f"<b>Avoid</b>\n"
        f"{cue['avoid']}\n\n"
        f"<b>More help</b>\n"
        f"• Body: {support['body']}\n"
        f"• Relationship: {support['relationship']}\n"
        f"• Regulation: {support['regulate']}\n\n"
        f"<b>Signals today</b>\n"
        f"⚡ Energy: <b>{_level_word(now_stats['energy'])}</b> {_bar(now_stats['energy'])}\n"
        f"🎭 Mood: <b>{_level_word(now_stats['mood'])}</b> {_bar(now_stats['mood'])}\n"
        f"💢 Irritability: <b>{_level_word(now_stats['irritability'], positive=False)}</b> {_bar(now_stats['irritability'])}\n"
        f"🧠 Focus: <b>{_level_word(now_stats['focus'])}</b> {_bar(now_stats['focus'])}\n"
        f"💞 Connection: <b>{_level_word(now_stats['connection_openness'])}</b> {_bar(now_stats['connection_openness'])}\n\n"
        f"<b>More detail</b>\n"
        f"{stat_line('Social drive', '🗣️', 'social')}\n"
        f"{stat_line('Cravings', '🍫', 'cravings')}\n"
        f"{stat_line('Sensitivity', '🫶', 'sensitivity')}\n"
        f"{stat_line('Need for space', '🌫️', 'need_for_space')}\n"
        f"{stat_line('Physical comfort', '🛋️', 'physical_comfort')}\n"
        f"\n<b>Estimated hormone picture</b>\n"
        f"Estrogen <b>{hormones['estrogen']}</b>/100 · "
        f"Progesterone <b>{hormones['progesterone']}</b>/100 · "
        f"LH <b>{hormones['lh']}</b>/100 · "
        f"FSH <b>{hormones['fsh']}</b>/100"
        f"{change_txt}"
        f"\n\n<b>Feedback</b>\n"
        f"Tap <b>{BTN_HELPFUL}</b> or <b>{BTN_NOT_HELPFUL}</b> so Daycue can learn which cues actually help."
        f"\n\nUse <b>/stats</b> or the <b>{BTN_STATS}</b> button for the full cycle view."
    )


async def render_settings(profile: UserProfile) -> str:
    snap = _cycle_snapshot(profile)
    phase = snap["phase"]
    day = snap["day"]
    stats = snap["stats"]
    desc = await copy_get(f"phase_desc_{phase}", phase=phase)

    next_shift = "No phase switch left in this cycle."
    if snap["next_change"] and snap["next_phase"]:
        next_shift = (
            f"<b>{snap['next_change'].isoformat()}</b> → "
            f"{PHASE_NAME[snap['next_phase']]} {PHASE_EMOJI[snap['next_phase']]}"
        )

    return (
        f"<b>Settings</b>\n\n"
        f"<b>Partner</b>\n"
        f"Name: <b>{profile.partner_name}</b>\n"
        f"Paused: <b>{'yes' if profile.paused else 'no'}</b>\n"
        f"Notify: <b>{profile.notify_time}</b> ({profile.tz})\n\n"
        f"<b>Cycle</b>\n"
        f"Today: <b>Day {day}/{profile.cycle_length}</b>\n"
        f"Phase: <b>{PHASE_NAME[phase]}</b> {PHASE_EMOJI[phase]}\n"
        f"Last period: <b>{profile.period_start}</b> → <b>{profile.period_end or 'unknown'}</b>\n"
        f"Estimated period length: <b>{snap['period_len']} days</b>\n"
        f"Next shift: {next_shift}\n\n"
        f"<b>Phase description</b>\n"
        f"{desc}\n\n"
        f"<b>Quick signal check</b>\n"
        f"{_settings_stat_line('Energy', '⚡', stats['energy'])}\n"
        f"{_settings_stat_line('Mood', '🎭', stats['mood'])}\n"
        f"{_settings_stat_line('Irritability', '💢', stats['irritability'], positive=False)}\n\n"
        f"Open <b>{BTN_STATS}</b> or use <b>/stats</b> for the full stat breakdown, cycle path, and hormone picture.\n\n"
        f"<b>Commands</b>\n"
        f"• /update_period START [END]\n"
        f"• /set_time HH:MM\n"
        f"• /set_cycle 21-35\n"
        f"• /pause or /resume\n"
        f"• /re_onboard"
    )


async def render_stats(profile: UserProfile) -> str:
    snap = _cycle_snapshot(profile)
    day = snap["day"]
    phase = snap["phase"]
    bounds = snap["bounds"]
    stats = snap["stats"]
    hormones = snap["hormones"]

    energy_curve = _spark(_phase_window_values(day, profile.cycle_length, bounds, "energy"))
    mood_curve = _spark(_phase_window_values(day, profile.cycle_length, bounds, "mood"))
    irrit_curve = _spark(_phase_window_values(day, profile.cycle_length, bounds, "irritability"))
    focus_curve = _spark(_phase_window_values(day, profile.cycle_length, bounds, "focus"))
    connect_curve = _spark(_phase_window_values(day, profile.cycle_length, bounds, "connection_openness"))

    return (
        f"<b>Partner stats for today</b>\n"
        f"{profile.partner_name} · Day <b>{day}/{profile.cycle_length}</b> · "
        f"<b>{PHASE_NAME[phase]}</b> {PHASE_EMOJI[phase]}\n\n"
        f"<b>Cycle path</b>\n"
        f"{_cycle_path(day, bounds, profile.cycle_length)}\n\n"
        f"<b>Current dimensions</b>\n"
        f"{_settings_stat_line('Energy', '⚡', stats['energy'])}\n"
        f"{_settings_stat_line('Mood', '🎭', stats['mood'])}\n"
        f"{_settings_stat_line('Social drive', '🗣️', stats['social'])}\n"
        f"{_settings_stat_line('Focus', '🧠', stats['focus'])}\n"
        f"{_settings_stat_line('Connection openness', '💞', stats['connection_openness'])}\n"
        f"{_settings_stat_line('Physical comfort', '🛋️', stats['physical_comfort'])}\n"
        f"{_settings_stat_line('Sensitivity', '🫶', stats['sensitivity'], positive=False)}\n"
        f"{_settings_stat_line('Need for space', '🌫️', stats['need_for_space'], positive=False)}\n"
        f"{_settings_stat_line('Cravings', '🍫', stats['cravings'], positive=False)}\n"
        f"{_settings_stat_line('Irritability', '💢', stats['irritability'], positive=False)}\n\n"
        f"<b>Trend window</b>\n"
        f"Energy   {energy_curve}\n"
        f"Mood     {mood_curve}\n"
        f"Irrit.   {irrit_curve}\n"
        f"Focus    {focus_curve}\n"
        f"Connect  {connect_curve}\n\n"
        f"<b>Estimated hormone picture</b>\n"
        f"• Estrogen: <b>{hormones['estrogen']}</b>/100\n"
        f"• Progesterone: <b>{hormones['progesterone']}</b>/100\n"
        f"• LH: <b>{hormones['lh']}</b>/100\n"
        f"• FSH: <b>{hormones['fsh']}</b>/100\n\n"
        f"<b>How to read this</b>\n"
        f"These are estimated support signals, not medical measurements. "
        f"They help the bot shape the tone, pressure level, and type of care for today.\n\n"
        f"Use <b>{BTN_TODAY}</b> for the daily cue and <b>{BTN_ABOUT}</b> for the current phase explanation."
    )


async def render_insights(profile: UserProfile) -> str:
    summary = await db_feedback_summary(profile.chat_id, 14)

    if summary["total"] == 0:
        return (
            f"<b>Insights</b>\n\n"
            f"No feedback yet for {profile.partner_name}.\n\n"
            f"Use <b>{BTN_HELPFUL}</b> or <b>{BTN_NOT_HELPFUL}</b> after daily cues to start training the model."
        )

    phase_lines = []
    for row in summary["by_phase"]:
        total = int(row["total"] or 0)
        helpful_count = int(row["helpful_count"] or 0)
        rate = round((helpful_count / total) * 100) if total else 0
        phase = row["phase"]
        phase_lines.append(
            f"• {PHASE_NAME.get(phase, phase)} {PHASE_EMOJI.get(phase, '')}: "
            f"{helpful_count}/{total} helpful ({rate}%)"
        )

    latest_lines = []
    for row in summary["latest"]:
        latest_lines.append(
            f"• {row['cue_date'].isoformat()} · "
            f"{'👍' if row['helpful'] else '👎'} · "
            f"{PHASE_NAME.get(row['phase'], row['phase'])} · "
            f"{row['cue_headline']}"
        )

    avg_energy = summary["avg_energy"] if summary["avg_energy"] is not None else "n/a"
    avg_irrit = summary["avg_irritability"] if summary["avg_irritability"] is not None else "n/a"
    avg_connect = summary["avg_connection"] if summary["avg_connection"] is not None else "n/a"

    return (
        f"<b>Insights</b>\n\n"
        f"<b>Last {summary['days']} days</b>\n"
        f"Responses: <b>{summary['total']}</b>\n"
        f"Helpful: <b>{summary['helpful_count']}</b> ({summary['helpful_rate']}%)\n"
        f"Avg energy on rated days: <b>{avg_energy}</b>\n"
        f"Avg irritability on rated days: <b>{avg_irrit}</b>\n"
        f"Avg connection on rated days: <b>{avg_connect}</b>\n\n"
        f"<b>By phase</b>\n"
        f"{chr(10).join(phase_lines)}\n\n"
        f"<b>Recent feedback</b>\n"
        f"{chr(10).join(latest_lines)}"
    )

async def render_about_phase(profile: UserProfile) -> str:
    snap = _cycle_snapshot(profile)
    phase = snap["phase"]

    desc = await copy_get(f"phase_desc_{phase}", phase=phase)
    return (
        f"<b>{PHASE_NAME[phase]} phase {PHASE_EMOJI[phase]}</b>\n\n"
        f"{desc}\n\n"
        f"<b>What usually works best</b>\n"
        f"{await copy_get(f'help_{phase}', phase=phase)}"
    )

async def render_forecast(profile: UserProfile, days: int = 7) -> str:
    snap = _cycle_snapshot(profile)
    today = snap["today"]
    bounds = snap["bounds"]
    start = snap["start"]

    lines = [f"<b>Next {days} days for {profile.partner_name}</b>\n"]
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
        cue = _cue_pack(ph, cd, st)
        hormones = _estimated_hormones(cd, profile.cycle_length, bounds)
        lines.append(
            f"{d.isoformat()} · Day {cd}/{profile.cycle_length} · {PHASE_NAME[ph]} {PHASE_EMOJI[ph]}\n"
            f"• Cue: {cue['headline']}\n"
            f"• Energy {_level_word(st['energy'])} · Irritability {_level_word(st['irritability'], positive=False)} · "
            f"Connection {_level_word(st['connection_openness'])}\n"
            f"• Est/P4: {hormones['estrogen']}/{hormones['progesterone']}"
        )

    lines.append("\n<b>Important shifts</b>")
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
        "Welcome to <b>Daycue</b>.\n\n"
        "We'll set up a simple daily support cue for your partner.\n\n"
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
    await _send(update, context, "✅ Setup complete.\n\n" + await render_today(profile))
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

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_stats(profile))

async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_insights(profile))

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_about_phase(profile))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    await _send(update, context, await render_settings(profile))

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


async def _record_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, helpful: bool):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)

    snap = _cycle_snapshot(profile)
    cue = _cue_pack(snap["phase"], snap["day"], snap["stats"])
    await db_upsert_feedback(
        chat_id=profile.chat_id,
        cue_date=snap["today"].isoformat(),
        cycle_day=snap["day"],
        phase=snap["phase"],
        helpful=helpful,
        cue_headline=cue["headline"],
        stats=snap["stats"],
    )
    await _send(
        update,
        context,
        (
            f"{'👍' if helpful else '👎'} {_feedback_ack(helpful, snap['phase'], snap['stats'])}\n\n"
            f"Today's cue saved as: <b>{cue['headline']}</b>\n"
            f"Day <b>{snap['day']}/{profile.cycle_length}</b> · "
            f"<b>{PHASE_NAME[snap['phase']]}</b> {PHASE_EMOJI[snap['phase']]}"
        ),
    )


async def cmd_helpful(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _record_feedback(update, context, True)


async def cmd_not_helpful(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _record_feedback(update, context, False)

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if t == BTN_TODAY:
        return await cmd_today(update, context)
    if t == BTN_FORECAST:
        return await cmd_forecast(update, context)
    if t == BTN_STATS:
        return await cmd_stats(update, context)
    if t == BTN_INSIGHTS:
        return await cmd_insights(update, context)
    if t == BTN_SETTINGS:
        return await cmd_settings(update, context)
    if t == BTN_ABOUT:
        return await cmd_about(update, context)
    if t == BTN_HELPFUL:
        return await cmd_helpful(update, context)
    if t == BTN_NOT_HELPFUL:
        return await cmd_not_helpful(update, context)
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
                tz = r["tz"] or os.getenv("TZ_DEFAULT", "Europe/Stockholm")

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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("re_onboard", cmd_re_onboard))
    app.add_handler(CommandHandler("set_time", cmd_set_time))
    app.add_handler(CommandHandler("set_cycle", cmd_set_cycle))
    app.add_handler(CommandHandler("update_period", cmd_update_period))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("helpful", cmd_helpful))
    app.add_handler(CommandHandler("not_helpful", cmd_not_helpful))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    return app

def main():
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
