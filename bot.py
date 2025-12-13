import os
import sys
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta, time, timezone
from typing import Dict, Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -------------------------
# CONFIG + LOGGING
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("daycue")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

STORAGE_PATH = os.getenv("DAYCUE_STORAGE_PATH", "profiles.json")

# Menu buttons (ReplyKeyboard - always visible)
BTN_TODAY = "📍 Today"
BTN_FORECAST = "🔮 Forecast"
BTN_SETTINGS = "⚙️ Settings"
BTN_SEND_NOW = "🔔 Send now"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_TODAY), KeyboardButton(BTN_FORECAST)],
        [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_SEND_NOW)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Onboarding conversation states
(
    S_NICK,
    S_DOB,
    S_START,
    S_END,
    S_LENGTH,
    S_NOTIFY,
) = range(6)


# -------------------------
# STORAGE (JSON FILE)
# -------------------------
def load_profiles() -> Dict[str, dict]:
    if not os.path.exists(STORAGE_PATH):
        return {}
    try:
        with open(STORAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("Failed to load profiles.json")
        return {}


def save_profiles(store: Dict[str, dict]) -> None:
    try:
        with open(STORAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Failed to save profiles.json")


PROFILES: Dict[str, dict] = load_profiles()


@dataclass
class Profile:
    chat_id: int
    partner_nick: str = ""
    partner_dob: Optional[str] = None  # YYYY-MM-DD or None
    last_period_start: str = ""        # YYYY-MM-DD
    last_period_end: str = ""          # YYYY-MM-DD
    cycle_length: int = 28
    notify_time_local: str = "09:00"   # HH:MM (local)
    tz_offset_min: int = 0             # minutes from UTC, default 0 (we can set later)
    paused: bool = False
    created_at: str = ""
    updated_at: str = ""

    def touch(self) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        if not self.created_at:
            self.created_at = now
        self.updated_at = now

    @staticmethod
    def from_store(chat_id: int) -> "Profile":
        raw = PROFILES.get(str(chat_id))
        if not raw:
            p = Profile(chat_id=chat_id)
            p.touch()
            return p
        return Profile(**raw)

    def save(self) -> None:
        self.touch()
        PROFILES[str(self.chat_id)] = asdict(self)
        save_profiles(PROFILES)


# -------------------------
# DATE HELPERS
# -------------------------
def parse_ymd(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    try:
        s = s.strip()
        hh, mm = s.split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
        return None
    except Exception:
        return None


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


# -------------------------
# CYCLE MODEL (MVP)
# -------------------------
def phase_for_day(day_in_cycle_1based: int, cycle_length: int) -> str:
    """
    Simple phase split (MVP):
    - Menstrual: day 1-5
    - Follicular: day 6 -> ovulation_day-1
    - Ovulatory: ovulation_day -> ovulation_day+2
    - Luteal: rest
    ovulation_day ~ cycle_length-14
    """
    ov = max(12, cycle_length - 14)  # guard
    if day_in_cycle_1based <= 5:
        return "🩸 Menstrual"
    if day_in_cycle_1based < ov:
        return "🌱 Follicular"
    if ov <= day_in_cycle_1based <= (ov + 2):
        return "🔥 Ovulatory"
    return "🌙 Luteal"


def day_in_cycle(today: date, last_period_start: date, cycle_length: int) -> int:
    # day 1 starts at last_period_start
    delta = (today - last_period_start).days
    if delta < 0:
        return 1
    return (delta % cycle_length) + 1


def hormone_levels(phase: str) -> Dict[str, int]:
    """
    MVP hormone levels 0..100 (not medical-grade; UX heuristics for coaching suggestions)
    """
    if "Menstrual" in phase:
        return {"Estrogen": 25, "Progesterone": 20, "LH": 15, "FSH": 35, "Testosterone": 35}
    if "Follicular" in phase:
        return {"Estrogen": 70, "Progesterone": 25, "LH": 30, "FSH": 40, "Testosterone": 55}
    if "Ovulatory" in phase:
        return {"Estrogen": 85, "Progesterone": 35, "LH": 95, "FSH": 55, "Testosterone": 65}
    # Luteal
    return {"Estrogen": 55, "Progesterone": 80, "LH": 20, "FSH": 20, "Testosterone": 40}


def stats_for_phase(phase: str) -> Dict[str, int]:
    """
    Game stats 0..100
    """
    if "Menstrual" in phase:
        return {
            "🎭 Mood Stability": 45,
            "🗣️ Social Drive": 35,
            "❤️ Emotional Needs": 75,
            "🔥 Anxiety Level": 55,
            "💢 Irritability": 55,
            "🍩 Cravings": 75,
            "💕 Sexual Drive": 35,
            "🧠 Cognitive Focus": 45,
        }
    if "Follicular" in phase:
        return {
            "🎭 Mood Stability": 70,
            "🗣️ Social Drive": 75,
            "❤️ Emotional Needs": 55,
            "🔥 Anxiety Level": 30,
            "💢 Irritability": 25,
            "🍩 Cravings": 35,
            "💕 Sexual Drive": 60,
            "🧠 Cognitive Focus": 80,
        }
    if "Ovulatory" in phase:
        return {
            "🎭 Mood Stability": 80,
            "🗣️ Social Drive": 90,
            "❤️ Emotional Needs": 65,
            "🔥 Anxiety Level": 25,
            "💢 Irritability": 20,
            "🍩 Cravings": 40,
            "💕 Sexual Drive": 90,
            "🧠 Cognitive Focus": 75,
        }
    # Luteal
    return {
        "🎭 Mood Stability": 50,
        "🗣️ Social Drive": 45,
        "❤️ Emotional Needs": 80,
        "🔥 Anxiety Level": 60,
        "💢 Irritability": 75,
        "🍩 Cravings": 80,
        "💕 Sexual Drive": 55,
        "🧠 Cognitive Focus": 50,
    }


def recommendations(phase: str) -> Dict[str, str]:
    if "Menstrual" in phase:
        return {
            "🤝 Together": "Low-pressure care: tea, warm food, quiet company, help with chores.",
            "🍲 Food": "Warm, iron-rich meals + carbs. Keep it comforting.",
            "💬 Talk": "Short questions, soft tone. Offer support and space.",
            "🧘 Recovery": "Walks, rest, early night. Reduce demanding plans.",
        }
    if "Follicular" in phase:
        return {
            "🤝 Together": "Plan something new: café, gym, museum, mini-trip, playful date.",
            "🍲 Food": "Light + protein. Hydration. Keep energy clean.",
            "💬 Talk": "Brainstorm future plans. Encourage and hype her wins.",
            "🎯 Action": "Great time for important conversations and decisions.",
        }
    if "Ovulatory" in phase:
        return {
            "🤝 Together": "Connection + fun: social, romantic, compliments, intimacy.",
            "🍲 Food": "Balanced. Nothing too heavy. Keep it fresh.",
            "💬 Talk": "Deep talks land well. Be present and emotionally tuned in.",
            "✨ Romance": "High “spark” phase. Quality time is king.",
        }
    return {
        "🤝 Together": "Reassurance + stability. Avoid escalations. Be consistent.",
        "🍲 Food": "Comfort foods, magnesium-rich snacks. Lower friction choices.",
        "💬 Talk": "Validate feelings first, then solve. Keep your tone steady.",
        "🧯 Conflict": "If tension rises: pause, breathe, revisit later.",
    }


def build_today_message(p: Profile) -> str:
    today = date.today()
    start = parse_ymd(p.last_period_start) or today
    d = day_in_cycle(today, start, p.cycle_length)
    ph = phase_for_day(d, p.cycle_length)

    h = hormone_levels(ph)
    s = stats_for_phase(ph)
    r = recommendations(ph)

    lines = []
    lines.append(f"🧭 *Today’s Brief*")
    lines.append(f"👤 Partner: *{p.partner_nick or '—'}*")
    lines.append(f"📅 Date: *{today.isoformat()}*")
    lines.append(f"🧬 Cycle day: *{d}/{p.cycle_length}*")
    lines.append(f"🌗 Phase: *{ph}*")
    lines.append("")
    lines.append("🧪 *Hormones (MVP model)*")
    lines.append(f"• Estrogen: {h['Estrogen']}/100")
    lines.append(f"• Progesterone: {h['Progesterone']}/100")
    lines.append(f"• LH: {h['LH']}/100")
    lines.append(f"• FSH: {h['FSH']}/100")
    lines.append(f"• Testosterone: {h['Testosterone']}/100")
    lines.append("")
    lines.append("🎮 *Stats*")
    for k, v in s.items():
        lines.append(f"• {k}: {v}/100")
    lines.append("")
    lines.append("🧠 *What helps today*")
    for k, v in r.items():
        lines.append(f"• {k}: {v}")

    return "\n".join(lines)


def build_forecast_message(p: Profile, days: int = 7) -> str:
    today = date.today()
    start = parse_ymd(p.last_period_start) or today

    lines = [f"🔮 *Forecast ({days} days)*", f"👤 Partner: *{p.partner_nick or '—'}*", ""]
    for i in range(days):
        dday = today + timedelta(days=i)
        d = day_in_cycle(dday, start, p.cycle_length)
        ph = phase_for_day(d, p.cycle_length)
        lines.append(f"• {dday.isoformat()} — day {d}: {ph}")
    return "\n".join(lines)


# -------------------------
# SCHEDULING (JobQueue)
# -------------------------
def ensure_daily_job(app: Application, p: Profile) -> Tuple[time, datetime]:
    """
    Schedules a daily notification at user's local HH:MM by converting to UTC time.
    Returns (utc_time, next_run_utc_datetime).
    """
    # Remove existing job for this chat_id
    job_name = f"daily:{p.chat_id}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    hhmm = parse_hhmm(p.notify_time_local)
    if not hhmm:
        raise ValueError("Invalid notify_time_local")
    local_h, local_m = hhmm

    # Convert local time to UTC clock time using tz_offset_min
    # local = utc + offset  => utc = local - offset
    utc_minutes = (local_h * 60 + local_m) - int(p.tz_offset_min)
    utc_minutes = utc_minutes % (24 * 60)
    utc_h = utc_minutes // 60
    utc_m = utc_minutes % 60

    t_utc = time(utc_h, utc_m, tzinfo=timezone.utc)

    # Schedule daily
    app.job_queue.run_daily(
        callback=job_send_daily,
        time=t_utc,
        name=job_name,
        data={"chat_id": p.chat_id},
    )

    # Compute next run in UTC (approx)
    now_utc = datetime.now(timezone.utc)
    next_run = datetime.combine(now_utc.date(), t_utc, tzinfo=timezone.utc)
    if next_run <= now_utc:
        next_run += timedelta(days=1)

    return t_utc, next_run


async def job_send_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return

    p = Profile.from_store(chat_id)
    if p.paused:
        return

    msg = build_today_message(p)
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# -------------------------
# HANDLERS
# -------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = Profile.from_store(chat_id)

    # Always show menu
    if p.partner_nick and p.last_period_start:
        await update.message.reply_text(
            "✅ You’re set.\nChoose an action:",
            reply_markup=MAIN_MENU,
        )
        await update.message.reply_text(
            build_today_message(p),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "Welcome. Quick onboarding.\n\n1/6 - Enter partner nickname (example: Anna)",
            reply_markup=MAIN_MENU,
        )
        context.user_data["onboarding"] = Profile(chat_id=chat_id)
        return


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    If user presses menu buttons, route them.
    Also show menu if user types random text after onboarding.
    """
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    p = Profile.from_store(chat_id)

    if txt == BTN_TODAY:
        if not p.partner_nick:
            await update.message.reply_text("⚠️ Setup first: /start", reply_markup=MAIN_MENU)
            return
        await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)
        return

    if txt == BTN_FORECAST:
        if not p.partner_nick:
            await update.message.reply_text("⚠️ Setup first: /start", reply_markup=MAIN_MENU)
            return
        await update.message.reply_text(build_forecast_message(p, days=7), parse_mode="Markdown", reply_markup=MAIN_MENU)
        return

    if txt == BTN_SEND_NOW:
        if not p.partner_nick:
            await update.message.reply_text("⚠️ Setup first: /start", reply_markup=MAIN_MENU)
            return
        await update.message.reply_text("🔔 Sending now…", reply_markup=MAIN_MENU)
        await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)
        return

    if txt == BTN_SETTINGS:
        await update.message.reply_text(
            "⚙️ Settings\n\nType one command:\n"
            "• /restart — run onboarding again\n"
            "• /pause — stop daily notifications\n"
            "• /resume — resume daily notifications\n"
            "• /status — show your saved data\n",
            reply_markup=MAIN_MENU,
        )
        return

    # Default: keep menu visible
    await update.message.reply_text("Menu ready ✅", reply_markup=MAIN_MENU)


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    context.user_data["onboarding"] = Profile(chat_id=chat_id)
    await update.message.reply_text(
        "Restarting onboarding.\n\n1/6 - Enter partner nickname (example: Anna)",
        reply_markup=MAIN_MENU,
    )
    return S_NICK


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = Profile.from_store(chat_id)
    p.paused = True
    p.save()
    await update.message.reply_text("⏸ Paused daily notifications.", reply_markup=MAIN_MENU)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = Profile.from_store(chat_id)
    p.paused = False
    p.save()

    try:
        ensure_daily_job(context.application, p)
        await update.message.reply_text("▶️ Resumed and scheduled daily notifications.", reply_markup=MAIN_MENU)
    except Exception as e:
        log.exception("resume schedule failed: %s", e)
        await update.message.reply_text("⚠️ Resumed, but scheduling failed. Use 🔔 Send now for MVP.", reply_markup=MAIN_MENU)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = Profile.from_store(chat_id)
    await update.message.reply_text(
        "🧾 Status\n"
        f"Partner: {p.partner_nick or '—'}\n"
        f"DOB: {p.partner_dob or '—'}\n"
        f"Period start: {p.last_period_start or '—'}\n"
        f"Period end: {p.last_period_end or '—'}\n"
        f"Cycle length: {p.cycle_length}\n"
        f"Notify time (local): {p.notify_time_local}\n"
        f"TZ offset (min): {p.tz_offset_min}\n"
        f"Paused: {p.paused}\n"
        f"Updated: {p.updated_at}\n",
        reply_markup=MAIN_MENU,
    )


# -------------------------
# ONBOARDING STEPS
# -------------------------
async def step_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data.get("onboarding") or Profile(chat_id=update.effective_chat.id)
    p.partner_nick = update.message.text.strip()
    context.user_data["onboarding"] = p

    await update.message.reply_text(
        "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'",
        reply_markup=MAIN_MENU,
    )
    return S_DOB


async def step_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data["onboarding"]
    txt = update.message.text.strip().lower()
    if txt == "skip":
        p.partner_dob = None
    else:
        d = parse_ymd(txt)
        if not d:
            await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD or 'skip'.", reply_markup=MAIN_MENU)
            return S_DOB
        p.partner_dob = d.isoformat()

    await update.message.reply_text("3/6 - Last period START date (YYYY-MM-DD)", reply_markup=MAIN_MENU)
    return S_START


async def step_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data["onboarding"]
    d = parse_ymd(update.message.text.strip())
    if not d:
        await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD.", reply_markup=MAIN_MENU)
        return S_START
    p.last_period_start = d.isoformat()

    await update.message.reply_text("4/6 - Last period END date (YYYY-MM-DD)", reply_markup=MAIN_MENU)
    return S_END


async def step_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data["onboarding"]
    d = parse_ymd(update.message.text.strip())
    if not d:
        await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD.", reply_markup=MAIN_MENU)
        return S_END
    p.last_period_end = d.isoformat()

    await update.message.reply_text("5/6 - Cycle length in days (21-35). Example: 28", reply_markup=MAIN_MENU)
    return S_LENGTH


async def step_cycle_length(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data["onboarding"]
    try:
        n = int(update.message.text.strip())
        if n < 21 or n > 35:
            raise ValueError
        p.cycle_length = n
    except Exception:
        await update.message.reply_text("❌ Enter a number 21-35. Example: 28", reply_markup=MAIN_MENU)
        return S_LENGTH

    await update.message.reply_text("6/6 - Daily notification time (HH:MM). Example: 09:00", reply_markup=MAIN_MENU)
    return S_NOTIFY


async def step_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p: Profile = context.user_data["onboarding"]
    txt = update.message.text.strip()

    hhmm = parse_hhmm(txt)
    if not hhmm:
        await update.message.reply_text("❌ Invalid time. Use HH:MM (e.g., 09:00)", reply_markup=MAIN_MENU)
        return S_NOTIFY

    # Optional: detect user timezone offset from Telegram (if present)
    # Telegram doesn’t reliably provide timezone; keep 0 unless you set it later.
    p.notify_time_local = txt
    p.paused = False

    # Save profile
    p.save()

    # Reply FIRST (so you never “hang”)
    await update.message.reply_text(
        f"✅ Saved.\n🕒 Daily notify: {p.notify_time_local}\n\nChoose an action:",
        reply_markup=MAIN_MENU,
    )
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)

    # Then schedule daily job (if JobQueue is available)
    try:
        t_utc, next_run = ensure_daily_job(context.application, p)
        next_local = next_run + timedelta(minutes=p.tz_offset_min)
        await update.message.reply_text(
            f"⏭ Next notify (server UTC): {next_run.strftime('%Y-%m-%d %H:%M')}\n"
            f"🧭 Next notify (local approx): {next_local.strftime('%Y-%m-%d %H:%M')}",
            reply_markup=MAIN_MENU,
        )
    except Exception as e:
        log.exception("Scheduling failed: %s", e)
        await update.message.reply_text(
            "⚠️ Scheduling failed on server.\n"
            "Bot still works. Use 🔔 Send now for MVP testing.\n"
            "If you want, we’ll lock scheduling + timezone next.",
            reply_markup=MAIN_MENU,
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Use /start to begin again.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# -------------------------
# MAIN
# -------------------------
def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(error_handler)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("restart", restart_cmd),
        ],
        states={
            S_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nick)],
            S_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_dob)],
            S_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_start)],
            S_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_end)],
            S_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cycle_length)],
            S_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_notify)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)

    # commands
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # menu buttons + default text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    return app


def schedule_existing_users(app: Application) -> None:
    """
    After restart/redeploy, schedule for all saved users.
    """
    for k, raw in PROFILES.items():
        try:
            p = Profile(**raw)
            if p.partner_nick and p.last_period_start and not p.paused:
                ensure_daily_job(app, p)
        except Exception:
            log.exception("Failed to schedule existing user %s", k)


def main() -> None:
    log.info("BOOT: starting bot.py")
    app = build_app()
    schedule_existing_users(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
