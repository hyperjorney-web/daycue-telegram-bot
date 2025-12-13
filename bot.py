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

# Settings flow states
SET_MENU, SET_NICK, SET_DOB, SET_START, SET_END, SET_CYCLE, SET_NOTIFY = range(6, 13)

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

def is_onboarded(p: UserProfile) -> bool:
    return bool(p.partner_nick and p.period_start and p.cycle_length and p.notify_time)

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
    # MVP mapping: smooth enough for 21-35, best at 28
    if day <= 5:
        return "Menstrual"
    if day <= max(6, int(cycle_len * 0.45)):
        return "Follicular"
    if day <= max(7, int(cycle_len * 0.57)):
        return "Ovulatory"
    return "Luteal"

def day_payload(profile: UserProfile, day_number: int | None = None) -> dict:
    d = day_number if day_number is not None else cycle_day(profile)
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
            "Keep plans light - offer warmth + help",
            "Ask: comfort or space?",
            "Food: soup, tea, chocolate, cozy dinner",
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
            "Do something active together (walk/date/new place)",
            "Support goals and ideas - be playful",
            "Good timing for planning and decisions",
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
            "Compliments land extra well now",
            "Great timing for deeper talks and intimacy",
            "Plan a social activity (dinner/event)",
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
            "Lower friction - fewer debates, calmer tone",
            "Don’t take sharpness personally - offer reassurance",
            "Food: comfort meals + early night",
        ]
        prognosis = "Potential PMS sensitivity"

    return {"day": d, "phase": phase, "stats": stats, "recs": recs, "prognosis": prognosis}

def mini_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="TODAY"),
         InlineKeyboardButton("Prognosis", callback_data="PROGNOSIS")],
        [InlineKeyboardButton("Settings", callback_data="SETTINGS"),
         InlineKeyboardButton("Send now", callback_data="SEND_NOW")],
        [InlineKeyboardButton("Pause", callback_data="PAUSE"),
         InlineKeyboardButton("Resume", callback_data="RESUME")],
        [InlineKeyboardButton("Reset", callback_data="RESET")],
    ])

def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Partner nickname", callback_data="SET_NICK"),
         InlineKeyboardButton("Partner DOB", callback_data="SET_DOB")],
        [InlineKeyboardButton("Period start", callback_data="SET_START"),
         InlineKeyboardButton("Period end", callback_data="SET_END")],
        [InlineKeyboardButton("Cycle length", callback_data="SET_CYCLE"),
         InlineKeyboardButton("Notify time", callback_data="SET_NOTIFY")],
        [InlineKeyboardButton("Back", callback_data="BACK")],
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

def format_prognosis(profile: UserProfile, days: int = 7) -> str:
    base = cycle_day(profile)
    lines = []
    for i in range(days):
        d = clamp_day(base + i, profile.cycle_length)
        payload = day_payload(profile, d)
        short_tip = payload["recs"][0] if payload["recs"] else "-"
        lines.append(f"Day {d} - {payload['phase']} - {payload['prognosis']} - {short_tip}")
    return "Next 7 days\n\n" + "\n".join(lines)

async def ensure_daily_job(app: Application, profile: UserProfile) -> None:
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
)

async def schedule_test_ping(app: Application, chat_id: int) -> None:
    app.job_queue.run_once(
        callback=send_daily,
        when=10,
        data={"chat_id": chat_id},
        name=f"test_{chat_id}",
    )

async def send_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    profile = users.get(chat_id)
    if not profile or profile.paused or not is_onboarded(profile):
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="Daily check-in\n\n" + format_today(profile),
        reply_markup=mini_app_keyboard(),
    )

# -----------------------
# Onboarding flow
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)

    if is_onboarded(profile):
        await update.message.reply_text(
            "Welcome back.\n\n" + format_today(profile),
            reply_markup=mini_app_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Welcome. Quick onboarding.\n\n"
        "1/6 - Enter partner nickname (example: Alona)"
    )
    return ASK_NICK

