import os
import sys
import re
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
    nickname: str | None = None
    dob: str | None = None  # YYYY-MM-DD (optional)
    period_start: str | None = None  # YYYY-MM-DD
    period_end: str | None = None    # YYYY-MM-DD
    cycle_length: int = 28
    notify_time: str = "09:00"  # HH:MM
    paused: bool = False

users: dict[int, UserProfile] = {}
jobs: dict[int, str] = {}  # chat_id -> job name

# -----------------------
# Conversation states
# -----------------------
ASK_NICK, ASK_DOB, ASK_PERIOD_START, ASK_PERIOD_END, ASK_CYCLE_LENGTH, ASK_NOTIFY_TIME, CONFIRM = range(7)

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

def clamp_day(day: int, cycle_len: int) -> int:
    # Convert to 1..cycle_len
    if day < 1:
        return 1
    if day > cycle_len:
        return ((day - 1) % cycle_len) + 1
    return day

def cycle_day(profile: UserProfile, today: date | None = None) -> int:
    if not profile.period_start:
        return 1
    start = parse_yyyy_mm_dd(profile.period_start)
    if not start:
        return 1
    if today is None:
        today = now_local().date()
    diff = (today - start).days
    return clamp_day(diff + 1, profile.cycle_length)

def phase_for_day(day: int) -> str:
    # Default mapping for ~28 days. For MVP: keep stable + understandable.
    if 1 <= day <= 5:
        return "Menstrual"
    if 6 <= day <= 13:
        return "Follicular"
    if 14 <= day <= 16:
        return "Ovulatory"
    return "Luteal"

def tips_for(day: int) -> dict:
    phase = phase_for_day(day)

    # MVP stat levels: Low / Medium / High (simple)
    if phase == "Menstrual":
        stats = dict(
            mood_stability="Low",
            social_drive="Low",
            emotional_needs="Comfort + patience",
            anxiety="Medium",
            irritability="Medium",
            cravings="High (warm + sweet)",
            sexual_drive="Low",
            cognitive_focus="Low/Medium",
        )
        recs = [
            "Warm, simple care: tea, blanket, cozy meal",
            "Keep plans light - offer help, don’t push",
            "Ask: “Do you want comfort or space?”",
        ]
    elif phase == "Follicular":
        stats = dict(
            mood_stability="Medium/High",
            social_drive="High",
            emotional_needs="Encouragement + fun",
            anxiety="Low",
            irritability="Low",
            cravings="Low",
            sexual_drive="Rising",
            cognitive_focus="High",
        )
        recs = [
            "Plan something active together - walk, date, new place",
            "Be playful - support goals and new ideas",
            "Great days for planning and talking about future stuff",
        ]
    elif phase == "Ovulatory":
        stats = dict(
            mood_stability="High",
            social_drive="Very High",
            emotional_needs="Connection + compliments",
            anxiety="Low",
            irritability="Low",
            cravings="Low/Medium",
            sexual_drive="High",
            cognitive_focus="Very High",
        )
        recs = [
            "Compliments + attention land extra well now",
            "Good timing for deeper conversations and intimacy",
            "Do something social: friends, dinner, event",
        ]
    else:  # Luteal
        stats = dict(
            mood_stability="Low/Medium",
            social_drive="Low/Medium",
            emotional_needs="Reassurance + stability",
            anxiety="Medium/High",
            irritability="High",
            cravings="High (comfort food)",
            sexual_drive="Medium",
            cognitive_focus="Low/Medium",
        )
        recs = [
            "Lower friction: fewer debates, more calm tone",
            "Offer reassurance - don’t take sharpness personally",
            "Comfort food + early night = real impact",
        ]

    # A simple “forecast tag”
    if phase == "Luteal":
        prognosis = "Potential PMS sensitivity"
    elif phase == "Menstrual":
        prognosis = "Low energy window"
    else:
        prognosis = "High capacity window"

    return {
        "phase": phase,
        "stats": stats,
        "recs": recs,
        "prognosis": prognosis,
    }

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="TODAY"),
         InlineKeyboardButton("Status", callback_data="STATUS")],
        [InlineKeyboardButton("Pause", callback_data="PAUSE"),
         InlineKeyboardButton("Resume", callback_data="RESUME")],
        [InlineKeyboardButton("Reset", callback_data="RESET")],
    ])

