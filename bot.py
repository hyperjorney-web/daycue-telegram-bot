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
BTN_LANGUAGE = "🌐 Language"
BTN_ABOUT = "📚 About phase"
BTN_HELPFUL = "👍 Helpful"
BTN_NOT_HELPFUL = "👎 Not helpful"

BUTTON_TEXT = {
    "en": {
        BTN_TODAY: "📍 Today",
        BTN_FORECAST: "🔮 Forecast",
        BTN_STATS: "📊 Stats",
        BTN_INSIGHTS: "🧾 Insights",
        BTN_SETTINGS: "⚙️ Settings",
        BTN_LANGUAGE: "🌐 Language",
        BTN_ABOUT: "📚 About phase",
        BTN_HELPFUL: "👍 Helpful",
        BTN_NOT_HELPFUL: "👎 Not helpful",
    },
    "sv": {
        BTN_TODAY: "📍 Idag",
        BTN_FORECAST: "🔮 Prognos",
        BTN_STATS: "📊 Statistik",
        BTN_INSIGHTS: "🧾 Insikter",
        BTN_SETTINGS: "⚙️ Installningar",
        BTN_LANGUAGE: "🌐 Sprak",
        BTN_ABOUT: "📚 Om fasen",
        BTN_HELPFUL: "👍 Hjalpsamt",
        BTN_NOT_HELPFUL: "👎 Inte hjalpsamt",
    },
    "ru": {
        BTN_TODAY: "📍 Сегодня",
        BTN_FORECAST: "🔮 Прогноз",
        BTN_STATS: "📊 Статы",
        BTN_INSIGHTS: "🧾 Инсайты",
        BTN_SETTINGS: "⚙️ Настройки",
        BTN_LANGUAGE: "🌐 Язык",
        BTN_ABOUT: "📚 О фазе",
        BTN_HELPFUL: "👍 Полезно",
        BTN_NOT_HELPFUL: "👎 Не полезно",
    },
}


def _btn(locale: str, key: str) -> str:
    return BUTTON_TEXT.get(locale, BUTTON_TEXT["en"]).get(key, BUTTON_TEXT["en"].get(key, key))


