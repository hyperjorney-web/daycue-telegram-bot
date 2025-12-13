import os
import sys
import re
from dataclasses import dataclass
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -----------------------
# Config
# -----------------------
TZ = ZoneInfo("Europe/Stockholm")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

# -----------------------
# MVP In-memory storage
# -----------------------
@dataclass
class UserProfile:
    chat_id: int
    partner_nick: str | None = None
    partner_dob: str | None = None        # YYYY-MM-DD or None
    period_start: str | None = None       # YYYY-MM-DD
    period_end: str | None = None         # YYYY-MM-DD
    cycle_length: int = 28
    notify_time: str = "09:00"            # HH:MM
    paused: bool = False

users: dict[int, UserProfile] = {}
jobs: dict[int, str] = {}  # chat_id -> job name

# -----------------------
# Conversation states
# -----------------------
ASK_NICK, ASK_DOB, ASK_START, ASK_END, ASK_CYCLE, ASK_NOTIFY = range(6)

# -----------------------
# Helpers
# -----------------------
def now_local() -> datetime:
    return datetime.now(TZ)

def parse_yyyy_mm_dd(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def parse_hh_mm(s: str) -> time | None:
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))

def get_or_create(chat_id: int) -> UserProfile:
    if chat_id not in users:
        users[chat_id] = UserProfile(chat_id=chat_id)
    return users[chat_id]

def clamp_day(day: int, cycle_len: int) -> int:
    if cycle_len <= 0:
        return 1
    return ((day - 1) % cycle_len) + 1

def cycle_day(profile: UserProfile, today: date | None = None) -> int:
    if today is None:
        today = now_local().date()
    if not profile.period_start:
        return 1
    start = parse_yyyy_mm_dd(profile.period_start)
    if not start:
        return 1
    diff = (today - start).days
    return clamp_day(diff + 1, profile.cycle_length)

def phase_for_day(day: int, cycle_len: int) -> str:
    # MVP mapping - readable and stable
    # (works best at 28, but good enough for 21-35 MVP)
    if day <= 5:
        return "Menstrual"
    if day <= max(6, int(cycle_len * 0.45)):  # roughly follicular until ~45%
        return "Follicular"
    if day <= max(7, int(cycle_len * 0.57)):  # short ovulation window
        return "Ovulatory"
    return "Luteal"

def day_payload(profile: UserProfile) -> dict:
    d = cycle_day(profile)
    phase = phase_for_day(d, profile.cycle_length)

    if phase == "Menstrual":
        stats = {
            "Mood stability": "Low",
            "Social drive": "Low",
            "Emotional needs": "Comfort + patience",
            "Anxiety": "Medium",
            "Irritability": "Medium",
            "Cravings": "High (warm + sweet)",
            "Sexual drive": "Low",
            "Cognitive focus": "Low/Medium",
        }
        recs = [
            "Keep plans light. Offer help + warmth.",
            "Ask: comfort or space?",
            "Food: soup, tea, chocolate, cozy dinner.",
        ]
        prognosis = "Low energy window"
    elif phase == "Follicular":
        stats = {
            "Mood stability": "Medium/High",
            "Social drive": "High",
            "Emotional needs": "Encouragement + fun",
            "Anxiety": "Low",
            "Irritability": "Low",
            "Cravings": "Low",
            "Sexual drive": "Rising",
            "Cognitive focus": "High",
        }
        recs = [
            "Do something active together (walk/date/new place).",
            "Support goals and ideas. Be playful.",
            "Good days for planning and decisions.",
        ]
        prognosis = "High capacity window"
    elif phase == "Ovulatory":
        stats = {
            "Mood stability": "High",
            "Social drive": "Very High",
            "Emotional needs": "Connection + compliments",
            "Anxiety": "Low",
            "Irritability": "Low",
            "Cravings": "Low/Medium",
            "Sexual drive": "High",
            "Cognitive focus": "Very High",
        }
        recs = [
            "Compliments land extra well now.",
            "Great timing for deeper talks and intimacy.",
            "Plan a social activity (dinner/event).",
        ]
        prognosis = "Peak window"
    else:
        stats = {
            "Mood stability": "Low/Medium",
            "Social drive": "Low/Medium",
            "Emotional needs": "Reassurance + stability",
            "Anxiety": "Medium/High",
            "Irritability": "High",
            "Cravings": "High (comfort food)",
            "Sexual drive": "Medium",
            "Cognitive focus": "Low/Medium",
        }
        recs = [
            "Lower friction: fewer debates, calmer tone.",
            "Don’t take sharpness personally. Offer reassurance.",
            "Food: comfort meals, early night.",
        ]
        prognosis = "Potential PMS sensitivity"

    return {"day": d, "phase": phase, "stats": stats, "recs": recs, "prognosis": prognosis}

def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="TODAY"),
         InlineKeyboardButton("Status", callback_data="STATUS")],
        [InlineKeyboardButton("Pause", callback_data="PAUSE"),
         InlineKeyboardButton("Resume", callback_data="RESUME")],
        [InlineKeyboardButton("Reset", callback_data="RESET")],
    ])

def format_today(profile: UserProfile) -> str:
    p = day_payload(profile)
    stats_lines = "\n".join([f"- {k}: {v}" for k, v in p["stats"].items()])
    rec_lines = "\n".join([f"- {x}" for x in p["recs"]])
    return (
        f"Partner: {profile.partner_nick}\n"
        f"Day {p['day']}/{profile.cycle_length} - {p['phase']}\n"
        f"Prognosis: {p['prognosis']}\n\n"
        f"Stats\n{stats_lines}\n\n"
        f"Recommended actions\n{rec_lines}"
    )