def format_today(profile: UserProfile) -> str:
    day = cycle_day(profile)
    t = tips_for(day)
    s = t["stats"]
    recs = "\n- " + "\n- ".join(t["recs"])

    return (
        f"Day {day}/{profile.cycle_length} - {t['phase']}\n"
        f"Prognosis: {t['prognosis']}\n\n"
        f"Stats\n"
        f"- Mood stability: {s['mood_stability']}\n"
        f"- Social drive: {s['social_drive']}\n"
        f"- Emotional needs: {s['emotional_needs']}\n"
        f"- Anxiety: {s['anxiety']}\n"
        f"- Irritability: {s['irritability']}\n"
        f"- Cravings: {s['cravings']}\n"
        f"- Sexual drive: {s['sexual_drive']}\n"
        f"- Cognitive focus: {s['cognitive_focus']}\n\n"
        f"Recommended actions{recs}\n"
    )

def get_or_create_profile(chat_id: int) -> UserProfile:
    if chat_id not in users:
        users[chat_id] = UserProfile(chat_id=chat_id)
    return users[chat_id]

async def ensure_daily_job(app: Application, profile: UserProfile) -> None:
    # Remove existing job
    if profile.chat_id in jobs:
        for j in app.job_queue.get_jobs_by_name(jobs[profile.chat_id]):
            j.schedule_removal()
        jobs.pop(profile.chat_id, None)

    # Create new daily job
    hhmm = parse_hh_mm(profile.notify_time) or time(9, 0)
    job_name = f"daily_{profile.chat_id}"
    jobs[profile.chat_id] = job_name

    app.job_queue.run_daily(
        callback=send_daily_tip_job,
        time=hhmm,
        days=(0, 1, 2, 3, 4, 5, 6),
        name=job_name,
        data={"chat_id": profile.chat_id},
        timezone=TZ,
    )

async def send_daily_tip_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    profile = users.get(chat_id)
    if not profile:
        return
    if profile.paused:
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="Daily check-in\n\n" + format_today(profile),
        reply_markup=main_menu(),
    )

# -----------------------
# Commands & Callbacks
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    # If already onboarded, show menu
    if profile.nickname and profile.period_start:
        await update.message.reply_text(
            f"Welcome back, {profile.nickname}.\n\n" + format_today(profile),
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Welcome. Let’s set up your partner’s cycle in 2 minutes.\n\n"
        "Step 1/6 - Enter partner nickname (example: Anna)"
    )
    return ASK_NICK

async def ask_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    nick = (update.message.text or "").strip()
    if len(nick) < 2:
        await update.message.reply_text("Nickname too short. Try again (example: Anna).")
        return ASK_NICK

    profile.nickname = nick
    await update.message.reply_text(
        "Step 2/6 - Enter partner date of birth (YYYY-MM-DD) or type 'skip'"
    )
    return ASK_DOB

async def ask_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    txt = (update.message.text or "").strip().lower()
    if txt == "skip":
        profile.dob = None
    else:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD or type 'skip'.")
            return ASK_DOB
        profile.dob = d.strftime("%Y-%m-%d")

    await update.message.reply_text(
        "Step 3/6 - Enter last period START date (YYYY-MM-DD)"
    )
    return ASK_PERIOD_START

async def ask_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_PERIOD_START

    profile.period_start = d.strftime("%Y-%m-%d")
    await update.message.reply_text(
        "Step 4/6 - Enter last period END date (YYYY-MM-DD)"
    )
    return ASK_PERIOD_END