async def on_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    nick = (update.message.text or "").strip()
    if len(nick) < 2:
        await update.message.reply_text("Nickname too short. Try again (example: Alona).")
        return ASK_NICK
    profile.partner_nick = nick
    await update.message.reply_text("2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return ASK_DOB

async def on_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
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
    profile = get_or_create(update.effective_chat.id)
    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_START
    profile.period_start = d.strftime("%Y-%m-%d")
    await update.message.reply_text("4/6 - Last period END date (YYYY-MM-DD)")
    return ASK_END

async def on_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return ASK_END

    start_d = parse_yyyy_mm_dd(profile.period_start or "")
    if start_d and d < start_d:
        await update.message.reply_text("End date can’t be before start date. Try again.")
        return ASK_END

    profile.period_end = d.strftime("%Y-%m-%d")
    await update.message.reply_text("5/6 - Cycle length in days (21-35). Example: 28")
    return ASK_CYCLE

async def on_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
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

    await ensure_daily_job(context.application, profile)
    await schedule_test_ping(context.application, chat_id)

    await update.message.reply_text(
        "✅ Setup complete. Test notification will arrive in ~10 seconds.\n\n"
        + format_today(profile),
        reply_markup=mini_app_keyboard(),
    )
    return ConversationHandler.END

# -----------------------
# Settings flow
# -----------------------
async def settings_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    chat_id = q.message.chat_id
    profile = users.get(chat_id)
    if not profile or not is_onboarded(profile):
        await q.message.reply_text("Not set up yet. Type /start to begin.")
        return ConversationHandler.END

    await q.message.reply_text(
        "Settings - choose what to update",
        reply_markup=settings_keyboard(),
    )
    return SET_MENU

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "BACK":
        await q.message.reply_text("Back to main.", reply_markup=mini_app_keyboard())
        return ConversationHandler.END

    if data == "SET_NICK":
        await q.message.reply_text("Enter new partner nickname:")
        return SET_NICK

    if data == "SET_DOB":
        await q.message.reply_text("Enter new partner DOB (YYYY-MM-DD) or type 'skip':")
        return SET_DOB

    if data == "SET_START":
        await q.message.reply_text("Enter new last period START date (YYYY-MM-DD):")
        return SET_START

    if data == "SET_END":
        await q.message.reply_text("Enter new last period END date (YYYY-MM-DD):")
        return SET_END

    if data == "SET_CYCLE":
        await q.message.reply_text("Enter new cycle length (21-35):")
        return SET_CYCLE

    if data == "SET_NOTIFY":
        await q.message.reply_text("Enter new daily notification time (HH:MM):")
        return SET_NOTIFY

    await q.message.reply_text("Pick one option.", reply_markup=settings_keyboard())
    return SET_MENU

async def set_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    nick = (update.message.text or "").strip()
    if len(nick) < 2:
        await update.message.reply_text("Nickname too short. Try again.")
        return SET_NICK
    profile.partner_nick = nick
    await update.message.reply_text("Saved. Back to main.", reply_markup=mini_app_keyboard())
    return ConversationHandler.END

async def set_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    txt = (update.message.text or "").strip().lower()
    if txt == "skip":
        profile.partner_dob = None
    else:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD or type 'skip'.")
            return SET_DOB
        profile.partner_dob = d.strftime("%Y-%m-%d")
    await update.message.reply_text("Saved. Back to main.", reply_markup=mini_app_keyboard())
    return ConversationHandler.END

async def set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return SET_START
    profile.period_start = d.strftime("%Y-%m-%d")
    await update.message.reply_text("Saved. Back to main.", reply_markup=mini_app_keyboard())
    return ConversationHandler.END

async def set_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    d = parse_yyyy_mm_dd(update.message.text or "")
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return SET_END
    start_d = parse_yyyy_mm_dd(profile.period_start or "")
    if start_d and d < start_d:
        await update.message.reply_text("End date can’t be before start date. Try again.")
        return SET_END
    profile.period_end = d.strftime("%Y-%m-%d")
    await update.message.reply_text("Saved. Back to main.", reply_markup=mini_app_keyboard())
    return ConversationHandler.END

async def set_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_or_create(update.effective_chat.id)
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Please enter a number (example: 28).")
        return SET_CYCLE
    n = int(txt)
    if n < 21 or n > 35:
        await update.message.reply_text("For MVP, use 21-35.")
        return SET_CYCLE
    profile.cycle_length = n
    await update.message.reply_text("Saved. Back to main.", reply_markup=mini_app_keyboard())
    return ConversationHandler.END

async def set_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profile = get_or_create(chat_id)
    t = parse_hh_mm(update.message.text or "")
    if not t:
        await update.message.reply_text("Invalid time. Use HH:MM (example: 09:00).")
        return SET_NOTIFY
    profile.notify_time = f"{t.hour:02d}:{t.minute:02d}"
    await ensure_daily_job(context.application, profile)
    await update.message.reply_text(
        "Saved. Daily schedule updated. Test ping in ~10 seconds.",
        reply_markup=mini_app_keyboard(),
    )
    await schedule_test_ping(context.application, chat_id)
    return ConversationHandler.END

# -----------------------
# Main menu callbacks
# -----------------------
async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    if not profile or not is_onboarded(profile):
        await q.message.reply_text("Not set up yet. Type /start to begin.")
        return

    if q.data == "TODAY":
        await q.message.reply_text(format_today(profile), reply_markup=mini_app_keyboard())
        return

    if q.data == "PROGNOSIS":
        await q.message.reply_text(format_prognosis(profile, days=7), reply_markup=mini_app_keyboard())
        return

    if q.data == "SEND_NOW":
        await q.message.reply_text("Manual check-in\n\n" + format_today(profile), reply_markup=mini_app_keyboard())
        return

    if q.data == "PAUSE":
        profile.paused = True
        await q.message.reply_text("Paused. No daily messages.", reply_markup=mini_app_keyboard())
        return

    if q.data == "RESUME":
        profile.paused = False
        await q.message.reply_text("Resumed. Daily messages ON.", reply_markup=mini_app_keyboard())
        return

    if q.data == "SETTINGS":
        # Handled by settings conversation entry
        return

# -----------------------
# Build app
# -----------------------
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    onboarding = ConversationHandler(
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

    settings_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(settings_entry, pattern="^SETTINGS$")],
        states={
            SET_MENU: [CallbackQueryHandler(settings_menu)],
            SET_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_nick)],
            SET_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_dob)],
            SET_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_start)],
            SET_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_end)],
            SET_CYCLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cycle)],
            SET_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_notify)],
        },
        fallbacks=[CallbackQueryHandler(settings_entry, pattern="^SETTINGS$")],
        allow_reentry=True,
    )

    app.add_handler(onboarding)
    app.add_handler(settings_conv)

    # Main menu callbacks (Today/Prognosis/Send now/Pause/Resume/Reset)
    app.add_handler(CallbackQueryHandler(on_main_menu))

    return app

def main() -> None:
    print("BOOT: starting bot.py", flush=True)
    app = build_app()
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
