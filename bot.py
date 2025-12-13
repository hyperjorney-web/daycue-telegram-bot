import os
import re
import sys
import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from typing import Dict, Optional, Tuple

from zoneinfo import ZoneInfo

from aiohttp import web

from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("daycue-bot")

# ----------------------------
# ENV
# ----------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

PORT = int(os.getenv("PORT", "8080"))
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Stockholm")  # change if you want

# ----------------------------
# UI: Main Menu Keyboard
# ----------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Today"), KeyboardButton("Prognosis")],
        [KeyboardButton("Settings"), KeyboardButton("Send now")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Choose an action…",
)

# ----------------------------
# MVP storage (in-memory)
# NOTE: resets on restart
# ----------------------------

@dataclass
class Profile:
    chat_id: int
    partner_name: str
    partner_dob: Optional[str]  # YYYY-MM-DD or None
    period_start: str           # YYYY-MM-DD
    period_end: str             # YYYY-MM-DD
    cycle_length: int           # 21-35
    notify_time: str            # HH:MM (local TZ)
    tz: str                     # IANA TZ name
    paused: bool = False
    created_at: str = ""

profiles: Dict[int, Profile] = {}

# ----------------------------
# Conversation states
# ----------------------------
(
    STEP_PARTNER_NAME,
    STEP_DOB,
    STEP_PERIOD_START,
    STEP_PERIOD_END,
    STEP_CYCLE_LENGTH,
    STEP_NOTIFY_TIME,
) = range(6)

# ----------------------------
# Helpers: parse/validate
# ----------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

def parse_date(s: str) -> Optional[date]:
    s = s.strip()
    if not DATE_RE.match(s):
        return None
    try:
        y, m, d = map(int, s.split("-"))
        return date(y, m, d)
    except Exception:
        return None

def parse_time(s: str) -> Optional[time]:
    s = s.strip()
    if not TIME_RE.match(s):
        return None
    try:
        hh, mm = map(int, s.split(":"))
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return None
        return time(hour=hh, minute=mm)
    except Exception:
        return None

def safe_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")

def now_in_tz(tz_name: str) -> datetime:
    return datetime.now(tz=safe_tz(tz_name))

def profile_ready(chat_id: int) -> bool:
    return chat_id in profiles and not profiles[chat_id].paused

# ----------------------------
# Cycle model (simple MVP)
# ----------------------------
def compute_cycle_day(today: date, start: date, cycle_len: int) -> int:
    # Day number 1..cycle_len
    delta = (today - start).days
    if delta < 0:
        return 1
    return (delta % cycle_len) + 1

def phase_ranges(cycle_len: int, period_len: int) -> Tuple[Tuple[int,int], Tuple[int,int], Tuple[int,int], Tuple[int,int]]:
    # Menstrual: 1..period_len
    # Follicular: (period_len+1)..(ovulation_day-1)
    # Ovulatory: ovulation_day..(ovulation_day+2)
    # Luteal: rest
    period_len = max(3, min(7, period_len))
    ovulation_day = max(12, min(cycle_len - 12, cycle_len - 14))  # rough; keeps in sane range
    ov_start = ovulation_day
    ov_end = min(cycle_len, ov_start + 2)

    men = (1, period_len)
    fol = (period_len + 1, max(period_len + 1, ov_start - 1))
    ovu = (ov_start, ov_end)
    lut = (ov_end + 1, cycle_len)
    return men, fol, ovu, lut