async def ensure_daily_job(app: Application, profile: UserProfile) -> None:
    # remove existing job
    if profile.chat_id in jobs:
        for j in app.job_queue.get_jobs_by_name(jobs[profile.chat_id]):
            j.schedule_removal()
        jobs.pop(profile.chat_id, None)

    t = parse_hh_mm(profile.notify_time) or time(9, 0)
    name = f"daily_{profile.chat_id}"
    jobs[profile.chat_id] = name

    app.job_queue.run_daily(
        send_daily,
        time=t,
        days=(0, 1, 2, 3, 4, 5, 6),
        name=name,
        data={"chat_id": profile.chat_id},
        timezone=TZ,
    )

async def send_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    profile = users.get(chat_id)
    if not profile or profile.paused or not profile.partner_nick or not profile.period_start:
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="Daily check-in\n\n" + format_today(profile),
        reply_markup=menu_keyboard(),
    )

# -----------------------
# Onboarding flow
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    if profile.partner_nick and profile.period_start:
        await update.message.reply_text(
            "Welcome back.\n\n" + format_today(profile),
            reply_markup=menu_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Welcome. Quick onboarding.\n\n"
        "1/6 - Enter partner nickname (example: Anna)"
    )
    return ASK_NICK

async def on_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    nick = (update.message.text or "").strip()
    if len(nick) < 2:
        await update.message.reply_text("Nickname too short. Try again (example: Anna).")
        return ASK_NICK

    profile.partner_nick = nick
    await update.message.reply_text("2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return ASK_DOB

async def on_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    txt = (update.message.text or "").strip().lower()
    if txt == "skip":
        profile.partner_dob = None
    else:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD or type 'skip'.")
            return ASK_DOB
        profile.partner_dob = d.strftime("%Y-%m-%d")

    await update.message.reply_text("3/6 - Last period START date (YYYY-MM-DD)")
    return ASK_START

async def on_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_START

    profile.period_start = d.strftime("%Y-%m-%d")
    await update.message.reply_text("4/6 - Last period END date (YYYY-MM-DD)")
    return ASK_END

async def on_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_END

    start = parse_yyyy_mm_dd(profile.period_start or "")
    if start and d < start:
        await update.message.reply_text("End date can’t be before start date. Try again.")
        return ASK_END

    profile.period_end = d.strftime("%Y-%m-%d")
    await update.message.reply_text("5/6 - Cycle length in days (21-35). Example: 28")
    return ASK_CYCLE

async def on_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Please enter a number (example: 28).")
        return ASK_CYCLE

    n = int(txt)
    if n < 21 or n > 35:
        await update.message.reply_text("For MVP, use 21-35.")
        return ASK_CYCLE

    profile.cycle_length = n
    await update.message.reply_text("6/6 - Daily notification time (HH:MM). Example: 09:00")
    return ASK_NOTIFY

async def on_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    t = parse_hh_mm(update.message.text or "")
    if not t:
        await update.message.reply_text("Invalid time. Use HH:MM (example: 09:00).")
        return ASK_NOTIFY

    profile.notify_time = f"{t.hour:02d}:{t.minute:02d}"

    # schedule daily message
    await ensure_daily_job(context.application, profile)

    await update.message.reply_text(
        "Setup complete.\n\n" + format_today(profile),
        reply_markup=menu_keyboard(),
    )
    return ConversationHandler.END

# -----------------------
# Menu callbacks
# -----------------------
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    chat_id = q.message.chat_id
    profile = users.get(chat_id)

    if q.data == "RESET":
        users.pop(chat_id, None)
        if chat_id in jobs:
            for j in context.application.job_queue.get_jobs_by_name(jobs[chat_id]):
                j.schedule_removal()
            jobs.pop(chat_id, None)
        await q.message.reply_text("Reset done. Type /start to set up again.")
        return

    if not profile or not profile.partner_nick or not profile.period_start:
        await q.message.reply_text("Not set up yet. Type /start to begin.")
        return

    if q.data == "TODAY":
        await q.message.reply_text(format_today(profile), reply_markup=menu_keyboard())
        return

    if q.data == "STATUS":
        d = cycle_day(profile)
        await q.message.reply_text(
            f"Status\n- Partner: {profile.partner_nick}\n- Day: {d}/{profile.cycle_length}\n"
            f"- Phase: {phase_for_day(d, profile.cycle_length)}\n"
            f"- Notify: {profile.notify_time}\n- Paused: {profile.paused}",
            reply_markup=menu_keyboard(),
        )
        return

    if q.data == "PAUSE":
        profile.paused = True
        await q.message.reply_text("Paused. No daily messages.", reply_markup=menu_keyboard())
        return

    if q.data == "RESUME":
        profile.paused = False
        await q.message.reply_text("Resumed. Daily messages ON.", reply_markup=menu_keyboard())
        return

# -----------------------
# Build app
# -----------------------
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_nick)],
            ASK_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_dob)],
            ASK_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_start_date)],
            ASK_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_end_date)],
            ASK_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_cycle)],
            ASK_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_notify)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_menu))

    return app

def main() -> None:
    print("BOOT: starting bot.py", flush=True)
    app = build_app()
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