async def ask_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_PERIOD_END

    # Optional validation: end >= start
    start = parse_yyyy_mm_dd(profile.period_start or "")
    if start and d < start:
        await update.message.reply_text("End date cannot be before start date. Try again.")
        return ASK_PERIOD_END

    profile.period_end = d.strftime("%Y-%m-%d")
    await update.message.reply_text(
        "Step 5/6 - Enter cycle length in days (default 28). Example: 28"
    )
    return ASK_CYCLE_LENGTH

async def ask_cycle_length(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Please enter a number, like 28.")
        return ASK_CYCLE_LENGTH

    n = int(txt)
    if n < 21 or n > 35:
        await update.message.reply_text("For MVP, use a value between 21 and 35.")
        return ASK_CYCLE_LENGTH

    profile.cycle_length = n
    await update.message.reply_text(
        "Step 6/6 - Choose daily notification time (HH:MM). Example: 09:00"
    )
    return ASK_NOTIFY_TIME

async def ask_notify_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create_profile(chat_id)

    t = parse_hh_mm(update.message.text or "")
    if not t:
        await update.message.reply_text("Invalid time. Use HH:MM (example: 09:00).")
        return ASK_NOTIFY_TIME

    profile.notify_time = f"{t.hour:02d}:{t.minute:02d}"

    preview = format_today(profile)
    await update.message.reply_text(
        "Setup complete.\n\n"
        f"Profile saved for: {profile.nickname}\n"
        f"Daily time: {profile.notify_time}\n\n"
        "Here is today’s view:\n\n"
        f"{preview}\n"
        "You will receive a daily check-in.\n",
        reply_markup=main_menu(),
    )

    # Schedule daily job
    await ensure_daily_job(context.application, profile)
    return ConversationHandler.END

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    profile = users.get(chat_id)
    if not profile or not profile.nickname or not profile.period_start:
        await update.message.reply_text("Not set up yet. Type /start to begin.")
        return
    await update.message.reply_text(format_today(profile), reply_markup=main_menu())

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    profile = users.get(chat_id)
    if not profile:
        await update.message.reply_text("Not set up yet. Type /start to begin.")
        return
    d = cycle_day(profile)
    await update.message.reply_text(
        "Current status\n"
        f"- Partner: {profile.nickname}\n"
        f"- Cycle day: {d}/{profile.cycle_length}\n"
        f"- Phase: {phase_for_day(d)}\n"
        f"- Notify time: {profile.notify_time}\n"
        f"- Paused: {profile.paused}\n",
        reply_markup=main_menu(),
    )

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    profile = users.get(chat_id)

    if query.data == "RESET":
        users.pop(chat_id, None)
        # remove job
        if chat_id in jobs:
            for j in context.application.job_queue.get_jobs_by_name(jobs[chat_id]):
                j.schedule_removal()
            jobs.pop(chat_id, None)
        await query.message.reply_text("Reset done. Type /start to set up again.")
        return

    if not profile or not profile.nickname or not profile.period_start:
        await query.message.reply_text("Not set up yet. Type /start to begin.")
        return

    if query.data == "TODAY":
        await query.message.reply_text(format_today(profile), reply_markup=main_menu())
        return

    if query.data == "STATUS":
        d = cycle_day(profile)
        await query.message.reply_text(
            f"Status\n- Day {d}/{profile.cycle_length}\n- Phase: {phase_for_day(d)}\n- Notify: {profile.notify_time}\n- Paused: {profile.paused}",
            reply_markup=main_menu(),
        )
        return

    if query.data == "PAUSE":
        profile.paused = True
        await query.message.reply_text("Paused. You will not receive daily messages.", reply_markup=main_menu())
        return

    if query.data == "RESUME":
        profile.paused = False
        await query.message.reply_text("Resumed. Daily messages are on.", reply_markup=main_menu())
        return

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_nick)],
            ASK_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_dob)],
            ASK_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_period_start)],
            ASK_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_period_end)],
            ASK_CYCLE_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_cycle_length)],
            ASK_NOTIFY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notify_time)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )

    app.add_handler(onboarding)
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_menu))

    return app

def main():
    app = build_app()
    # Long polling (MVP)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