def pick_phase(day_num: int, cycle_len: int, period_len: int) -> str:
    men, fol, ovu, lut = phase_ranges(cycle_len, period_len)
    if men[0] <= day_num <= men[1]:
        return "Menstrual"
    if fol[0] <= day_num <= fol[1]:
        return "Follicular"
    if ovu[0] <= day_num <= ovu[1]:
        return "Ovulatory"
    return "Luteal"

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def hormone_levels(day_num: int, cycle_len: int, period_len: int) -> Dict[str, int]:
    """
    Very simplified normalized 0-100 curve:
    - Estrogen rises in follicular, peaks near ovulation, small bump in luteal.
    - Progesterone low until after ovulation, then high in luteal.
    - LH spike at ovulation.
    - FSH small rise early.
    """
    men, fol, ovu, lut = phase_ranges(cycle_len, period_len)

    estrogen = 35
    progesterone = 15
    lh = 10
    fsh = 18

    # progress helpers
    def prog_in_range(r: Tuple[int,int]) -> float:
        lo, hi = r
        if hi <= lo:
            return 1.0
        return clamp01((day_num - lo) / (hi - lo))

    phase = pick_phase(day_num, cycle_len, period_len)

    if phase == "Menstrual":
        t = prog_in_range(men)
        estrogen = int(lerp(30, 40, t))
        progesterone = int(lerp(20, 10, t))
        lh = 10
        fsh = int(lerp(25, 18, t))
    elif phase == "Follicular":
        t = prog_in_range(fol)
        estrogen = int(lerp(40, 85, t))
        progesterone = 15
        lh = int(lerp(10, 20, t))
        fsh = int(lerp(18, 15, t))
    elif phase == "Ovulatory":
        t = prog_in_range(ovu)
        estrogen = int(lerp(85, 70, t))
        progesterone = int(lerp(15, 25, t))
        lh = int(lerp(95, 60, t))
        fsh = int(lerp(20, 18, t))
    else:  # Luteal
        # split luteal: early high progesterone then decline
        lo, hi = lut
        mid = lo + max(1, (hi - lo) // 2)
        if day_num <= mid:
            t = clamp01((day_num - lo) / max(1, (mid - lo)))
            estrogen = int(lerp(70, 60, t))
            progesterone = int(lerp(30, 85, t))
            lh = 12
            fsh = 14
        else:
            t = clamp01((day_num - mid) / max(1, (hi - mid)))
            estrogen = int(lerp(60, 40, t))
            progesterone = int(lerp(85, 25, t))
            lh = 10
            fsh = 16

    return {
        "estrogen": max(0, min(100, estrogen)),
        "progesterone": max(0, min(100, progesterone)),
        "LH": max(0, min(100, lh)),
        "FSH": max(0, min(100, fsh)),
    }

def state_levels(phase: str) -> Dict[str, int]:
    """
    0-100 “game bars” for MVP.
    """
    if phase == "Menstrual":
        return {
            "Mood Stability": 45,
            "Social Drive": 35,
            "Emotional Needs": 75,
            "Anxiety Level": 55,
            "Irritability": 60,
            "Cravings": 80,
            "Sexual Drive": 35,
            "Cognitive Focus": 45,
        }
    if phase == "Follicular":
        return {
            "Mood Stability": 70,
            "Social Drive": 70,
            "Emotional Needs": 45,
            "Anxiety Level": 30,
            "Irritability": 30,
            "Cravings": 35,
            "Sexual Drive": 55,
            "Cognitive Focus": 75,
        }
    if phase == "Ovulatory":
        return {
            "Mood Stability": 80,
            "Social Drive": 85,
            "Emotional Needs": 55,
            "Anxiety Level": 25,
            "Irritability": 20,
            "Cravings": 40,
            "Sexual Drive": 85,
            "Cognitive Focus": 70,
        }
    # Luteal
    return {
        "Mood Stability": 50,
        "Social Drive": 40,
        "Emotional Needs": 80,
        "Anxiety Level": 55,
        "Irritability": 70,
        "Cravings": 75,
        "Sexual Drive": 50,
        "Cognitive Focus": 50,
    }

def recommendations(phase: str) -> Dict[str, str]:
    if phase == "Menstrual":
        return {
            "Together": "Keep it gentle: low-pressure time, comfort, no “big talks” unless she starts.",
            "Food": "Warm comfort food, iron-rich meals, hydration. Small treats are strategic diplomacy.",
            "Care": "Offer practical help (tea, heat pad, errands). Ask: “Do you want help or space?”",
        }
    if phase == "Follicular":
        return {
            "Together": "Plan something active: walk, gym, new café, playful banter.",
            "Food": "Lighter meals, protein + fresh carbs. Energy is back—ride the wave.",
            "Care": "Encourage goals and fun. This is “let’s build” mode.",
        }
    if phase == "Ovulatory":
        return {
            "Together": "Date night energy. Compliments land well. Connection + fun.",
            "Food": "Balanced meals; keep it simple. Social calendar can get full—help reduce friction.",
            "Care": "Be present. Match her energy but don’t compete with it.",
        }
    return {
        "Together": "Stability matters. Keep promises. Reduce stress, avoid sarcasm-as-a-hobby.",
        "Food": "Comfort cravings are real. Magnesium-ish foods, warm dinners, steady snacks.",
        "Care": "Reassurance + space. Ask before problem-solving; be the calm anchor.",
    }

def build_today_message(p: Profile) -> str:
    tz = safe_tz(p.tz)
    now = datetime.now(tz=tz)
    today = now.date()

    start = parse_date(p.period_start) or today
    end_ = parse_date(p.period_end) or today
    period_len = max(1, (end_ - start).days + 1)

    day_num = compute_cycle_day(today, start, p.cycle_length)
    phase = pick_phase(day_num, p.cycle_length, period_len)
    hormones = hormone_levels(day_num, p.cycle_length, period_len)
    levels = state_levels(phase)
    rec = recommendations(phase)

    # light prognosis (next 3 days)
    prog = []
    for i in range(1, 4):
        d = ((day_num - 1 + i) % p.cycle_length) + 1
        ph = pick_phase(d, p.cycle_length, period_len)
        prog.append(f"D+{i}: {ph}")

    msg = []
    msg.append(f"📅 *Today* ({today.isoformat()})")
    msg.append(f"Partner: *{p.partner_name}*")
    msg.append(f"Cycle: Day *{day_num}/{p.cycle_length}* - *{phase}*")
    msg.append("")
    msg.append("🧪 *Hormones (0-100)*")
    msg.append(f"- Estrogen: {hormones['estrogen']}")
    msg.append(f"- Progesterone: {hormones['progesterone']}")
    msg.append(f"- LH: {hormones['LH']}")
    msg.append(f"- FSH: {hormones['FSH']}")
    msg.append("")
    msg.append("🎮 *Hero stats (0-100)*")
    for k, v in levels.items():
        msg.append(f"- {k}: {v}")
    msg.append("")
    msg.append("💡 *Recommendations*")
    msg.append(f"👫 Together: {rec['Together']}")
    msg.append(f"🍲 Food: {rec['Food']}")
    msg.append(f"🧸 Care: {rec['Care']}")
    msg.append("")
    msg.append("🔮 *Next 3 days*")
    msg.extend([f"- {x}" for x in prog])

    return "\n".join(msg)

# ----------------------------
# Jobs: daily notifications
# ----------------------------
async def send_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    p = profiles.get(chat_id)
    if not p or p.paused:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_today_message(p),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
    except Exception:
        log.exception("Failed to send daily message to chat_id=%s", chat_id)

def remove_existing_daily_jobs(app: Application, chat_id: int) -> None:
    name = f"daily:{chat_id}"
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

def schedule_daily(app: Application, p: Profile) -> None:
    remove_existing_daily_jobs(app, p.chat_id)

    tz = safe_tz(p.tz)
    t = parse_time(p.notify_time) or time(9, 0)
    # Put tzinfo on time so JobQueue knows timezone without needing `timezone=...`
    t = time(hour=t.hour, minute=t.minute, tzinfo=tz)

    app.job_queue.run_daily(
        callback=send_daily_job,
        time=t,
        chat_id=p.chat_id,
        name=f"daily:{p.chat_id}",
    )
    log.info("Scheduled daily job chat_id=%s at %s (%s)", p.chat_id, p.notify_time, p.tz)

# ----------------------------
# Handlers: commands & menu
# ----------------------------
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Menu:", reply_markup=MAIN_MENU)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start to onboard.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)

async def cmd_prognosis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start to onboard.", reply_markup=MAIN_MENU)
        return

    tz = safe_tz(p.tz)
    today = datetime.now(tz=tz).date()
    start = parse_date(p.period_start) or today
    end_ = parse_date(p.period_end) or today
    period_len = max(1, (end_ - start).days + 1)

    day_num = compute_cycle_day(today, start, p.cycle_length)

    lines = ["🔮 *Prognosis (7 days)*"]
    for i in range(0, 7):
        d = ((day_num - 1 + i) % p.cycle_length) + 1
        ph = pick_phase(d, p.cycle_length, period_len)
        lines.append(f"- Day {d}: {ph}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_MENU)

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start to onboard.", reply_markup=MAIN_MENU)
        return

    data = asdict(p)
    data.pop("chat_id", None)

    lines = ["⚙️ *Settings*", "Current profile:"]
    for k, v in data.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("To change anything: type /start to re-onboard.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_MENU)

async def cmd_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    p = profiles.get(chat_id)
    if not p:
        await update.message.reply_text("No profile yet. Type /start to onboard.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text("🔔 Sending now…", reply_markup=MAIN_MENU)
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == "Today":
        await cmd_today(update, context)
        return
    if text == "Prognosis":
        await cmd_prognosis(update, context)
        return
    if text == "Settings":
        await cmd_settings(update, context)
        return
    if text == "Send now":
        await cmd_send_now(update, context)
        return

    # fallback
    await update.message.reply_text("Use the menu buttons or type /menu.", reply_markup=MAIN_MENU)

# ----------------------------
# Onboarding conversation
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Welcome. Quick onboarding.\n\n1/6 - Enter partner nickname (example: Anna)",
        reply_markup=MAIN_MENU,
    )
    return STEP_PARTNER_NAME

async def step_partner_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 1:
        await update.message.reply_text("Please enter a nickname (example: Anna)")
        return STEP_PARTNER_NAME

    context.user_data["partner_name"] = name
    await update.message.reply_text("2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
    return STEP_DOB

async def step_dob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = (update.message.text or "").strip().lower()
    if s == "skip":
        context.user_data["partner_dob"] = None
    else:
        d = parse_date(s)
        if not d:
            await update.message.reply_text("DOB must be YYYY-MM-DD or type 'skip'")
            return STEP_DOB
        context.user_data["partner_dob"] = d.isoformat()

    await update.message.reply_text("3/6 - Last period START date (YYYY-MM-DD)")
    return STEP_PERIOD_START

async def step_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await update.message.reply_text("Start date must be YYYY-MM-DD")
        return STEP_PERIOD_START

    context.user_data["period_start"] = d
    await update.message.reply_text("4/6 - Last period END date (YYYY-MM-DD)")
    return STEP_PERIOD_END

async def step_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text or "")
    if not d:
        await update.message.reply_text("End date must be YYYY-MM-DD")
        return STEP_PERIOD_END

    start_d: date = context.user_data["period_start"]
    if d < start_d:
        await update.message.reply_text("End date can’t be before start date. Try again.")
        return STEP_PERIOD_END

    context.user_data["period_end"] = d
    await update.message.reply_text("5/6 - Cycle length in days (21-35). Example: 28")
    return STEP_CYCLE_LENGTH

async def step_cycle_length(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = (update.message.text or "").strip()
    if not s.isdigit():
        await update.message.reply_text("Enter a number between 21 and 35")
        return STEP_CYCLE_LENGTH

    n = int(s)
    if n < 21 or n > 35:
        await update.message.reply_text("Cycle length must be 21-35")
        return STEP_CYCLE_LENGTH

    context.user_data["cycle_length"] = n
    await update.message.reply_text("6/6 - Daily notification time (HH:MM). Example: 09:00")
    return STEP_NOTIFY_TIME

async def step_notify_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_time(update.message.text or "")
    if not t:
        await update.message.reply_text("Time must be HH:MM (example: 09:00)")
        return STEP_NOTIFY_TIME

    chat_id = update.effective_chat.id
    tz_name = DEFAULT_TZ  # MVP: one timezone, can be made user-specific later

    p = Profile(
        chat_id=chat_id,
        partner_name=context.user_data["partner_name"],
        partner_dob=context.user_data.get("partner_dob"),
        period_start=context.user_data["period_start"].isoformat(),
        period_end=context.user_data["period_end"].isoformat(),
        cycle_length=context.user_data["cycle_length"],
        notify_time=f"{t.hour:02d}:{t.minute:02d}",
        tz=tz_name,
        paused=False,
        created_at=datetime.utcnow().isoformat(),
    )
    profiles[chat_id] = p

    # schedule daily
    schedule_daily(context.application, p)

    # confirm + menu
    await update.message.reply_text(
        "✅ Setup complete.\n\nUse the menu below:\n- Today\n- Prognosis\n- Settings\n- Send now (test instantly)",
        reply_markup=MAIN_MENU,
    )

    # instant test message
    await update.message.reply_text(
        "🔔 Test: sending your first message now.",
        reply_markup=MAIN_MENU,
    )
    await update.message.reply_text(build_today_message(p), parse_mode="Markdown", reply_markup=MAIN_MENU)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Type /start to begin again.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# ----------------------------
# Health server for Fly (port)
# ----------------------------
async def health_server() -> web.AppRunner:
    async def handle_health(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)
    return runner

# ----------------------------
# Build Application
# ----------------------------
def build_application() -> Application:
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STEP_PARTNER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_partner_name)],
            STEP_DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_dob)],
            STEP_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_start)],
            STEP_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_period_end)],
            STEP_CYCLE_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cycle_length)],
            STEP_NOTIFY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_notify_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app = Application.builder().token(TOKEN).build()

    # commands
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("prognosis", cmd_prognosis))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("sendnow", cmd_send_now))

    # onboarding
    app.add_handler(conv)

    # menu buttons (non-command messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_button))

    return app

# ----------------------------
# Async main
# ----------------------------
async def main() -> None:
    log.info("BOOT: starting bot.py")
    runner = await health_server()

    application = build_application()

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        log.info("Bot polling started.")

        # Keep running forever
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