def _menu_kb(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [_btn(locale, BTN_TODAY), _btn(locale, BTN_FORECAST)],
            [_btn(locale, BTN_STATS), _btn(locale, BTN_INSIGHTS)],
            [_btn(locale, BTN_SETTINGS), _btn(locale, BTN_LANGUAGE)],
            [_btn(locale, BTN_ABOUT), _btn(locale, BTN_HELPFUL)],
            [_btn(locale, BTN_NOT_HELPFUL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Choose…",
    )


MENU_TEXTS = {label for labels in BUTTON_TEXT.values() for label in labels.values()}

# ----------------------------
# Data model
# ----------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
DATE_PARTIAL_RE = re.compile(r"^(\d{1,2})[.\-/ ](\d{1,2})(?:[.\-/ ](\d{2,4}))?$")

DATE_TODAY = "📅 Today"
DATE_YESTERDAY = "📅 Yesterday"
TIME_MORNING = "🌅 Morning (08:00)"
TIME_AFTER_WORK = "💼 After work (18:00)"
TIME_EVENING = "🌙 Evening (21:00)"
LANG_EN = "🇬🇧 English"
LANG_SV = "🇸🇪 Svenska"
LANG_RU = "🇷🇺 Русский"
LANG_MAP = {LANG_EN: "en", LANG_SV: "sv", LANG_RU: "ru"}

LANG_KB = ReplyKeyboardMarkup(
    [[LANG_EN, LANG_SV], [LANG_RU]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Language",
)

DATE_TEXT = {
    "en": {DATE_TODAY: "📅 Today", DATE_YESTERDAY: "📅 Yesterday"},
    "sv": {DATE_TODAY: "📅 Idag", DATE_YESTERDAY: "📅 Igar"},
    "ru": {DATE_TODAY: "📅 Сегодня", DATE_YESTERDAY: "📅 Вчера"},
}

TIME_TEXT = {
    "en": {TIME_MORNING: "🌅 Morning (08:00)", TIME_AFTER_WORK: "💼 After work (18:00)", TIME_EVENING: "🌙 Evening (21:00)"},
    "sv": {TIME_MORNING: "🌅 Morgon (08:00)", TIME_AFTER_WORK: "💼 Efter jobbet (18:00)", TIME_EVENING: "🌙 Kvall (21:00)"},
    "ru": {TIME_MORNING: "🌅 Утро (08:00)", TIME_AFTER_WORK: "💼 После работы (18:00)", TIME_EVENING: "🌙 Вечер (21:00)"},
}


def _date_btn(locale: str, key: str) -> str:
    return DATE_TEXT.get(locale, DATE_TEXT["en"]).get(key, DATE_TEXT["en"][key])


def _time_btn(locale: str, key: str) -> str:
    return TIME_TEXT.get(locale, TIME_TEXT["en"]).get(key, TIME_TEXT["en"][key])

def _date_kb(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_date_btn(locale, DATE_TODAY), _date_btn(locale, DATE_YESTERDAY)], [_btn(locale, BTN_TODAY), _btn(locale, BTN_SETTINGS)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Date",
    )


def _time_kb(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_time_btn(locale, TIME_MORNING), _time_btn(locale, TIME_AFTER_WORK)], [_time_btn(locale, TIME_EVENING), _btn(locale, BTN_SETTINGS)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Time",
    )

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
    locale: str = "en"
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
  locale TEXT NOT NULL DEFAULT 'en',
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
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locale TEXT NOT NULL DEFAULT 'en'")
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
            locale=(row["locale"] if "locale" in row else "en") or "en",
            paused=bool(row["paused"]),
        )

async def db_upsert_user(p: UserProfile) -> None:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users(chat_id, partner_name, partner_dob, period_start, period_end, cycle_length, notify_time, tz, locale, paused)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT(chat_id) DO UPDATE SET
              partner_name=EXCLUDED.partner_name,
              partner_dob=EXCLUDED.partner_dob,
              period_start=EXCLUDED.period_start,
              period_end=EXCLUDED.period_end,
              cycle_length=EXCLUDED.cycle_length,
              notify_time=EXCLUDED.notify_time,
              tz=EXCLUDED.tz,
              locale=EXCLUDED.locale,
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
            p.locale,
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


async def db_fetch_period_history(chat_id: int, limit: int = 3) -> List[Dict[str, Any]]:
    assert DB_POOL
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT period_start, period_end, created_at
            FROM period_log
            WHERE chat_id=$1
            ORDER BY period_start DESC, created_at DESC
            LIMIT $2
            """,
            chat_id,
            limit,
        )
    return [
        {
            "period_start": row["period_start"].isoformat(),
            "period_end": row["period_end"].isoformat() if row["period_end"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]

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

LANG_TEXT = {
    "en": {
        "welcome": "Welcome to <b>Daycue</b>.\n\nChoose a language first.",
        "lang_invalid": "Choose one of the language buttons below.",
        "nick": "1/6 - Enter a partner label or nickname.\nA real name is not required. Example: <b>Anna</b> or <b>Partner</b>",
        "nick_short": "Label too short.\n\n1/6 - Enter a partner label or nickname (2+ letters)",
        "dob": "2/6 - Partner DOB.\nUse <b>YYYY-MM-DD</b> or <b>DD.MM.YYYY</b>, or type <b>skip</b>.",
        "start": "3/6 - Last period START date.\nYou can use <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, or tap a quick option below.",
        "end": "4/6 - Last period END date or type <b>skip</b>.\nSame formats work here too: <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>.",
        "cycle": "5/6 - Cycle length in days (21-35). Example: 28",
        "time": "6/6 - Daily notification time.\nTap a quick option or enter your own <b>HH:MM</b>.\nExamples: <b>08:00</b>, <b>18:00</b>, <b>21:00</b>.",
        "setup_done": "✅ Setup complete.",
        "cancelled": "Onboarding cancelled.",
    },
    "sv": {
        "welcome": "Valkommen till <b>Daycue</b>.\n\nValj sprak forst.",
        "lang_invalid": "Valj ett av sprakknapparna nedan.",
        "nick": "1/6 - Ange ett namn eller en etikett for partnern.\nRiktigt namn behovs inte. Exempel: <b>Anna</b> eller <b>Partner</b>",
        "nick_short": "For kort namn.\n\n1/6 - Ange ett namn eller en etikett (minst 2 tecken)",
        "dob": "2/6 - Partnerns fodelsedatum.\nAnvand <b>YYYY-MM-DD</b> eller <b>DD.MM.YYYY</b>, eller skriv <b>skip</b>.",
        "start": "3/6 - Startdatum for senaste mens.\nDu kan skriva <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, eller valja nedan.",
        "end": "4/6 - Slutdatum for senaste mens eller skriv <b>skip</b>.\nSamma format fungerar har: <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>.",
        "cycle": "5/6 - Cykellangd i dagar (21-35). Exempel: 28",
        "time": "6/6 - Tid for daglig notis.\nValj ett snabbt alternativ eller skriv <b>HH:MM</b> sjalv.\nExempel: <b>08:00</b>, <b>18:00</b>, <b>21:00</b>.",
        "setup_done": "✅ Installning klar.",
        "cancelled": "Onboarding avbruten.",
    },
    "ru": {
        "welcome": "Добро пожаловать в <b>Daycue</b>.\n\nСначала выбери язык.",
        "lang_invalid": "Выбери один из языков ниже.",
        "nick": "1/6 - Введи ярлык или ник для партнера.\nНастоящее имя не обязательно. Например: <b>Anna</b> или <b>Partner</b>",
        "nick_short": "Слишком коротко.\n\n1/6 - Введи ярлык или ник (минимум 2 символа)",
        "dob": "2/6 - Дата рождения партнера.\nМожно <b>YYYY-MM-DD</b> или <b>DD.MM.YYYY</b>, либо <b>skip</b>.",
        "start": "3/6 - Дата начала последних месячных.\nМожно ввести <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b> или выбрать ниже.",
        "end": "4/6 - Дата окончания последних месячных или <b>skip</b>.\nПодойдут форматы <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>.",
        "cycle": "5/6 - Длина цикла в днях (21-35). Например: 28",
        "time": "6/6 - Время ежедневного уведомления.\nВыбери готовый вариант или введи свое <b>HH:MM</b>.\nПримеры: <b>08:00</b>, <b>18:00</b>, <b>21:00</b>.",
        "setup_done": "✅ Настройка завершена.",
        "cancelled": "Онбординг отменен.",
    },
}

UI_TEXT = {
    "en": {
        "settings": "Settings",
        "partner": "Partner",
        "name": "Name",
        "language": "Language",
        "paused": "Paused",
        "yes": "yes",
        "no": "no",
        "notify": "Notify",
        "cycle": "Cycle",
        "today_day": "Today",
        "phase": "Phase",
        "last_period": "Last period",
        "estimated_period_length": "Estimated period length",
        "next_shift": "Next shift",
        "no_next_shift": "No phase switch left in this cycle.",
        "phase_description": "Phase description",
        "quick_signal_check": "Quick signal check",
        "recent_period_history": "Recent period history",
        "no_history": "• No period history yet.",
        "settings_stats_hint": "Open <b>{stats}</b> or use <b>/stats</b> for the full stat breakdown, cycle path, and hormone picture.",
        "commands": "Commands",
        "today_for": "Today for {name}",
        "todays_cue": "Today's cue",
        "try_saying": "Try saying",
        "avoid": "Avoid",
        "more_help": "More help",
        "body": "Body",
        "relationship": "Relationship",
        "regulation": "Regulation",
        "signals_today": "Signals today",
        "more_detail": "More detail",
        "estimated_hormone_picture": "Estimated hormone picture",
        "feedback": "Feedback",
        "feedback_hint": "Tap <b>{helpful}</b> or <b>{not_helpful}</b> so Daycue can learn which cues actually help.",
        "today_stats_hint": "Use <b>/stats</b> or the <b>{stats}</b> button for the full cycle view.",
        "stats_title": "Partner stats for today",
        "cycle_path": "Cycle path",
        "current_dimensions": "Current dimensions",
        "trend_window": "Trend window",
        "how_to_read_this": "How to read this",
        "stats_explainer": "These are estimated support signals, not medical measurements. They help the bot shape the tone, pressure level, and type of care for today.",
        "stats_hint": "Use <b>{today}</b> for the daily cue and <b>{about}</b> for the current phase explanation.",
        "insights": "Insights",
        "no_feedback_for": "No feedback yet for {name}.",
        "insights_train_hint": "Use <b>{helpful}</b> or <b>{not_helpful}</b> after daily cues to start training the model.",
        "last_days": "Last {days} days",
        "responses": "Responses",
        "helpful_count": "Helpful",
        "avg_energy": "Avg energy on rated days",
        "avg_irritability": "Avg irritability on rated days",
        "avg_connection": "Avg connection on rated days",
        "by_phase": "By phase",
        "recent_feedback": "Recent feedback",
        "what_works_best": "What usually works best",
        "forecast_title": "Next {days} days for {name}",
        "important_shifts": "Important shifts",
        "no_phase_switch": "• No phase switch within this window.",
        "fertility_window": "Fertility window",
        "no_fertility_window": "• No high-fertility day inside this forecast window.",
        "switches_to": "switches to",
        "cue": "Cue",
        "energy": "Energy",
        "irritability": "Irritability",
        "connection": "Connection",
        "fertility": "Fertility",
        "day_marker": "Today marker: <b>day {day}/{cycle_len}</b>",
        "language_updated": "✅ Language updated to <b>{locale}</b>.",
        "social_drive": "Social drive",
        "focus": "Focus",
        "connection_openness": "Connection openness",
        "physical_comfort": "Physical comfort",
        "sensitivity": "Sensitivity",
        "need_for_space": "Need for space",
        "cravings": "Cravings",
        "mood": "Mood",
        "trend_energy": "Energy",
        "trend_mood": "Mood",
        "trend_irrit": "Irrit.",
        "trend_focus": "Focus",
        "trend_connect": "Connect",
        "hormone_estrogen": "Estrogen",
        "hormone_progesterone": "Progesterone",
        "unknown": "unknown",
        "day_short": "Day",
        "days_unit": "days",
        "level_low": "low",
        "level_steady": "steady",
        "level_moderate": "moderate",
        "level_high": "high",
        "updated": "✅ Updated.",
        "paused_pings": "⏸ Paused daily pings.",
        "resumed_pings": "▶️ Resumed daily pings.",
        "choose_language": "Choose a language.",
        "usage_set_time": "Usage: /set_time HH:MM\nExamples: /set_time 08:00, /set_time evening",
        "usage_set_cycle": "Usage: /set_cycle 21-35",
        "cycle_length_invalid": "Cycle length should be 21-35.",
        "usage_set_lang": "Usage: /set_lang en|sv|ru",
        "usage_dates_short": "Dates can be short.\nExamples: /update_period 5.4 9.4 or /update_period 2026-04-05",
        "end_before_start_short": "END cannot be before START.",
        "update_period_intro": "Update period.\n\nStep 1/2 - Send the <b>start date</b>.\nExamples: <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, or use quick date buttons.",
        "update_period_step1": "Step 1/2 - Send the <b>start date</b>.",
        "update_period_step2": "Step 2/2 - Send the <b>end date</b>, or type <b>skip</b> if you only want to record the start.",
        "update_period_step2_short": "Step 2/2 - Send the <b>end date</b>, or type <b>skip</b>.",
        "invalid_date_start": "Invalid date.\n\nTry <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, or quick date buttons.",
        "invalid_date_end": "Invalid date.\n\nTry <b>9.4</b>, <b>09/04</b>, <b>2026-04-09</b>, or type <b>skip</b>.",
        "period_saved": "✅ Period updated and added to history.",
        "period_end_before_start": "End date cannot be before start date. Try again or type <b>skip</b>.",
        "invalid_dob": "Invalid date.\n\nUse <b>YYYY-MM-DD</b> or <b>DD.MM.YYYY</b>, or type <b>skip</b>.",
        "invalid_end_onboarding": "Invalid date.\n\nUse <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>, or type <b>skip</b>.",
        "end_before_start_onboarding": "End date can't be before start date.\n\n4/6 - Try again.",
        "enter_cycle_number": "Enter a number 21-35.\n\n5/6 - Cycle length in days (21-35).",
        "invalid_time": "Time format should be <b>HH:MM</b> or choose a preset like <b>Morning</b> or <b>After work</b>.",
        "cue_saved": "Today's cue saved as: <b>{headline}</b>",
        "use_menu": "Use the menu buttons, or type /start.",
    },
    "sv": {
        "settings": "Installningar",
        "partner": "Partner",
        "name": "Namn",
        "language": "Sprak",
        "paused": "Pausad",
        "yes": "ja",
        "no": "nej",
        "notify": "Notis",
        "cycle": "Cykel",
        "today_day": "Idag",
        "phase": "Fas",
        "last_period": "Senaste mens",
        "estimated_period_length": "Beraknad menslangd",
        "next_shift": "Nasta skifte",
        "no_next_shift": "Ingen fasvaxling kvar i den har cykeln.",
        "phase_description": "Fasbeskrivning",
        "quick_signal_check": "Snabb signalcheck",
        "recent_period_history": "Senaste periodhistorik",
        "no_history": "• Ingen historik an.",
        "settings_stats_hint": "Oppna <b>{stats}</b> eller anvand <b>/stats</b> for full statistik, cykelvag och hormonbild.",
        "commands": "Kommandon",
        "today_for": "Idag for {name}",
        "todays_cue": "Dagens cue",
        "try_saying": "Prova att saga",
        "avoid": "Undvik",
        "more_help": "Mer hjalp",
        "body": "Kropp",
        "relationship": "Relation",
        "regulation": "Reglering",
        "signals_today": "Signaler idag",
        "more_detail": "Mer detalj",
        "estimated_hormone_picture": "Uppskattad hormonbild",
        "feedback": "Feedback",
        "feedback_hint": "Tryck <b>{helpful}</b> eller <b>{not_helpful}</b> sa Daycue kan lara sig vilka cues som faktiskt hjalper.",
        "today_stats_hint": "Anvand <b>/stats</b> eller knappen <b>{stats}</b> for full cykelvy.",
        "stats_title": "Partnerstatistik idag",
        "cycle_path": "Cykelvag",
        "current_dimensions": "Nuvarande dimensioner",
        "trend_window": "Trendfonster",
        "how_to_read_this": "Sa lases detta",
        "stats_explainer": "Detta ar uppskattade stodsignaler, inte medicinska matningar. De hjalper boten att forma ton, tryckniva och typ av omtanke for idag.",
        "stats_hint": "Anvand <b>{today}</b> for dagens cue och <b>{about}</b> for forklaring av aktuell fas.",
        "insights": "Insikter",
        "no_feedback_for": "Ingen feedback an for {name}.",
        "insights_train_hint": "Anvand <b>{helpful}</b> eller <b>{not_helpful}</b> efter dagliga cues for att borja trana modellen.",
        "last_days": "Senaste {days} dagarna",
        "responses": "Svar",
        "helpful_count": "Hjalpsamt",
        "avg_energy": "Snittenergi pa bedomda dagar",
        "avg_irritability": "Snittirritation pa bedomda dagar",
        "avg_connection": "Snittkontakt pa bedomda dagar",
        "by_phase": "Per fas",
        "recent_feedback": "Senaste feedback",
        "what_works_best": "Det som oftast fungerar bast",
        "forecast_title": "Nasta {days} dagar for {name}",
        "important_shifts": "Viktiga skiften",
        "no_phase_switch": "• Ingen fasvaxling inom detta fonster.",
        "fertility_window": "Fertilitetsfonster",
        "no_fertility_window": "• Ingen dag med hog fertilitet i prognosen.",
        "switches_to": "byter till",
        "cue": "Cue",
        "energy": "Energi",
        "irritability": "Irritation",
        "connection": "Kontakt",
        "fertility": "Fertilitet",
        "day_marker": "Markor idag: <b>dag {day}/{cycle_len}</b>",
        "language_updated": "✅ Spraket uppdaterades till <b>{locale}</b>.",
        "social_drive": "Social drift",
        "focus": "Fokus",
        "connection_openness": "Oppning for kontakt",
        "physical_comfort": "Fysisk komfort",
        "sensitivity": "Kanslighet",
        "need_for_space": "Behov av utrymme",
        "cravings": "Sug",
        "mood": "Humor",
        "trend_energy": "Energi",
        "trend_mood": "Humor",
        "trend_irrit": "Irrit.",
        "trend_focus": "Fokus",
        "trend_connect": "Kontakt",
        "hormone_estrogen": "Ostrogen",
        "hormone_progesterone": "Progesteron",
        "unknown": "okant",
        "day_short": "Dag",
        "days_unit": "dagar",
        "level_low": "lag",
        "level_steady": "stabil",
        "level_moderate": "medel",
        "level_high": "hog",
        "updated": "✅ Uppdaterat.",
        "paused_pings": "⏸ Dagliga notiser pausade.",
        "resumed_pings": "▶️ Dagliga notiser aterupptagna.",
        "choose_language": "Valj sprak.",
        "usage_set_time": "Anvandning: /set_time HH:MM\nExempel: /set_time 08:00, /set_time evening",
        "usage_set_cycle": "Anvandning: /set_cycle 21-35",
        "cycle_length_invalid": "Cykellangden ska vara 21-35.",
        "usage_set_lang": "Anvandning: /set_lang en|sv|ru",
        "usage_dates_short": "Datum kan vara korta.\nExempel: /update_period 5.4 9.4 eller /update_period 2026-04-05",
        "end_before_start_short": "SLUT kan inte vara fore START.",
        "update_period_intro": "Uppdatera period.\n\nSteg 1/2 - Skicka <b>startdatum</b>.\nExempel: <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, eller anvand snabbknapparna.",
        "update_period_step1": "Steg 1/2 - Skicka <b>startdatum</b>.",
        "update_period_step2": "Steg 2/2 - Skicka <b>slutdatum</b>, eller skriv <b>skip</b> om du bara vill spara starten.",
        "update_period_step2_short": "Steg 2/2 - Skicka <b>slutdatum</b>, eller skriv <b>skip</b>.",
        "invalid_date_start": "Ogiltigt datum.\n\nProva <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, eller snabbknapparna.",
        "invalid_date_end": "Ogiltigt datum.\n\nProva <b>9.4</b>, <b>09/04</b>, <b>2026-04-09</b>, eller skriv <b>skip</b>.",
        "period_saved": "✅ Perioden uppdaterades och lades till i historiken.",
        "period_end_before_start": "Slutdatum kan inte vara fore startdatum. Forsok igen eller skriv <b>skip</b>.",
        "invalid_dob": "Ogiltigt datum.\n\nAnvand <b>YYYY-MM-DD</b> eller <b>DD.MM.YYYY</b>, eller skriv <b>skip</b>.",
        "invalid_end_onboarding": "Ogiltigt datum.\n\nAnvand <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>, eller skriv <b>skip</b>.",
        "end_before_start_onboarding": "Slutdatum kan inte vara fore startdatum.\n\n4/6 - Forsok igen.",
        "enter_cycle_number": "Ange ett tal 21-35.\n\n5/6 - Cykellangd i dagar (21-35).",
        "invalid_time": "Tidsformatet ska vara <b>HH:MM</b> eller valj en preset som <b>Morgon</b> eller <b>Efter jobbet</b>.",
        "cue_saved": "Dagens cue sparades som: <b>{headline}</b>",
        "use_menu": "Anvand menyknapparna eller skriv /start.",
    },
    "ru": {
        "settings": "Настройки",
        "partner": "Партнер",
        "name": "Имя",
        "language": "Язык",
        "paused": "Пауза",
        "yes": "да",
        "no": "нет",
        "notify": "Уведомление",
        "cycle": "Цикл",
        "today_day": "Сегодня",
        "phase": "Фаза",
        "last_period": "Последние месячные",
        "estimated_period_length": "Примерная длина менструации",
        "next_shift": "Следующий переход",
        "no_next_shift": "В этом цикле больше не будет смены фазы.",
        "phase_description": "Описание фазы",
        "quick_signal_check": "Быстрый срез",
        "recent_period_history": "Последняя история цикла",
        "no_history": "• Истории пока нет.",
        "settings_stats_hint": "Открой <b>{stats}</b> или используй <b>/stats</b> для полного разбора статов, пути цикла и гормональной картины.",
        "commands": "Команды",
        "today_for": "Сегодня для {name}",
        "todays_cue": "Совет дня",
        "try_saying": "Можно сказать",
        "avoid": "Избегай",
        "more_help": "Дополнительно",
        "body": "Тело",
        "relationship": "Отношения",
        "regulation": "Регуляция",
        "signals_today": "Сигналы на сегодня",
        "more_detail": "Детальнее",
        "estimated_hormone_picture": "Примерная гормональная картина",
        "feedback": "Фидбек",
        "feedback_hint": "Нажми <b>{helpful}</b> или <b>{not_helpful}</b>, чтобы Daycue понял, какие советы реально помогают.",
        "today_stats_hint": "Используй <b>/stats</b> или кнопку <b>{stats}</b> для полного вида цикла.",
        "stats_title": "Статы партнера на сегодня",
        "cycle_path": "Путь цикла",
        "current_dimensions": "Текущие параметры",
        "trend_window": "Окно тренда",
        "how_to_read_this": "Как это читать",
        "stats_explainer": "Это оценочные сигналы поддержки, а не медицинские измерения. Они помогают боту подбирать тон, уровень давления и тип заботы на сегодня.",
        "stats_hint": "Используй <b>{today}</b> для ежедневного совета и <b>{about}</b> для описания текущей фазы.",
        "insights": "Инсайты",
        "no_feedback_for": "Пока нет фидбека для {name}.",
        "insights_train_hint": "Используй <b>{helpful}</b> или <b>{not_helpful}</b> после ежедневных советов, чтобы начать обучать модель.",
        "last_days": "Последние {days} дней",
        "responses": "Ответы",
        "helpful_count": "Полезно",
        "avg_energy": "Средняя энергия в оцененные дни",
        "avg_irritability": "Средняя раздражительность в оцененные дни",
        "avg_connection": "Средняя открытость к контакту",
        "by_phase": "По фазам",
        "recent_feedback": "Последний фидбек",
        "what_works_best": "Что обычно работает лучше",
        "forecast_title": "Следующие {days} дней для {name}",
        "important_shifts": "Важные переходы",
        "no_phase_switch": "• В этом окне нет смены фазы.",
        "fertility_window": "Окно фертильности",
        "no_fertility_window": "• В этом прогнозе нет дней с высокой фертильностью.",
        "switches_to": "переход в",
        "cue": "Совет",
        "energy": "Энергия",
        "irritability": "Раздражительность",
        "connection": "Контакт",
        "fertility": "Фертильность",
        "day_marker": "Маркер сегодня: <b>день {day}/{cycle_len}</b>",
        "language_updated": "✅ Язык обновлен на <b>{locale}</b>.",
        "social_drive": "Социальность",
        "focus": "Фокус",
        "connection_openness": "Открытость к контакту",
        "physical_comfort": "Физический комфорт",
        "sensitivity": "Чувствительность",
        "need_for_space": "Потребность в пространстве",
        "cravings": "Тяга",
        "mood": "Настроение",
        "trend_energy": "Энергия",
        "trend_mood": "Настр.",
        "trend_irrit": "Раздр.",
        "trend_focus": "Фокус",
        "trend_connect": "Контакт",
        "hormone_estrogen": "Эстроген",
        "hormone_progesterone": "Прогестерон",
        "unknown": "неизвестно",
        "day_short": "День",
        "days_unit": "дней",
        "level_low": "низкий",
        "level_steady": "ровный",
        "level_moderate": "умеренный",
        "level_high": "высокий",
        "updated": "✅ Обновлено.",
        "paused_pings": "⏸ Ежедневные уведомления поставлены на паузу.",
        "resumed_pings": "▶️ Ежедневные уведомления снова включены.",
        "choose_language": "Выбери язык.",
        "usage_set_time": "Использование: /set_time HH:MM\nПримеры: /set_time 08:00, /set_time evening",
        "usage_set_cycle": "Использование: /set_cycle 21-35",
        "cycle_length_invalid": "Длина цикла должна быть 21-35.",
        "usage_set_lang": "Использование: /set_lang en|sv|ru",
        "usage_dates_short": "Даты могут быть короткими.\nПримеры: /update_period 5.4 9.4 или /update_period 2026-04-05",
        "end_before_start_short": "КОНЕЦ не может быть раньше НАЧАЛА.",
        "update_period_intro": "Обновление периода.\n\nШаг 1/2 - Отправь <b>дату начала</b>.\nПримеры: <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b>, или используй быстрые кнопки.",
        "update_period_step1": "Шаг 1/2 - Отправь <b>дату начала</b>.",
        "update_period_step2": "Шаг 2/2 - Отправь <b>дату окончания</b> или напиши <b>skip</b>, если хочешь сохранить только начало.",
        "update_period_step2_short": "Шаг 2/2 - Отправь <b>дату окончания</b> или напиши <b>skip</b>.",
        "invalid_date_start": "Неверная дата.\n\nПопробуй <b>5.4</b>, <b>05/04</b>, <b>2026-04-05</b> или быстрые кнопки.",
        "invalid_date_end": "Неверная дата.\n\nПопробуй <b>9.4</b>, <b>09/04</b>, <b>2026-04-09</b> или напиши <b>skip</b>.",
        "period_saved": "✅ Период обновлен и добавлен в историю.",
        "period_end_before_start": "Дата окончания не может быть раньше даты начала. Попробуй снова или напиши <b>skip</b>.",
        "invalid_dob": "Неверная дата.\n\nИспользуй <b>YYYY-MM-DD</b> или <b>DD.MM.YYYY</b>, либо <b>skip</b>.",
        "invalid_end_onboarding": "Неверная дата.\n\nИспользуй <b>8.4</b>, <b>08/04</b>, <b>2026-04-08</b>, или напиши <b>skip</b>.",
        "end_before_start_onboarding": "Дата окончания не может быть раньше даты начала.\n\n4/6 - Попробуй снова.",
        "enter_cycle_number": "Введи число 21-35.\n\n5/6 - Длина цикла в днях (21-35).",
        "invalid_time": "Формат времени должен быть <b>HH:MM</b> или выбери пресет вроде <b>Утро</b> или <b>После работы</b>.",
        "cue_saved": "Совет дня сохранен как: <b>{headline}</b>",
        "use_menu": "Используй кнопки меню или напиши /start.",
    },
}



def _lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("locale", "en")


def _lt(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    locale = _lang(context)
    return LANG_TEXT.get(locale, LANG_TEXT["en"]).get(key, LANG_TEXT["en"][key])


def _ui(locale: str, key: str, **kwargs) -> str:
    text = UI_TEXT.get(locale, UI_TEXT["en"]).get(key, UI_TEXT["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def _skip_words(locale: str) -> set[str]:
    return {
        "en": {"skip"},
        "sv": {"skip", "hoppa over", "hoppaöver", "hoppa-over"},
        "ru": {"skip", "пропустить", "пропуск"},
    }.get(locale, {"skip"})


def _sync_locale(context: ContextTypes.DEFAULT_TYPE, profile: UserProfile):
    context.user_data["locale"] = profile.locale or "en"


def _button_key(text: str) -> Optional[str]:
    normalized = _norm(text)
    for labels in BUTTON_TEXT.values():
        for key, label in labels.items():
            if normalized == label:
                return key
    return None

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


def _parse_notify_input(s: str) -> Optional[str]:
    text = _norm(s)
    preset_map = {"morning": "08:00", "after work": "18:00", "evening": "21:00"}
    for labels in TIME_TEXT.values():
        preset_map[labels[TIME_MORNING]] = "08:00"
        preset_map[labels[TIME_AFTER_WORK]] = "18:00"
        preset_map[labels[TIME_EVENING]] = "21:00"
    preset_map.update({
        "morgon": "08:00",
        "efter jobbet": "18:00",
        "kvall": "21:00",
        "утро": "08:00",
        "после работы": "18:00",
        "вечер": "21:00",
    })
    lowered = text.lower()
    if lowered in preset_map:
        return preset_map[lowered]
    if text in preset_map:
        return preset_map[text]
    if TIME_RE.match(text):
        _parse_time_hhmm(text)
        return text
    return None


def _parse_flexible_date_input(s: str, *, tz_name: str, allow_without_year: bool = False) -> Optional[str]:
    text = _norm(s).lower()
    today = _today_in_tz(tz_name)

    all_today = {"today", "idag", "сегодня"} | {v[DATE_TODAY].lower() for v in DATE_TEXT.values()}
    all_yesterday = {"yesterday", "igar", "вчера"} | {v[DATE_YESTERDAY].lower() for v in DATE_TEXT.values()}
    if text in all_today:
        return today.isoformat()
    if text in all_yesterday:
        return (today - dt.timedelta(days=1)).isoformat()

    if DATE_RE.match(text):
        return dt.date.fromisoformat(text).isoformat()

    match = DATE_PARTIAL_RE.match(text)
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))
    year_raw = match.group(3)

    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
        return dt.date(year, month, day).isoformat()

    if not allow_without_year:
        return None

    inferred = dt.date(today.year, month, day)
    if inferred > today:
        inferred = dt.date(today.year - 1, month, day)
    return inferred.isoformat()

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

PHASE_NAME_I18N = {
    "en": PHASE_NAME,
    "sv": {"menstrual": "Menstruell", "follicular": "Follikular", "ovulatory": "Ovulatorisk", "luteal": "Luteal"},
    "ru": {"menstrual": "Менструальная", "follicular": "Фолликулярная", "ovulatory": "Овуляторная", "luteal": "Лютеиновая"},
}

FERTILITY_I18N = {
    "en": {"low": "low", "elevated": "elevated", "high": "high", "peak": "peak"},
    "sv": {"low": "lag", "elevated": "forhojd", "high": "hog", "peak": "topp"},
    "ru": {"low": "низкая", "elevated": "повышенная", "high": "высокая", "peak": "пик"},
}


def _phase_name(phase: str, locale: str) -> str:
    return PHASE_NAME_I18N.get(locale, PHASE_NAME_I18N["en"]).get(phase, PHASE_NAME.get(phase, phase))


def _fertility_text(level: str, locale: str) -> str:
    return FERTILITY_I18N.get(locale, FERTILITY_I18N["en"]).get(level, level)

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


def _level_word(level: int, *, positive: bool = True, locale: str = "en") -> str:
    if positive:
        if level <= 2:
            key = "level_low"
        elif level == 3:
            key = "level_steady"
        else:
            key = "level_high"
        return _ui(locale, key)
    if level <= 2:
        key = "level_low"
    elif level == 3:
        key = "level_moderate"
    else:
        key = "level_high"
    return _ui(locale, key)


def _phase_summary_text(phase: str, stats: Dict[str, int], locale: str = "en") -> str:
    if stats["physical_comfort"] <= 2 and stats["energy"] <= 2:
        return _locale_pack(locale, "Body comfort is lower today and energy is limited. Softer pacing, warmth, and fewer demands will help most.", "Kroppslig komfort ar lagre idag och energin ar begransad. Mjukare tempo, varme och farre krav hjalper mest.", "Телесный комфорт сегодня ниже, а энергия ограничена. Мягкий темп, тепло и меньше требований помогут лучше всего.")
    if stats["connection_openness"] >= 4 and stats["social"] >= 4:
        return _locale_pack(locale, "Connection looks easier today. Shared time, warmth, and a more open tone are likely to land well.", "Kontakt verkar lattare idag. Gemensam tid, varme och en mer oppen ton landar troligen bra.", "Сегодня контакт дается легче. Совместное время, тепло и более открытый тон, скорее всего, зайдут хорошо.")
    if stats["need_for_space"] >= 4 and stats["sensitivity"] >= 4:
        return _locale_pack(locale, "Sensitivity is elevated today. Support matters, but it should come with more room and less pressure.", "Kansligheten ar forhojd idag. Stod spelar roll, men det bor komma med mer utrymme och mindre press.", "Сегодня повышенная чувствительность. Поддержка важна, но с большим пространством и меньшим давлением.")
    if phase == "menstrual":
        return _locale_pack(locale, "Lower energy and higher sensitivity today. Calm, warmth, and fewer demands will land best.", "Lagre energi och hogre kanslighet idag. Lugn, varme och farre krav landar bast.", "Сегодня ниже энергия и выше чувствительность. Лучше всего зайдут спокойствие, тепло и меньше требований.")
    if phase == "follicular":
        return _locale_pack(locale, "Energy is building. Lighter plans, movement, and positive momentum usually work well.", "Energien byggs upp. Lattare planer, rorelse och positivt momentum fungerar ofta bra.", "Энергия растет. Обычно хорошо работают более легкие планы, движение и позитивный импульс.")
    if phase == "ovulatory":
        return _locale_pack(locale, "Connection is easier today. Confidence, warmth, and shared time tend to land well.", "Kontakt ar enklare idag. Trygghet, varme och gemensam tid brukar landa bra.", "Сегодня легче идти в близость. Уверенность, тепло и совместное время обычно заходят хорошо.")
    if stats["irritability"] >= 4:
        return _locale_pack(locale, "Emotional load may be higher today. Keep things low-pressure, steady, and practical.", "Den emotionella belastningen kan vara hogre idag. Hall allt mindre pressat, stabilt och praktiskt.", "Эмоциональная нагрузка сегодня может быть выше. Держи всё менее напряженным, стабильным и практичным.")
    return _locale_pack(locale, "A steadier, lower-friction day. Predictability and gentle support will help more than pressure.", "En stadigare dag med mindre friktion. Forutsagbarhet och mjukt stod hjalper mer an press.", "Более ровный день с меньшим трением. Предсказуемость и мягкая поддержка помогут больше, чем давление.")


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
    return " → ".join(parts)


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


def _fertility_label(day: int, bounds: Dict[str, Tuple[int, int]]) -> str:
    ov_start, ov_end = bounds["ovulatory"]
    ov_center = (ov_start + ov_end) // 2
    distance = abs(day - ov_center)
    if distance == 0:
        return "peak"
    if distance <= 1:
        return "high"
    if distance <= 2:
        return "elevated"
    return "low"


def _pick_by_day(day: int, options: List[str]) -> str:
    return options[(day - 1) % len(options)]


def _locale_pack(locale: str, en: str, sv: str, ru: str) -> str:
    return {"en": en, "sv": sv, "ru": ru}.get(locale, en)


def _cue_pack(phase: str, day: int, stats: Dict[str, int], locale: str = "en") -> Dict[str, str]:
    pack: Dict[str, str]
    if phase == "menstrual":
        pack = {
            "headline": _pick_by_day(day, [
                _locale_pack(locale, "Lower the load", "Sank belastningen", "Снизь нагрузку"),
                _locale_pack(locale, "Lead with comfort", "Led med komfort", "Сделай упор на комфорт"),
                _locale_pack(locale, "Make the day easier", "Gor dagen enklare", "Сделай день легче"),
            ]),
            "do": _pick_by_day(day, [
                _locale_pack(locale, "Take one practical thing off her plate without making her decide.", "Ta bort en praktisk sak fran hennes lista utan att hon behover bestamma.", "Сними с нее одну практическую задачу, не заставляя выбирать."),
                _locale_pack(locale, "Keep plans light and create a quieter evening than usual.", "Hall planerna latta och gor kvallen lugnare an vanligt.", "Сделай планы легче и вечер спокойнее обычного."),
                _locale_pack(locale, "Offer comfort first: food, warmth, rest, and less pressure.", "Borja med komfort: mat, varme, vila och mindre press.", "Сначала дай комфорт: еда, тепло, отдых и меньше давления."),
            ]),
            "say": _pick_by_day(day, [
                _locale_pack(locale, "I can make tonight easier for you.", "Jag kan gora ikvall enklare for dig.", "Я могу сделать этот вечер для тебя легче."),
                _locale_pack(locale, "No pressure today, we can keep things simple.", "Ingen press idag, vi kan halla det enkelt.", "Сегодня без давления, давай оставим всё простым."),
                _locale_pack(locale, "Tell me what would feel most comfortable right now.", "Sag vad som skulle kannas mest bekvamt just nu.", "Скажи, что сейчас было бы для тебя самым комфортным."),
            ]),
            "avoid": _pick_by_day(day, [
                _locale_pack(locale, "Avoid piling on decisions or last-minute plans.", "Undvik att lagga pa beslut eller sista minuten-planer.", "Не нагружай решениями и планами в последний момент."),
                _locale_pack(locale, "Don't turn low energy into a debate about attitude.", "Gor inte lag energi till en diskussion om attityd.", "Не превращай низкую энергию в спор о настроении."),
                _locale_pack(locale, "Avoid expecting the same pace as a high-energy day.", "Forvanta dig inte samma tempo som pa en hogenergisk dag.", "Не жди того же темпа, что в день с высокой энергией."),
            ]),
        }
    elif phase == "follicular":
        pack = {
            "headline": _pick_by_day(day, [
                _locale_pack(locale, "Use the lighter energy", "Anvand den lattare energin", "Используй более легкую энергию"),
                _locale_pack(locale, "Say yes to momentum", "Sag ja till momentum", "Подхвати этот импульс"),
                _locale_pack(locale, "Keep it fresh and easy", "Hall det latt och frascht", "Сделай всё легко и свежо"),
            ]),
            "do": _pick_by_day(day, [
                _locale_pack(locale, "Suggest something light and energizing: a walk, coffee, or a simple plan.", "Foresla nagot latt och energigivande: en promenad, kaffe eller en enkel plan.", "Предложи что-то легкое и бодрящее: прогулку, кофе или простой план."),
                _locale_pack(locale, "Match the growing momentum with a positive, low-friction invitation.", "Mota det okande momentumet med en positiv och enkel invitation.", "Поддержи растущий импульс позитивным и ненапряжным приглашением."),
                _locale_pack(locale, "Use this window for plans, progress, or something playful together.", "Anvand det har fonstret for planer, framsteg eller nagot lekfullt tillsammans.", "Используй это окно для планов, движения вперед или чего-то игривого вместе."),
            ]),
            "say": _pick_by_day(day, [
                _locale_pack(locale, "Want to do something light and fun later?", "Vill du gora nagot latt och kul senare?", "Хочешь потом сделать что-то легкое и приятное?"),
                _locale_pack(locale, "You seem to have more energy today, let's use it well.", "Det verkar som att du har mer energi idag, lat oss anvanda den bra.", "Похоже, сегодня у тебя больше энергии, давай хорошо ее используем."),
                _locale_pack(locale, "This feels like a good day for something simple and nice together.", "Det har kanns som en bra dag for nagot enkelt och fint tillsammans.", "Похоже, это хороший день для чего-то простого и приятного вместе."),
            ]),
            "avoid": _pick_by_day(day, [
                _locale_pack(locale, "Avoid overcomplicating an easy day with too much structure.", "Overkomplicera inte en enkel dag med for mycket struktur.", "Не усложняй легкий день излишней структурой."),
                _locale_pack(locale, "Don't stay passive if she seems open to movement or plans.", "Var inte passiv om hon verkar oppen for aktivitet eller planer.", "Не оставайся пассивным, если она открыта к активности или планам."),
                _locale_pack(locale, "Avoid treating today like a low-energy day if the vibe is clearly better.", "Behandla inte dagen som lagenergi om stammningen ar tydligt battre.", "Не относись к этому дню как к дню низкой энергии, если настроение явно лучше."),
            ]),
        }
    elif phase == "ovulatory":
        pack = {
            "headline": _pick_by_day(day, [
                _locale_pack(locale, "Lean into connection", "Luta dig in i kontakt", "Опирайся на близость"),
                _locale_pack(locale, "Show up with warmth", "Visa varme", "Будь теплее"),
                _locale_pack(locale, "Make space for closeness", "Skapa plats for narhet", "Создай пространство для близости"),
            ]),
            "do": _pick_by_day(day, [
                _locale_pack(locale, "Plan a date, a thoughtful compliment, or quality time with your full attention.", "Planera en dejt, en omtanksam komplimang eller kvalitetstid med full narvaro.", "Запланируй свидание, теплый комплимент или качественное время вместе с полным вниманием."),
                _locale_pack(locale, "Be present, confident, and generous with affection or appreciation.", "Var narvarande, trygg och generos med omtanke eller uppskattning.", "Будь включенным, уверенным и щедрым на тепло и признание."),
                _locale_pack(locale, "Use today for shared time, open conversation, or something a bit special.", "Anvand dagen for gemensam tid, oppen konversation eller nagot lite extra.", "Используй этот день для совместного времени, открытого разговора или чего-то особенного."),
            ]),
            "say": _pick_by_day(day, [
                _locale_pack(locale, "You look amazing today.", "Du ser fantastisk ut idag.", "Ты сегодня выглядишь потрясающе."),
                _locale_pack(locale, "Let's make time for us later.", "Lat oss hitta tid for oss senare.", "Давай позже выделим время для нас."),
                _locale_pack(locale, "I love being around you when things feel this easy.", "Jag alskar att vara nara dig nar allt kanns sa har latt.", "Мне очень нравится быть рядом с тобой, когда всё так легко ощущается."),
            ]),
            "avoid": _pick_by_day(day, [
                _locale_pack(locale, "Avoid being distracted or half-present if she's clearly open to connection.", "Var inte distraherad eller halvnarvarande om hon tydligt ar oppen for kontakt.", "Не будь отвлеченным или наполовину включенным, если она явно открыта к близости."),
                _locale_pack(locale, "Don't waste a good connection window on logistical stress.", "Slosa inte bort ett bra kontaktfonster pa logistisk stress.", "Не трать хорошее окно близости на стресс из-за бытовых дел."),
                _locale_pack(locale, "Avoid coldness when warmth would go a long way today.", "Undvik kyla nar varme skulle gora stor skillnad idag.", "Избегай холодности, когда сегодня тепло даст гораздо больше."),
            ]),
        }
    else:
        pack = {
            "headline": _pick_by_day(day, [
                _locale_pack(locale, "Keep it low-pressure", "Hall det utan press", "Снизь давление"),
                _locale_pack(locale, "Lead with steadiness", "Led med stabilitet", "Держись спокойно и стабильно"),
                _locale_pack(locale, "Make the day simpler", "Gor dagen enklare", "Сделай день проще"),
            ]),
            "do": _pick_by_day(day, [
                _locale_pack(locale, "Reduce friction: fewer demands, simpler plans, and more practical support.", "Minska friktionen: farre krav, enklare planer och mer praktiskt stod.", "Снизь трение: меньше требований, проще планы и больше практической поддержки."),
                _locale_pack(locale, "Take initiative on something small so she doesn't have to manage it.", "Ta initiativ i nagot litet sa att hon slipper hantera det.", "Возьми на себя что-то небольшое, чтобы ей не пришлось это тянуть."),
                _locale_pack(locale, "Aim for steadiness and predictability rather than intensity or problem-solving.", "Satsa pa stabilitet och forutsagbarhet i stallet for intensitet eller problemlosning.", "Делай ставку на стабильность и предсказуемость вместо интенсивности и попыток всё решить."),
            ]),
            "say": _pick_by_day(day, [
                _locale_pack(locale, "I can handle this part, you don't need to carry it.", "Jag kan ta den har delen, du behover inte bara den.", "Я возьму эту часть на себя, тебе не нужно тащить всё."),
                _locale_pack(locale, "Let's keep tonight simple.", "Lat oss halla kvallen enkel.", "Давай сделаем этот вечер простым."),
                _locale_pack(locale, "I'm on your side, we don't need to force anything today.", "Jag ar pa din sida, vi behover inte tvinga fram nagot idag.", "Я на твоей стороне, сегодня не нужно ничего форсировать."),
            ]),
            "avoid": _pick_by_day(day, [
                _locale_pack(locale, "Avoid pushing for decisions or heavy conversations late in the day.", "Undvik att pressa fram beslut eller tunga samtal sent pa dagen.", "Не дави с решениями и тяжелыми разговорами ближе к вечеру."),
                _locale_pack(locale, "Don't argue with the mood; lower the pressure instead.", "Braka inte med stamningen; sank pressen i stallet.", "Не спорь с ее состоянием; лучше снизь давление."),
                _locale_pack(locale, "Avoid reading sensitivity as rejection or disinterest.", "Tolkar inte kanslighet som avvisande eller ointresse.", "Не воспринимай чувствительность как отвержение или отсутствие интереса."),
            ]),
        }

    if stats["need_for_space"] >= 4:
        pack["headline"] = _locale_pack(locale, "Support gently, with room", "Stod mjukt, med utrymme", "Поддержи мягко, оставляя пространство")
        pack["do"] = _locale_pack(locale, "Offer one concrete form of help, then give her more breathing room instead of staying on top of it.", "Erbjud en konkret form av hjalp och ge sedan mer luft i stallet for att ligga pa.", "Предложи одну конкретную помощь, а потом дай больше пространства вместо постоянного контроля.")
        pack["say"] = _locale_pack(locale, "I'm here if you want me, and I can also give you some space.", "Jag ar har om du vill, och jag kan ocksa ge dig lite utrymme.", "Я рядом, если нужен, и могу дать тебе пространство.")
        pack["avoid"] = _locale_pack(locale, "Avoid turning care into too many check-ins, follow-up questions, or emotional pressure.", "Gor inte omtanke till for manga avstamningar, foljdfragor eller emotionell press.", "Не превращай заботу в слишком частые проверки, уточнения и эмоциональное давление.")
    elif stats["energy"] <= 2 and stats["physical_comfort"] <= 2:
        pack["headline"] = _locale_pack(locale, "Protect her energy", "Skydda hennes energi", "Сохрани ее энергию")
        pack["do"] = _locale_pack(locale, "Make the day physically easier: simplify plans, lower demands, and help her rest without needing to ask.", "Gor dagen kroppsligt enklare: forenkla planer, sank kraven och hjalp henne att vila utan att hon behover be om det.", "Сделай день физически легче: упрости планы, снизь требования и помоги ей отдохнуть без необходимости просить.")
        pack["say"] = _locale_pack(locale, "Let's make today lighter. I'll take care of the practical part.", "Lat oss gora dagen lattare. Jag tar hand om det praktiska.", "Давай сделаем этот день легче. Я возьму на себя практическую часть.")
        pack["avoid"] = _locale_pack(locale, "Avoid expecting output, enthusiasm, or long conversations from a depleted day.", "Forvanta dig inte produktivitet, entusiasm eller langa samtal pa en uttomd dag.", "Не жди продуктивности, энтузиазма или длинных разговоров в истощенный день.")
    elif stats["connection_openness"] >= 4 and stats["social"] >= 4:
        pack["headline"] = _locale_pack(locale, "Use the connection window", "Anvand kontaktfonstret", "Используй окно близости")
        pack["do"] = _locale_pack(locale, "Choose a warm, active moment together: a date, a walk, flirting, or a slightly more open conversation.", "Valj ett varmt och aktivt ogonblick tillsammans: en dejt, en promenad, flirt eller ett lite mer oppet samtal.", "Выбери теплый и живой момент вместе: свидание, прогулку, флирт или чуть более открытый разговор.")
        pack["say"] = _locale_pack(locale, "You feel especially easy to be close to today. Let's make time for us.", "Du kanns extra latt att vara nara idag. Lat oss ta tid for oss.", "Сегодня к тебе особенно легко быть ближе. Давай найдем время для нас.")
        pack["avoid"] = _locale_pack(locale, "Avoid wasting a naturally open day on pure logistics or being emotionally half-present.", "Slosa inte bort en naturligt oppen dag pa logistik eller halvnarvaro.", "Не трать естественно открытый день только на бытовые вопросы и эмоциональную полу-вовлеченность.")
    elif stats["irritability"] >= 4 and stats["focus"] <= 2:
        pack["headline"] = _locale_pack(locale, "Keep things simple and clear", "Hall allt enkelt och tydligt", "Держи всё простым и ясным")
        pack["do"] = _locale_pack(locale, "Use short, practical communication and reduce the number of decisions that need attention today.", "Anvand kort och praktisk kommunikation och minska antalet beslut som kravs idag.", "Используй короткую и практичную коммуникацию и сократи число решений, которые требуют внимания сегодня.")
        pack["say"] = _locale_pack(locale, "We don't need to solve everything today. Let's keep this simple.", "Vi behover inte losa allt idag. Lat oss halla det enkelt.", "Нам не нужно решать всё сегодня. Давай оставим всё простым.")
        pack["avoid"] = _locale_pack(locale, "Avoid layered conversations, debates, or asking her to process too much at once.", "Undvik lager pa lager-samtal, debatter eller att be henne hantera for mycket pa en gang.", "Избегай многослойных разговоров, споров и просьб переварить слишком многое сразу.")

    return pack


def _extra_support_lines(phase: str, stats: Dict[str, int], hormones: Dict[str, int], day: int, locale: str = "en") -> Dict[str, str]:
    body = []
    relationship = []
    regulate = []

    if stats["physical_comfort"] <= 2:
        body.append(_locale_pack(locale, "Prioritize physical comfort: warmth, rest, lighter plans, and easier food.", "Prioritera fysisk komfort: varme, vila, lattare planer och enklare mat.", "Сделай приоритетом физический комфорт: тепло, отдых, более легкие планы и простую еду."))
    if stats["cravings"] >= 4:
        body.append(_locale_pack(locale, "Keep snacks or an easy meal around so basic needs do not become extra friction.", "Ha snacks eller en enkel maltid redo sa att grundbehoven inte blir extra friktion.", "Держи под рукой перекус или простую еду, чтобы базовые потребности не превращались в дополнительное трение."))
    if stats["energy"] <= 2:
        body.append(_locale_pack(locale, "Aim for recovery over productivity today: fewer errands, less rushing, more margin.", "Satsa pa aterhamtning framfor produktivitet idag: farre arenden, mindre stress, mer luft.", "Сегодня делай ставку на восстановление, а не на продуктивность: меньше дел, меньше спешки, больше запаса."))
    if hormones["progesterone"] >= 65:
        regulate.append(_locale_pack(locale, "Higher progesterone often pairs better with steadiness, sleep, and reduced stimulation.", "Hogre progesteron passar ofta battre med stabilitet, somn och mindre stimulans.", "Более высокий прогестерон часто лучше сочетается со стабильностью, сном и меньшей стимуляцией."))
    if hormones["estrogen"] >= 70 and phase in ("follicular", "ovulatory"):
        relationship.append(_locale_pack(locale, "This is a better window for plans, social time, shared momentum, or a date.", "Detta ar ett battre fonster for planer, social tid, gemensamt momentum eller en dejt.", "Это более удачное окно для планов, общения, общего импульса или свидания."))
    if stats["need_for_space"] >= 4:
        relationship.append(_locale_pack(locale, "Support without hovering. Offer presence, then leave breathing room.", "Stod utan att hanga over henne. Visa narvaro och lamna sedan utrymme.", "Поддерживай без нависания. Покажи присутствие, а потом оставь пространство."))
    if stats["connection_openness"] >= 4:
        relationship.append(_locale_pack(locale, "Warmth and active attention are more likely to land well today.", "Varme och aktiv uppmarksamhet landar troligen bra idag.", "Тепло и активное внимание сегодня, скорее всего, зайдут хорошо."))
    if stats["irritability"] >= 4 or stats["sensitivity"] >= 4:
        regulate.append(_locale_pack(locale, "Keep your tone softer than usual and make fewer things urgent.", "Hall tonen mjukare an vanligt och gor farre saker bradskande.", "Держи тон мягче обычного и делай меньше вещей срочными."))
    if stats["focus"] <= 2:
        regulate.append(_locale_pack(locale, "Use short, concrete communication instead of layered conversations.", "Anvand kort och konkret kommunikation i stallet for lager pa lager-samtal.", "Используй короткую и конкретную коммуникацию вместо многослойных разговоров."))
    if phase == "ovulatory" and day % 2 == 0:
        relationship.append(_locale_pack(locale, "Good day for appreciation, flirtation, or a more expressive check-in.", "Bra dag for uppskattning, flirt eller en mer uttrycksfull check-in.", "Хороший день для признательности, флирта или более выразительного контакта."))
    if phase == "menstrual":
        body.append(_locale_pack(locale, "Simple comfort beats optimization today.", "Enkel komfort slar optimering idag.", "Сегодня простой комфорт важнее любой оптимизации."))
    if phase == "luteal" and day % 2 == 1:
        regulate.append(_locale_pack(locale, "Lower expectations on timing and avoid treating delays as a problem.", "Sank forvantningarna pa timing och gor inte forseningar till ett problem.", "Снизь ожидания к таймингу и не превращай задержки в проблему."))

    def choose(lines: List[str], fallback: str) -> str:
        return lines[0] if lines else fallback

    return {
        "body": choose(body, _locale_pack(locale, "Keep the body side easy today: food, rest, and less friction.", "Hall kroppssidan enkel idag: mat, vila och mindre friktion.", "Сделай телесную сторону дня проще: еда, отдых и меньше трения.")),
        "relationship": choose(relationship, _locale_pack(locale, "Stay present and supportive without making the day heavier.", "Var narvarande och stodjande utan att gora dagen tyngre.", "Оставайся рядом и поддерживай, не делая день тяжелее.")),
        "regulate": choose(regulate, _locale_pack(locale, "The best move today is reducing pressure and keeping things simple.", "Basta draget idag ar att minska pressen och halla det enkelt.", "Лучший ход сегодня — снизить давление и оставить всё простым.")),
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


def _settings_stat_line(label: str, emoji: str, level: int, locale: str, *, positive: bool = True) -> str:
    return f"{emoji} {label}: <b>{_level_word(level, positive=positive, locale=locale)}</b> {_bar(level)}"


def _feedback_ack(helpful: bool, phase: str, stats: Dict[str, int], locale: str = "en") -> str:
    if helpful:
        if stats["connection_openness"] >= 4:
            return _locale_pack(locale, "Saved. This helps us learn what works in higher-connection windows.", "Sparat. Det hjalper oss lara vad som fungerar i mer oppna kontaktfonster.", "Сохранено. Это помогает понять, что работает в более открытые окна близости.")
        if stats["need_for_space"] >= 4:
            return _locale_pack(locale, "Saved. This helps us learn how to support better on more sensitive, space-needing days.", "Sparat. Det hjalper oss lara hur vi stottar battre pa mer kansliga dagar med behov av mer utrymme.", "Сохранено. Это помогает понять, как лучше поддерживать в более чувствительные дни, когда нужно больше пространства.")
        return _locale_pack(locale, "Saved. This helps us learn which types of daily cues land best.", "Sparat. Det hjalper oss lara vilka typer av dagliga cues som landar bast.", "Сохранено. Это помогает понять, какие типы ежедневных советов заходят лучше всего.")
    if phase == "luteal" or stats["irritability"] >= 4:
        return _locale_pack(locale, "Saved. This tells us we should soften or simplify support on heavier days.", "Sparat. Det visar att vi bor mjuka upp eller forenkla stodet under tyngre dagar.", "Сохранено. Это показывает, что в более тяжелые дни поддержку нужно делать мягче и проще.")
    if stats["physical_comfort"] <= 2:
        return _locale_pack(locale, "Saved. This tells us we should adjust more toward comfort and recovery.", "Sparat. Det visar att vi bor justera mer mot komfort och aterhamtning.", "Сохранено. Это показывает, что стоит сильнее смещаться в сторону комфорта и восстановления.")
    return _locale_pack(locale, "Saved. This helps us refine the model when the advice misses the moment.", "Sparat. Det hjalper oss forfina modellen nar radet missar ogonblicket.", "Сохранено. Это помогает донастроить модель, когда совет не попадает в момент.")

# ----------------------------
# Rendering
# ----------------------------
async def render_today(profile: UserProfile) -> str:
    locale = profile.locale or "en"
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
    cue = _cue_pack(phase, day, now_stats, locale)
    support = _extra_support_lines(phase, now_stats, hormones, day, locale)

    def stat_line(label: str, emoji: str, key: str):
        return f"{emoji} {label}: {_bar(now_stats[key])} {_arrow(now_stats[key], prev_stats[key])}"

    # Next phase change within current cycle
    change_txt = ""
    if snap["next_change"] and snap["next_phase"] and snap["next_phase"] != phase:
        change_txt = (
            f"\n\n⏭ Next shift: <b>{snap['next_change'].isoformat()}</b> → "
            f"{_phase_name(snap['next_phase'], locale)} {PHASE_EMOJI[snap['next_phase']]}"
        )

    return (
        f"<b>{_ui(locale, 'today_for', name=profile.partner_name)}</b>\n"
        f"{_ui(locale, 'day_short')} <b>{day}/{profile.cycle_length}</b> · <b>{_phase_name(phase, locale)}</b> {PHASE_EMOJI[phase]}\n"
        f"{_phase_summary_text(phase, now_stats, locale)}\n\n"
        f"<b>{_ui(locale, 'todays_cue')}</b>\n"
        f"<b>{cue['headline']}</b>\n"
        f"{cue['do']}\n\n"
        f"<b>{_ui(locale, 'try_saying')}</b>\n"
        f"“{cue['say']}”\n\n"
        f"<b>{_ui(locale, 'avoid')}</b>\n"
        f"{cue['avoid']}\n\n"
        f"<b>{_ui(locale, 'more_help')}</b>\n"
        f"• {_ui(locale, 'body')}: {support['body']}\n"
        f"• {_ui(locale, 'relationship')}: {support['relationship']}\n"
        f"• {_ui(locale, 'regulation')}: {support['regulate']}\n\n"
        f"<b>{_ui(locale, 'signals_today')}</b>\n"
        f"⚡ {_ui(locale, 'energy')}: <b>{_level_word(now_stats['energy'], locale=locale)}</b> {_bar(now_stats['energy'])}\n"
        f"🎭 {_ui(locale, 'mood')}: <b>{_level_word(now_stats['mood'], locale=locale)}</b> {_bar(now_stats['mood'])}\n"
        f"💢 {_ui(locale, 'irritability')}: <b>{_level_word(now_stats['irritability'], positive=False, locale=locale)}</b> {_bar(now_stats['irritability'])}\n"
        f"🧠 {_ui(locale, 'focus')}: <b>{_level_word(now_stats['focus'], locale=locale)}</b> {_bar(now_stats['focus'])}\n"
        f"💞 {_ui(locale, 'connection')}: <b>{_level_word(now_stats['connection_openness'], locale=locale)}</b> {_bar(now_stats['connection_openness'])}\n\n"
        f"<b>{_ui(locale, 'more_detail')}</b>\n"
        f"{stat_line(_ui(locale, 'social_drive'), '🗣️', 'social')}\n"
        f"{stat_line(_ui(locale, 'cravings'), '🍫', 'cravings')}\n"
        f"{stat_line(_ui(locale, 'sensitivity'), '🫶', 'sensitivity')}\n"
        f"{stat_line(_ui(locale, 'need_for_space'), '🌫️', 'need_for_space')}\n"
        f"{stat_line(_ui(locale, 'physical_comfort'), '🛋️', 'physical_comfort')}\n"
        f"\n<b>{_ui(locale, 'estimated_hormone_picture')}</b>\n"
        f"{_ui(locale, 'hormone_estrogen')} <b>{hormones['estrogen']}</b>/100 · "
        f"{_ui(locale, 'hormone_progesterone')} <b>{hormones['progesterone']}</b>/100 · "
        f"LH <b>{hormones['lh']}</b>/100 · "
        f"FSH <b>{hormones['fsh']}</b>/100"
        f"{change_txt}"
        f"\n\n<b>{_ui(locale, 'feedback')}</b>\n"
        f"{_ui(locale, 'feedback_hint', helpful=_btn(locale, BTN_HELPFUL), not_helpful=_btn(locale, BTN_NOT_HELPFUL))}"
        f"\n\n{_ui(locale, 'today_stats_hint', stats=_btn(locale, BTN_STATS))}"
    )


async def render_settings(profile: UserProfile) -> str:
    locale = profile.locale or "en"
    snap = _cycle_snapshot(profile)
    phase = snap["phase"]
    day = snap["day"]
    stats = snap["stats"]
    desc = await copy_get(f"phase_desc_{phase}", locale=locale, phase=phase)
    history = await db_fetch_period_history(profile.chat_id, 3)

    next_shift = _ui(locale, "no_next_shift")
    if snap["next_change"] and snap["next_phase"]:
        next_shift = (
            f"<b>{snap['next_change'].isoformat()}</b> → "
            f"{_phase_name(snap['next_phase'], locale)} {PHASE_EMOJI[snap['next_phase']]}"
        )

    history_lines = []
    for item in history:
        history_lines.append(f"• {item['period_start']} → {item['period_end'] or _ui(locale, 'unknown')}")
    history_block = "\n".join(history_lines) if history_lines else _ui(locale, "no_history")

    return (
        f"<b>{_ui(locale, 'settings')}</b>\n\n"
        f"<b>{_ui(locale, 'partner')}</b>\n"
        f"{_ui(locale, 'name')}: <b>{profile.partner_name}</b>\n"
        f"{_ui(locale, 'language')}: <b>{profile.locale}</b>\n"
        f"{_ui(locale, 'paused')}: <b>{_ui(locale, 'yes') if profile.paused else _ui(locale, 'no')}</b>\n"
        f"{_ui(locale, 'notify')}: <b>{profile.notify_time}</b> ({profile.tz})\n\n"
        f"<b>{_ui(locale, 'cycle')}</b>\n"
        f"{_ui(locale, 'today_day')}: <b>{_ui(locale, 'day_short')} {day}/{profile.cycle_length}</b>\n"
        f"{_ui(locale, 'phase')}: <b>{_phase_name(phase, locale)}</b> {PHASE_EMOJI[phase]}\n"
        f"{_ui(locale, 'last_period')}: <b>{profile.period_start}</b> → <b>{profile.period_end or _ui(locale, 'unknown')}</b>\n"
        f"{_ui(locale, 'estimated_period_length')}: <b>{snap['period_len']} {_ui(locale, 'days_unit')}</b>\n"
        f"{_ui(locale, 'next_shift')}: {next_shift}\n\n"
        f"<b>{_ui(locale, 'phase_description')}</b>\n"
        f"{desc}\n\n"
        f"<b>{_ui(locale, 'quick_signal_check')}</b>\n"
        f"{_settings_stat_line(_ui(locale, 'energy'), '⚡', stats['energy'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'mood'), '🎭', stats['mood'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'irritability'), '💢', stats['irritability'], locale, positive=False)}\n\n"
        f"<b>{_ui(locale, 'recent_period_history')}</b>\n"
        f"{history_block}\n\n"
        f"{_ui(locale, 'settings_stats_hint', stats=_btn(locale, BTN_STATS))}\n\n"
        f"<b>{_ui(locale, 'commands')}</b>\n"
        f"• /update_period\n"
        f"• /set_time HH:MM\n"
        f"• /set_cycle 21-35\n"
        f"• {_btn(locale, BTN_LANGUAGE)} or /set_lang\n"
        f"• /pause or /resume\n"
        f"• /re_onboard"
    )


async def render_stats(profile: UserProfile) -> str:
    locale = profile.locale or "en"
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
        f"<b>{_ui(locale, 'stats_title')}</b>\n"
        f"{profile.partner_name} · {_ui(locale, 'day_short')} <b>{day}/{profile.cycle_length}</b> · "
        f"<b>{_phase_name(phase, locale)}</b> {PHASE_EMOJI[phase]}\n\n"
        f"<b>{_ui(locale, 'cycle_path')}</b>\n"
        f"{_cycle_path(day, bounds, profile.cycle_length)}\n"
        f"{_ui(locale, 'day_marker', day=day, cycle_len=profile.cycle_length)}\n\n"
        f"<b>{_ui(locale, 'current_dimensions')}</b>\n"
        f"{_settings_stat_line(_ui(locale, 'energy'), '⚡', stats['energy'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'mood'), '🎭', stats['mood'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'social_drive'), '🗣️', stats['social'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'focus'), '🧠', stats['focus'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'connection_openness'), '💞', stats['connection_openness'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'physical_comfort'), '🛋️', stats['physical_comfort'], locale)}\n"
        f"{_settings_stat_line(_ui(locale, 'sensitivity'), '🫶', stats['sensitivity'], locale, positive=False)}\n"
        f"{_settings_stat_line(_ui(locale, 'need_for_space'), '🌫️', stats['need_for_space'], locale, positive=False)}\n"
        f"{_settings_stat_line(_ui(locale, 'cravings'), '🍫', stats['cravings'], locale, positive=False)}\n"
        f"{_settings_stat_line(_ui(locale, 'irritability'), '💢', stats['irritability'], locale, positive=False)}\n\n"
        f"<b>{_ui(locale, 'trend_window')}</b>\n"
        f"{_ui(locale, 'trend_energy')}   {energy_curve}\n"
        f"{_ui(locale, 'trend_mood')}   {mood_curve}\n"
        f"{_ui(locale, 'trend_irrit')}   {irrit_curve}\n"
        f"{_ui(locale, 'trend_focus')}   {focus_curve}\n"
        f"{_ui(locale, 'trend_connect')}  {connect_curve}\n\n"
        f"<b>{_ui(locale, 'estimated_hormone_picture')}</b>\n"
        f"• {_ui(locale, 'hormone_estrogen')}: <b>{hormones['estrogen']}</b>/100\n"
        f"• {_ui(locale, 'hormone_progesterone')}: <b>{hormones['progesterone']}</b>/100\n"
        f"• LH: <b>{hormones['lh']}</b>/100\n"
        f"• FSH: <b>{hormones['fsh']}</b>/100\n\n"
        f"<b>{_ui(locale, 'how_to_read_this')}</b>\n"
        f"{_ui(locale, 'stats_explainer')}\n\n"
        f"{_ui(locale, 'stats_hint', today=_btn(locale, BTN_TODAY), about=_btn(locale, BTN_ABOUT))}"
    )


async def render_insights(profile: UserProfile) -> str:
    locale = profile.locale or "en"
    summary = await db_feedback_summary(profile.chat_id, 14)

    if summary["total"] == 0:
        return (
            f"<b>{_ui(locale, 'insights')}</b>\n\n"
            f"{_ui(locale, 'no_feedback_for', name=profile.partner_name)}\n\n"
            f"{_ui(locale, 'insights_train_hint', helpful=_btn(locale, BTN_HELPFUL), not_helpful=_btn(locale, BTN_NOT_HELPFUL))}"
        )

    phase_lines = []
    for row in summary["by_phase"]:
        total = int(row["total"] or 0)
        helpful_count = int(row["helpful_count"] or 0)
        rate = round((helpful_count / total) * 100) if total else 0
        phase = row["phase"]
        phase_lines.append(
            f"• {_phase_name(phase, locale)} {PHASE_EMOJI.get(phase, '')}: "
            f"{helpful_count}/{total} {_ui(locale, 'helpful_count').lower()} ({rate}%)"
        )

    latest_lines = []
    for row in summary["latest"]:
        latest_lines.append(
            f"• {row['cue_date'].isoformat()} · "
            f"{'👍' if row['helpful'] else '👎'} · "
            f"{_phase_name(row['phase'], locale)} · "
            f"{row['cue_headline']}"
        )

    avg_energy = summary["avg_energy"] if summary["avg_energy"] is not None else "—"
    avg_irrit = summary["avg_irritability"] if summary["avg_irritability"] is not None else "—"
    avg_connect = summary["avg_connection"] if summary["avg_connection"] is not None else "—"

    return (
        f"<b>{_ui(locale, 'insights')}</b>\n\n"
        f"<b>{_ui(locale, 'last_days', days=summary['days'])}</b>\n"
        f"{_ui(locale, 'responses')}: <b>{summary['total']}</b>\n"
        f"{_ui(locale, 'helpful_count')}: <b>{summary['helpful_count']}</b> ({summary['helpful_rate']}%)\n"
        f"{_ui(locale, 'avg_energy')}: <b>{avg_energy}</b>\n"
        f"{_ui(locale, 'avg_irritability')}: <b>{avg_irrit}</b>\n"
        f"{_ui(locale, 'avg_connection')}: <b>{avg_connect}</b>\n\n"
        f"<b>{_ui(locale, 'by_phase')}</b>\n"
        f"{chr(10).join(phase_lines)}\n\n"
        f"<b>{_ui(locale, 'recent_feedback')}</b>\n"
        f"{chr(10).join(latest_lines)}"
    )

async def render_about_phase(profile: UserProfile) -> str:
    snap = _cycle_snapshot(profile)
    phase = snap["phase"]
    locale = profile.locale or "en"
    desc = await copy_get(f"phase_desc_{phase}", locale=locale, phase=phase)
    return (
        f"<b>{_phase_name(phase, locale)} {PHASE_EMOJI[phase]}</b>\n\n"
        f"{desc}\n\n"
        f"<b>{_ui(locale, 'what_works_best')}</b>\n"
        f"{await copy_get(f'help_{phase}', locale=locale, phase=phase)}"
    )

async def render_forecast(profile: UserProfile, days: int = 7) -> str:
    locale = profile.locale or "en"
    snap = _cycle_snapshot(profile)
    today = snap["today"]
    bounds = snap["bounds"]
    start = snap["start"]

    lines = [f"<b>{_ui(locale, 'forecast_title', days=days, name=profile.partner_name)}</b>\n"]
    last_phase = None
    change_points: List[str] = []
    fertile_points: List[str] = []

    for i in range(days):
        d = today + dt.timedelta(days=i)
        cd = _cycle_day_for(d, start, profile.cycle_length)
        ph = _phase_for_cycle_day(cd, bounds)

        if last_phase is None:
            last_phase = ph
        elif ph != last_phase:
            change_points.append(f"• {d.isoformat()} - {_ui(locale, 'switches_to')} {_phase_name(ph, locale)} {PHASE_EMOJI[ph]}")
            last_phase = ph

        st = _phase_stats(cd, bounds)
        cue = _cue_pack(ph, cd, st, locale)
        hormones = _estimated_hormones(cd, profile.cycle_length, bounds)
        fertility = _fertility_label(cd, bounds)
        if fertility in {"peak", "high"}:
            fertile_points.append(f"• {d.isoformat()} - {_fertility_text(fertility, locale)} {_ui(locale, 'fertility').lower()}")
        lines.append(
            f"{d.isoformat()} · {_ui(locale, 'day_short')} {cd}/{profile.cycle_length} · {_phase_name(ph, locale)} {PHASE_EMOJI[ph]}\n"
            f"• {_ui(locale, 'cue')}: {cue['headline']}\n"
            f"• {_ui(locale, 'energy')} {_level_word(st['energy'], locale=locale)} · {_ui(locale, 'irritability')} {_level_word(st['irritability'], positive=False, locale=locale)} · "
            f"{_ui(locale, 'connection')} {_level_word(st['connection_openness'], locale=locale)}\n"
            f"• {_ui(locale, 'fertility')}: {_fertility_text(fertility, locale)}\n"
            f"• {_ui(locale, 'hormone_estrogen')}/{_ui(locale, 'hormone_progesterone')}: {hormones['estrogen']}/{hormones['progesterone']}"
        )

    lines.append(f"\n<b>{_ui(locale, 'important_shifts')}</b>")
    lines.append("\n".join(change_points) if change_points else _ui(locale, 'no_phase_switch'))
    lines.append(f"\n<b>{_ui(locale, 'fertility_window')}</b>")
    lines.append("\n".join(fertile_points) if fertile_points else _ui(locale, 'no_fertility_window'))
    return "\n".join(lines)

# ----------------------------
# Telegram send helper
# ----------------------------
async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    markup = reply_markup or _menu_kb(_lang(context))
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)

# ----------------------------
# Onboarding (menu always visible + menu presses don't break steps)
# ----------------------------
(O_LANG, O_NICK, O_DOB, O_START, O_END, O_CYCLE, O_TIME, U_START, U_END) = range(9)

async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["locale"] = "en"
    await _send(update, context, LANG_TEXT["en"]["welcome"], reply_markup=LANG_KB)
    return O_LANG


async def o_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected = LANG_MAP.get(_norm(update.message.text))
    if not selected:
        await _send(update, context, LANG_TEXT["en"]["lang_invalid"], reply_markup=LANG_KB)
        return O_LANG
    context.user_data["locale"] = selected
    await _send(update, context, _lt(context, "nick"))
    return O_NICK

async def o_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "nick"))
        return O_NICK
    nick = _norm(update.message.text)
    if len(nick) < 2:
        await _send(update, context, _lt(context, "nick_short"))
        return O_NICK
    context.user_data["partner_name"] = nick
    await _send(update, context, _lt(context, "dob"))
    return O_DOB

async def o_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "dob"))
        return O_DOB
    t = _norm(update.message.text).lower()
    if t in _skip_words(_lang(context)):
        context.user_data["partner_dob"] = None
    else:
        parsed = _parse_flexible_date_input(t, tz_name=_default_tz(), allow_without_year=False)
        if not parsed:
            await _send(update, context, _ui(_lang(context), "invalid_dob"))
            return O_DOB
        context.user_data["partner_dob"] = parsed
    await _send(
        update,
        context,
        _lt(context, "start"),
        reply_markup=_date_kb(_lang(context)),
    )
    return O_START

async def o_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "start"), reply_markup=_date_kb(_lang(context)))
        return O_START
    t = _norm(update.message.text)
    parsed = _parse_flexible_date_input(t, tz_name=_default_tz(), allow_without_year=True)
    if not parsed:
        await _send(
            update,
            context,
            _ui(_lang(context), "invalid_date_start"),
            reply_markup=_date_kb(_lang(context)),
        )
        return O_START
    context.user_data["period_start"] = parsed
    await _send(
        update,
        context,
        _lt(context, "end"),
        reply_markup=_date_kb(_lang(context)),
    )
    return O_END

async def o_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "end"), reply_markup=_date_kb(_lang(context)))
        return O_END
    t = _norm(update.message.text).lower()
    if t in _skip_words(_lang(context)):
        context.user_data["period_end"] = None
    else:
        parsed = _parse_flexible_date_input(t, tz_name=_default_tz(), allow_without_year=True)
        if not parsed:
            await _send(
                update,
                context,
                _ui(_lang(context), "invalid_end_onboarding"),
                reply_markup=_date_kb(_lang(context)),
            )
            return O_END
        end = dt.date.fromisoformat(parsed)
        start = dt.date.fromisoformat(context.user_data["period_start"])
        if end < start:
            await _send(update, context, _ui(_lang(context), "end_before_start_onboarding"), reply_markup=_date_kb(_lang(context)))
            return O_END
        context.user_data["period_end"] = parsed
    await _send(update, context, _lt(context, "cycle"))
    return O_CYCLE

async def o_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "cycle"))
        return O_CYCLE
    t = _norm(update.message.text)
    if not t.isdigit():
        await _send(update, context, _ui(_lang(context), "enter_cycle_number"))
        return O_CYCLE
    n = int(t)
    if n < 21 or n > 35:
        await _send(update, context, _lt(context, "cycle"))
        return O_CYCLE
    context.user_data["cycle_length"] = n
    await _send(
        update,
        context,
        _lt(context, "time"),
        reply_markup=_time_kb(_lang(context)),
    )
    return O_TIME

async def o_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_menu_press(update.message.text):
        await _send(update, context, _lt(context, "time"), reply_markup=_time_kb(_lang(context)))
        return O_TIME
    t = _parse_notify_input(update.message.text or "")
    if not t:
        await _send(
            update,
            context,
            _ui(_lang(context), "invalid_time"),
            reply_markup=_time_kb(_lang(context)),
        )
        return O_TIME

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
        locale=context.user_data.get("locale", "en"),
        paused=False,
    )

    await db_upsert_user(profile)
    await db_log_period(chat_id, profile.period_start, profile.period_end)
    locale = profile.locale
    context.user_data.clear()
    context.user_data["locale"] = locale

    # ✅ Critical: show TODAY immediately with menu
    await _send(update, context, _lt(context, "setup_done") + "\n\n" + await render_today(profile))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await _send(update, context, _lt(context, "cancelled"))
    return ConversationHandler.END

# ----------------------------
# Commands + menu
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_today(profile))

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_today(profile))

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_forecast(profile, 7))

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_stats(profile))

async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_insights(profile))

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_about_phase(profile))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    await _send(update, context, await render_settings(profile))

async def cmd_re_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_onboarding(update, context)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.paused = True
    await db_upsert_user(profile)
    _sync_locale(context, profile)
    await _send(update, context, _ui(profile.locale, "paused_pings") + "\n\n" + await render_today(profile))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.paused = False
    await db_upsert_user(profile)
    _sync_locale(context, profile)
    await _send(update, context, _ui(profile.locale, "resumed_pings") + "\n\n" + await render_today(profile))

async def cmd_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        return await _send(update, context, _ui(profile.locale, "usage_set_time"))
    parsed = _parse_notify_input(" ".join(parts[1:]))
    if not parsed:
        return await _send(update, context, _ui(profile.locale, "usage_set_time"))
    profile.notify_time = parsed
    await db_upsert_user(profile)
    await _send(update, context, _ui(profile.locale, "updated") + "\n\n" + await render_today(profile))

async def cmd_set_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await _send(update, context, _ui(profile.locale, "usage_set_cycle"))
    n = int(parts[1])
    if n < 21 or n > 35:
        return await _send(update, context, _ui(profile.locale, "cycle_length_invalid"))
    profile.cycle_length = n
    await db_upsert_user(profile)
    await _send(update, context, _ui(profile.locale, "updated") + "\n\n" + await render_today(profile))


async def cmd_set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    parts = (update.message.text or "").split()
    if len(parts) == 1:
        context.user_data["awaiting_lang_select"] = True
        return await _send(update, context, _ui(profile.locale, "choose_language"), reply_markup=LANG_KB)
    if len(parts) != 2 or parts[1].lower() not in {"en", "sv", "ru"}:
        return await _send(update, context, _ui(profile.locale, "usage_set_lang"), reply_markup=LANG_KB)
    profile.locale = parts[1].lower()
    await db_upsert_user(profile)
    context.user_data["awaiting_lang_select"] = False
    await _send(update, context, _ui(profile.locale, "language_updated", locale=profile.locale) + "\n\n" + await render_settings(profile))


async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    context.user_data["awaiting_lang_select"] = True
    await _send(update, context, _ui(profile.locale, "choose_language"), reply_markup=LANG_KB)


async def apply_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, locale: str):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.locale = locale
    await db_upsert_user(profile)
    context.user_data["locale"] = locale
    context.user_data["awaiting_lang_select"] = False
    await _send(
        update,
        context,
        _ui(locale, "language_updated", locale=locale) + "\n\n" + await render_settings(profile),
        reply_markup=_menu_kb(locale),
    )


async def _save_period_update(update: Update, context: ContextTypes.DEFAULT_TYPE, start_s: str, end_s: Optional[str]):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    profile.period_start = start_s
    profile.period_end = end_s
    await db_upsert_user(profile)
    await db_log_period(profile.chat_id, start_s, end_s)
    context.user_data.pop("period_update_start", None)
    await _send(
        update,
        context,
        _ui(profile.locale, "period_saved") + "\n\n"
        + await render_today(profile),
    )
    return ConversationHandler.END


async def cmd_update_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    parts = (update.message.text or "").split()
    if len(parts) in (2, 3):
        start_s = _parse_flexible_date_input(parts[1], tz_name=profile.tz, allow_without_year=True)
        end_s = _parse_flexible_date_input(parts[2], tz_name=profile.tz, allow_without_year=True) if len(parts) == 3 else None
        if not start_s or (len(parts) == 3 and not end_s):
            return await _send(update, context, _ui(profile.locale, "usage_dates_short"))
        s = dt.date.fromisoformat(start_s)
        if end_s:
            e = dt.date.fromisoformat(end_s)
            if e < s:
                return await _send(update, context, _ui(profile.locale, "end_before_start_short"))
        return await _save_period_update(update, context, start_s, end_s)

    await _send(
        update,
        context,
        _ui(profile.locale, "update_period_intro"),
        reply_markup=_date_kb(profile.locale),
    )
    return U_START


async def u_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    t = _norm(update.message.text)
    _sync_locale(context, profile)
    if _is_menu_press(t):
        await _send(update, context, _ui(profile.locale, "update_period_step1"), reply_markup=_date_kb(profile.locale))
        return U_START
    parsed = _parse_flexible_date_input(t, tz_name=profile.tz, allow_without_year=True)
    if not parsed:
        await _send(update, context, _ui(profile.locale, "invalid_date_start"), reply_markup=_date_kb(profile.locale))
        return U_START
    context.user_data["period_update_start"] = parsed
    await _send(
        update,
        context,
        _ui(profile.locale, "update_period_step2"),
        reply_markup=_date_kb(profile.locale),
    )
    return U_END


async def u_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    start_s = context.user_data.get("period_update_start")
    if not start_s:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)
    t = _norm(update.message.text).lower()
    if _is_menu_press(update.message.text):
        await _send(update, context, _ui(profile.locale, "update_period_step2_short"), reply_markup=_date_kb(profile.locale))
        return U_END
    if t in _skip_words(profile.locale):
        return await _save_period_update(update, context, start_s, None)
    end_s = _parse_flexible_date_input(t, tz_name=profile.tz, allow_without_year=True)
    if not end_s:
        await _send(update, context, _ui(profile.locale, "invalid_date_end"), reply_markup=_date_kb(profile.locale))
        return U_END
    if dt.date.fromisoformat(end_s) < dt.date.fromisoformat(start_s):
        await _send(update, context, _ui(profile.locale, "period_end_before_start"), reply_markup=_date_kb(profile.locale))
        return U_END
    return await _save_period_update(update, context, start_s, end_s)


async def _record_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, helpful: bool):
    profile = await db_fetch_user(update.effective_chat.id)
    if not profile:
        return await start_onboarding(update, context)
    _sync_locale(context, profile)

    snap = _cycle_snapshot(profile)
    cue = _cue_pack(snap["phase"], snap["day"], snap["stats"], profile.locale)
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
            f"{'👍' if helpful else '👎'} {_feedback_ack(helpful, snap['phase'], snap['stats'], profile.locale)}\n\n"
            f"{_ui(profile.locale, 'cue_saved', headline=cue['headline'])}\n"
            f"{_ui(profile.locale, 'day_short')} <b>{snap['day']}/{profile.cycle_length}</b> · "
            f"<b>{_phase_name(snap['phase'], profile.locale)}</b> {PHASE_EMOJI[snap['phase']]}"
        ),
    )


async def cmd_helpful(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _record_feedback(update, context, True)


async def cmd_not_helpful(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _record_feedback(update, context, False)

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = _norm(update.message.text)
    if t in LANG_MAP:
        return await apply_language_choice(update, context, LANG_MAP[t])
    key = _button_key(t)
    if key == BTN_TODAY:
        return await cmd_today(update, context)
    if key == BTN_FORECAST:
        return await cmd_forecast(update, context)
    if key == BTN_STATS:
        return await cmd_stats(update, context)
    if key == BTN_INSIGHTS:
        return await cmd_insights(update, context)
    if key == BTN_SETTINGS:
        return await cmd_settings(update, context)
    if key == BTN_LANGUAGE:
        return await choose_language(update, context)
    if key == BTN_ABOUT:
        return await cmd_about(update, context)
    if key == BTN_HELPFUL:
        return await cmd_helpful(update, context)
    if key == BTN_NOT_HELPFUL:
        return await cmd_not_helpful(update, context)
    await _send(update, context, _ui(_lang(context), "use_menu"))

# ----------------------------
# Notifications loop (no job-queue)
# ----------------------------
async def _send_daily_ping(app: Application, profile: UserProfile):
    try:
        await app.bot.send_message(
            chat_id=profile.chat_id,
            text=await render_today(profile),
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_kb(profile.locale),
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
            O_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, o_lang)],
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

    period_conv = ConversationHandler(
        entry_points=[CommandHandler("update_period", cmd_update_period)],
        states={
            U_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, u_start)],
            U_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, u_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(period_conv)

    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("re_onboard", cmd_re_onboard))
    app.add_handler(CommandHandler("set_time", cmd_set_time))
    app.add_handler(CommandHandler("set_cycle", cmd_set_cycle))
    app.add_handler(CommandHandler("set_lang", cmd_set_lang))
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
