import os
import sys
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from typing import Dict, Optional, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# Config / Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("daycue-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

# Optional (for better stability if Fly volume exists)
DATA_PATH = os.getenv("DATA_PATH", "/data")
PROFILES_FILE = os.path.join(DATA_PATH, "profiles.json")

DEFAULT_TZ_OFFSET = "+01:00"  # Sweden default; user can change later


# ----------------------------
# UI (Persistent menu)
# ----------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🎮 Today"), KeyboardButton("🗺 Prognosis")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("🔔 Send now")],
        [KeyboardButton("⏸ Pause/Resume"), KeyboardButton("♻️ Reset")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🕒 Change notify time"), KeyboardButton("🌍 Change timezone")],
        [KeyboardButton("⬅️ Back")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True,
)

# ----------------------------
# Data model
# ----------------------------
@dataclass
class Profile:
    chat_id: int
    partner_name: str
    partner_dob: Optional[str]
    period_start: str
    period_end: str
    cycle_length: int
    notify_time: str  # HH:MM
    tz_offset: str = DEFAULT_TZ_OFFSET
    paused: bool = False
    created_at: str = ""
    updated_at: str = ""

    def touch(self):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if not self.created_at:
            self.created_at = now
        self.updated_at = now


profiles: Dict[int, Profile] = {}


# ----------------------------
# Persistence (JSON file)
# ----------------------------
def ensure_data_dir():
    try:
        os.makedirs(DATA_PATH, exist_ok=True)
    except Exception:
        pass


def load_profiles():
    ensure_data_dir()
    global profiles
    try:
        if os.path.exists(PROFILES_FILE):
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            loaded = {}
            for k, v in raw.items():
                loaded[int(k)] = Profile(**v)
            profiles = loaded
            log.info("Loaded profiles: %s", len(profiles))
        else:
            log.info("No profiles.json found, starting empty.")
    except Exception as e:
        log.exception("Failed to load profiles: %s", e)


def save_profiles():
    ensure_data_dir()
    try:
        raw = {str(k): asdict(v) for k, v in profiles.items()}
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.exception("Failed to save profiles: %s", e)


# ----------------------------
# Helpers: parsing & cycle math
# ----------------------------
def parse_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except Exception:
        return None


def parse_hhmm(s: str) -> Optional[time]:
    try:
        hh, mm = s.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:
        return None


def parse_tz_offset(s: str) -> Optional[int]:
    """
    Returns offset minutes for strings like +01:00, -05:30
    """
    s = s.strip()
    if len(s) < 6 or (s[0] not in ["+", "-"]) or (":" not in s):
        return None
    sign = 1 if s[0] == "+" else -1
    try:
        hh = int(s[1:3])
        mm = int(s[4:6])
        return sign * (hh * 60 + mm)
    except Exception:
        return None


def now_local(tz_offset: str) -> datetime:
    off = parse_tz_offset(tz_offset) or 0
    return datetime.utcnow() + timedelta(minutes=off)


def local_to_utc_time(local_t: time, tz_offset: str) -> time:
    """
    Convert user's local HH:MM to UTC HH:MM using offset.
    This ignores DST changes (acceptable MVP).
    """
    off = parse_tz_offset(tz_offset) or 0
    # local = utc + off => utc = local - off
    base = datetime(2000, 1, 1, local_t.hour, local_t.minute) - timedelta(minutes=off)
    return time(base.hour, base.minute)


def get_day_number(p: Profile) -> int:
    start = parse_date(p.period_start)
    if not start:
        return 1
    today_local = now_local(p.tz_offset).date()
    delta = (today_local - start.date()).days
    day = (delta % p.cycle_length) + 1
    return max(1, min(day, p.cycle_length))


def phase_for_day(day: int) -> str:
    # Simple 28-ish mapping; works for MVP
    if 1 <= day <= 5:
        return "🌧 Menstrual"
    if 6 <= day <= 13:
        return "🌱 Follicular"
    if 14 <= day <= 17:
        return "🌼 Ovulatory"
    return "🍂 Luteal"


def stats_for_phase(phase: str) -> Dict[str, str]:
    # “Game-ish” values (MVP-level heuristics)
    if "Menstrual" in phase:
        return {
            "⚡ Energy": "Low 🔋",
            "🎭 Mood Stability": "Fragile 🌀",
            "🗣 Social Drive": "Low 💤",
            "❤️ Emotional Needs": "Comfort + Warmth 🫖",
            "🔥 Anxiety": "Medium ↑",
            "💢 Irritability": "Medium ↑",
            "🍩 Cravings": "High 🍫",
            "💕 Sexual Drive": "Low ↓",
            "🧠 Focus": "Lower 🌫",
        }
    if "Follicular" in phase:
        return {
            "⚡ Energy": "High ⚡",
            "🎭 Mood Stability": "Balanced 🙂",
            "🗣 Social Drive": "High 📣",
            "❤️ Emotional Needs": "Encouragement + Fun 🎯",
            "🔥 Anxiety": "Low ✅",
            "💢 Irritability": "Low ✅",
            "🍩 Cravings": "Low → Medium",
            "💕 Sexual Drive": "Rising 🔥",
            "🧠 Focus": "Sharp 🧩",
        }
    if "Ovulatory" in phase:
        return {
            "⚡ Energy": "Peak 🚀",
            "🎭 Mood Stability": "Strong 😎",
            "🗣 Social Drive": "Very High 🏟",
            "❤️ Emotional Needs": "Connection + Compliments 💬",
            "🔥 Anxiety": "Very Low 🧊",
            "💢 Irritability": "Very Low 🕊",
            "🍩 Cravings": "Medium",
            "💕 Sexual Drive": "Very High 💥",
            "🧠 Focus": "Very Sharp 🎯",
        }
    # Luteal
    return {
        "⚡ Energy": "Declining ↘",
        "🎭 Mood Stability": "Unstable ⚠️",
        "🗣 Social Drive": "Low ↘",
        "❤️ Emotional Needs": "Reassurance + Space 🫂",
        "🔥 Anxiety": "Can Increase 🔥",
        "💢 Irritability": "High 💢",
        "🍩 Cravings": "High 🍕",
        "💕 Sexual Drive": "Medium ↘",
        "🧠 Focus": "Weaker 😵",
    }


def recommendations(phase: str) -> Dict[str, str]:
    if "Menstrual" in phase:
        return {
            "🤝 Together": "Keep plans light. Offer warmth. No pressure.",
            "🍲 Food": "Soup, tea, chocolate, comfort carbs.",
            "🧘 Care": "Heat pad, slow walk, quiet company.",
            "💬 Words": "“I’m here. Want help or space?”",
        }
    if "Follicular" in phase:
        return {
            "🤝 Together": "Do something new. Short adventure.",
            "🍲 Food": "Fresh meals, protein + fruits.",
            "🧘 Care": "Encourage goals, playful vibe.",
            "💬 Words": "“Let’s plan something fun.”",
        }
    if "Ovulatory" in phase:
        return {
            "🤝 Together": "Date night, deeper talk, closeness.",
            "🍲 Food": "Balanced meal + something celebratory.",
            "🧘 Care": "Compliments, attention, affection.",
            "💬 Words": "“You’re glowing today.”",
        }
    return {
        "🤝 Together": "Reduce friction. Give space + stability.",
        "🍲 Food": "Comfort food, magnesium-ish snacks (nuts/dark choc).",
        "🧘 Care": "Calm tone, avoid debates, simplify decisions.",
        "💬 Words": "“I’ve got you. What would help right now?”",
    }


def build_today_message(p: Profile) -> str:
    day = get_day_number(p)
    ph = phase_for_day(day)
    stats = stats_for_phase(ph)
    rec = recommendations(ph)

    header = f"🎮 **DayCue** - Daily HUD\n"
    core = (
        f"👤 Partner: **{p.partner_name}**\n"
        f"📅 Cycle Day: **{day}/{p.cycle_length}**\n"
        f"🧭 Phase: **{ph}**\n"
    )

    stats_lines = "\n".join([f"- {k}: **{v}**" for k, v in stats.items()])
    rec_lines = "\n".join([f"- {k}: {v}" for k, v in rec.items()])

    return (
        f"{header}\n"
        f"{core}\n"
        f"📊 **Stats**\n{stats_lines}\n\n"
        f"🧰 **Recommendations**\n{rec_lines}\n"
    )


def build_prognosis_message(p: Profile) -> str:
    # 7-day simple view
    today_day = get_day_number(p)
    lines = ["🗺 **7-day Prognosis**"]
    for i in range(0, 7):
        d = ((today_day - 1 + i) % p.cycle_length) + 1
        ph = phase_for_day(d)
        tag = "✅" if "Follicular" in ph or "Ovulatory" in ph else "⚠️" if "Luteal" in ph else "🌧"
        lines.append(f"- Day {d}: {ph} {tag}")
    return "\n".join(lines)


# ----------------------------
# Scheduling (JobQueue)
# ----------------------------
async def send_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    p = profiles.get(chat_id)
    if not p:
        return
    if p.paused:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_today_message(p),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
    except Exception as e:
        log.exception("Failed to send daily message: %s", e)


def ensure_daily_job(app: Application, p: Profile) -> Tuple[time, datetime]:
    """
    Schedule run_daily in UTC based on user's tz offset.
    Returns (utc_time, next_run_utc_datetime)
    """
    local_t = parse_hhmm(p.notify_time) or time(9, 0)
    utc_t = local_to_utc_time(local_t, p.tz_offset)

    job_name = f"daily:{p.chat_id}"

    # Remove old job with same name
    existing = app.job_queue.get_jobs_by_name(job_name)
    for j in existing:
        j.schedule_removal()

    app.job_queue.run_daily(
        callback=send_daily_job,
        time=utc_t,
        chat_id=p.chat_id,
        name=job_name,
    )

    # Compute next run (approx) for confirmation
    now_utc = datetime.utcnow()
    next_run = datetime.combine(now_utc.date(), utc_t)
    if next_run <= now_utc:
        next_run += timedelta(days=1)

    return utc_t, next_run


# ----------------------------
# Onboarding conversation
# ----------------------------
(
    STEP_NAME,
    STEP_DOB,
    STEP_START,
    STEP_END,
    STEP_LENGTH,
    STEP_NOTIFY,
) = range(6)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    profiles.pop(chat_id, None)  # restart onboarding fresh
    save_profiles()

    await update.message.reply_text(
        "👋 Welcome.\n\n"
        "Quick onboarding.\n\n"
        "1/6 - Enter partner nickname (example: Anna)",
        reply_markup=MAIN_MENU,
    )
    return STEP_NAME


async def step_partner_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    name = (update.message.text or "").strip()
    if len(name) < 1:
        await update.message.reply_text("Please enter a name.", reply_markup=MAIN_MENU)
        return STEP_NAME

    p = Profile(
        chat_id=chat_id,
        partner_name=name,
        partner_dob=None,
        period_start="",
        period_end="",
        cycle_length=28,
        notify_time="09:00",
        tz_offset=DEFAULT_TZ_OFFSET,
    )
    p.touch()
    profiles[chat_id] = p
    save_profiles()

    await update.message.reply_text(
        "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'",
        reply_markup=MAIN_MENU,
    )
    return STEP_DOB


async def step_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    p = profiles.get(chat_id)
    if not p:
        return await start(update, context)

    if txt.lower() != "skip":
        d = parse_date(txt)
        if not d:
            await update.message.reply_text(
                "Invalid date. Use YYYY-MM-DD or type 'skip'.",
                reply_markup=MAIN_MENU,
            )
            return STEP_DOB
        p.partner_dob = txt

    p.touch()
    save_profiles()

    await update.message.reply_text(
        "3/6 - Last period START date (YYYY-MM-DD)",
        reply_markup=MAIN_MENU,
    )
    return STEP_START


async def step_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    p = profiles.get(chat_id)
    if not p:
        return await start(update, context)

    d = parse_date(txt)
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.", reply_markup=MAIN_MENU)
        return STEP_START

    p.period_start = txt
    p.touch()
    save_profiles()

    await update.message.reply_text(
        "4/6 - Last period END date (YYYY-MM-DD)",
        reply_markup=MAIN_MENU,
    )
    return STEP_END


async def step_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    p = profiles.get(chat_id)
    if not p:
        return await start(update, context)

    d = parse_date(txt)
    if not d:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.", reply_markup=MAIN_MENU)
        return STEP_END

    p.period_end = txt
    p.touch()
    save_profiles()

    await update.message.reply_text(
        "5/6 - Cycle length in days (21-35). Example: 28",
        reply_markup=MAIN_MENU,
    )
    return STEP_LENGTH


async def step_cycle_length(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    p = profiles.get(chat_id)
    if not p:
        return await start(update, context)

    try:
        n = int(txt)
        if n < 21 or n > 35:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Enter a number between 21 and 35.", reply_markup=MAIN_MENU)
        return STEP_LENGTH

    p.cycle_length = n
    p.touch()
    save_profiles()

    await update.message.reply_text(
        "6/6 - Daily notification time (HH:MM). Example: 09:00\n\n"
        "Note: daily notifications run once per day at this clock time.",
        reply_markup=MAIN_MENU,
    )
    return STEP_NOTIFY


async def step_notify_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    p = profiles.get(chat_id)
    if not p:
        return await start(update, context)

    t = parse_hhmm(txt)
    if not t:
        await update.message.reply_text("Invalid time. Use HH:MM (example 09:00).", reply_markup=MAIN_MENU)
        return STEP_NOTIFY

    p.notify_time = txt
    p.paused = False
    p.touch()
    save_profiles()

    # Schedule daily job (UTC converted)
    utc_t, next_run_utc = ensure_daily_job(context.application, p)

    # Confirm next run in user's local time (approx)
    next_local = next_run_utc + timedelta(minutes=(parse_tz_offset(p.tz_offset) or 0))

    await update.message.reply_text(
        f"✅ Saved.\n"
        f"🕒 Daily notify: {p.notify_time} (TZ {p.tz_offset})\n"
        f"⏭ Next notify: {next_local.strftime('%Y-%m-%d %H:%M')} (local)\n\n"
        f"🔔 Preview now (this is instant, not scheduled):",
        reply_markup=MAIN_MENU,
    )

    # Send preview immediately
    await update.message.reply_text(
        build_today_message(p),
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Type /start to onboard again.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ----------------------------
# Menu actions
# ----------------------------
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🎮 Menu ready.", reply_markup=MAIN_MENU)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)


async def cmd_prognosis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text(build_prognosis_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)


async def cmd_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text("🔔 Sending now:", reply_markup=MAIN_MENU)
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)


async def cmd_pause_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start.", reply_markup=MAIN_MENU)
        return

    p.paused = not p.paused
    p.touch()
    save_profiles()

    state = "⏸ Paused" if p.paused else "▶️ Active"
    await update.message.reply_text(f"{state}.", reply_markup=MAIN_MENU)

    # If resumed, ensure schedule exists
    if not p.paused:
        ensure_daily_job(context.application, p)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    profiles.pop(chat_id, None)
    save_profiles()
    await update.message.reply_text("♻️ Reset done. Type /start to onboard again.", reply_markup=MAIN_MENU)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⚙️ Settings", reply_markup=SETTINGS_MENU)


async def on_settings_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    if txt == "⬅️ Back":
        await update.message.reply_text("🎮 Back to menu.", reply_markup=MAIN_MENU)
        return

    if txt == "🕒 Change notify time":
        context.user_data["awaiting_notify_time"] = True
        await update.message.reply_text("Enter new daily time (HH:MM), example 09:00", reply_markup=SETTINGS_MENU)
        return

    if txt == "🌍 Change timezone":
        context.user_data["awaiting_tz"] = True
        await update.message.reply_text("Enter timezone offset like +01:00 or -05:00", reply_markup=SETTINGS_MENU)
        return

    # If user typed a value while in settings
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start.", reply_markup=MAIN_MENU)
        return

    if context.user_data.get("awaiting_notify_time"):
        t = parse_hhmm(txt)
        if not t:
            await update.message.reply_text("Invalid time. Use HH:MM.", reply_markup=SETTINGS_MENU)
            return
        p.notify_time = txt
        p.touch()
        save_profiles()
        ensure_daily_job(context.application, p)
        context.user_data["awaiting_notify_time"] = False
        await update.message.reply_text(f"✅ Notify time updated to {p.notify_time}.", reply_markup=SETTINGS_MENU)
        return

    if context.user_data.get("awaiting_tz"):
        off = parse_tz_offset(txt)
        if off is None:
            await update.message.reply_text("Invalid offset. Use format +01:00 or -05:00.", reply_markup=SETTINGS_MENU)
            return
        p.tz_offset = txt
        p.touch()
        save_profiles()
        ensure_daily_job(context.application, p)
        context.user_data["awaiting_tz"] = False
        await update.message.reply_text(f"✅ Timezone updated to {p.tz_offset}.", reply_markup=SETTINGS_MENU)
        return

    await update.message.reply_text("Choose an option.", reply_markup=SETTINGS_MENU)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (update.message.text or "").strip()

    if txt == "🎮 Today":
        return await cmd_today(update, context)
    if txt == "🗺 Prognosis":
        return await cmd_prognosis(update, context)
    if txt == "⚙️ Settings":
        return await cmd_settings(update, context)
    if txt == "🔔 Send now":
        return await cmd_send_now(update, context)
    if txt == "⏸ Pause/Resume":
        return await cmd_pause_resume(update, context)
    if txt == "♻️ Reset":
        return await cmd_reset(update, context)

    # If user is in settings menu flow
    if update.message and update.message.reply_markup == SETTINGS_MENU:
        return await on_settings_choice(update, context)

    # Default fallback: show menu
    await update.message.reply_text("🎮 Use the menu buttons.", reply_markup=MAIN_MENU)


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    log.info("BOOT: starting bot.py")
    load_profiles()

    app = Application.builder().token(TOKEN).build()

    # Onboarding conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STEP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_partner_name)],
            STEP_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_dob)],
            STEP_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_start)],
            STEP_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_end)],
            STEP_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cycle_length)],
            STEP_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_notify_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)

    # Commands
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("prognosis", cmd_prognosis))
    app.add_handler(CommandHandler("sendnow", cmd_send_now))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # Main menu handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu))

    # Re-schedule existing profiles on boot (best effort)
    for p in profiles.values():
        try:
            if not p.paused and p.notify_time:
                ensure_daily_job(app, p)
        except Exception as e:
            log.exception("Failed scheduling on boot for %s: %s", p.chat_id, e)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
