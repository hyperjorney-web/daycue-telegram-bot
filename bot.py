import os
import sys
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# =========================
# Config
# =========================
APP_VERSION = "0.9"
TZ = ZoneInfo("Europe/Stockholm")  # ändra om du vill
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

# =========================
# Data (MVP: in-memory)
# =========================
@dataclass
class Profile:
    chat_id: int
    partner_name: str
    partner_dob: str | None
    period_start: str         # YYYY-MM-DD
    period_end: str           # YYYY-MM-DD
    cycle_length: int         # 21-35
    notify_time: str          # HH:MM
    paused: bool = False
    last_sent_date: str | None = None  # YYYY-MM-DD

PROFILES: dict[int, Profile] = {}
ONBOARDING_STATE: dict[int, dict] = {}  # chat_id -> {"step": int, "data": {...}}

# =========================
# UI: Meny (alltid)
# =========================
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🧭 Idag", "🔮 Prognos"],
        ["⚙️ Inställningar", "🔔 Skicka nu"],
        ["⏸️ Pausa", "▶️ Starta"],
    ],
    resize_keyboard=True
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [
        ["✏️ Ändra cykeldata", "🕒 Ändra notistid"],
        ["⬅️ Tillbaka"],
    ],
    resize_keyboard=True
)

# =========================
# Helpers
# =========================
def parse_yyyy_mm_dd(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def parse_hh_mm(s: str) -> time | None:
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except Exception:
        return None

def cycle_day_for(profile: Profile, on_date: date) -> int:
    start = parse_yyyy_mm_dd(profile.period_start)
    if not start:
        return 1
    delta = (on_date - start).days
    # day 1..cycle_length
    return (delta % profile.cycle_length) + 1

def phase_for(profile: Profile, day_no: int) -> str:
    # enkel modell, skalar med cykellängd
    L = profile.cycle_length
    # ungefärliga boundaries
    menstrual_end = max(4, round(L * 0.18))           # ~ dag 1-5
    ovulation_start = round(L * 0.45)                 # ~ dag 14 i 28
    ovulation_end = min(L, ovulation_start + 2)       # 2-3 dagar
    follicular_end = ovulation_start - 1

    if day_no <= menstrual_end:
        return "🩸 Menstruation"
    if day_no <= follicular_end:
        return "🌱 Follikulär"
    if ovulation_start <= day_no <= ovulation_end:
        return "🔥 Ägglossning"
    return "🌙 Luteal"

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def score_bar(label: str, emoji: str, value_0_100: int) -> str:
    # 10-stegs bar
    blocks = int(round(value_0_100 / 10))
    bar = "█" * blocks + "░" * (10 - blocks)
    return f"{emoji} {label}: {bar} {value_0_100}/100"

def stats_for_phase(phase: str) -> dict:
    # MVP: fasta profiler per fas
    if "Menstruation" in phase:
        return {
            "Mood Stability": ("🎭", 45),
            "Social Drive": ("🗣️", 35),
            "Emotional Needs": ("❤️", 75),
            "Anxiety": ("🔥", 55),
            "Irritability": ("💢", 60),
            "Cravings": ("🍩", 80),
            "Sexual Drive": ("💕", 30),
            "Focus": ("🧠", 40),
            "tips": [
                "🫖 Värme + vila (te, filt, lugn kväll)",
                "🧘 Kort promenad / stretch",
                "🤝 Tydlig omtanke: ”Jag fixar maten idag”",
            ],
        }
    if "Follikulär" in phase:
        return {
            "Mood Stability": ("🎭", 75),
            "Social Drive": ("🗣️", 75),
            "Emotional Needs": ("❤️", 55),
            "Anxiety": ("🔥", 30),
            "Irritability": ("💢", 25),
            "Cravings": ("🍩", 30),
            "Sexual Drive": ("💕", 55),
            "Focus": ("🧠", 80),
            "tips": [
                "🎯 Planera saker ni vill få gjort",
                "🏃 Aktivitet tillsammans (gym, promenad, äventyr)",
                "💬 Pepp + framtidssnack",
            ],
        }
    if "Ägglossning" in phase:
        return {
            "Mood Stability": ("🎭", 85),
            "Social Drive": ("🗣️", 90),
            "Emotional Needs": ("❤️", 60),
            "Anxiety": ("🔥", 20),
            "Irritability": ("💢", 15),
            "Cravings": ("🍩", 35),
            "Sexual Drive": ("💕", 90),
            "Focus": ("🧠", 70),
            "tips": [
                "🌹 Ge komplimanger (specifikt!)",
                "🍽️ Date-night / social aktivitet",
                "🔥 Närhet + lekfullhet",
            ],
        }
    # Luteal
    return {
        "Mood Stability": ("🎭", 40),
        "Social Drive": ("🗣️", 40),
        "Emotional Needs": ("❤️", 80),
        "Anxiety": ("🔥", 60),
        "Irritability": ("💢", 75),
        "Cravings": ("🍩", 85),
        "Sexual Drive": ("💕", 50),
        "Focus": ("🧠", 35),
        "tips": [
            "🧩 Förenkla vardagen (minska friktion)",
            "🥣 Comfort food + snäll ton",
            "🛡️ Stabilitet: inga onödiga konflikter",
        ],
    }

def build_today_message(profile: Profile) -> str:
    today = datetime.now(TZ).date()
    d = cycle_day_for(profile, today)
    phase = phase_for(profile, d)
    stats = stats_for_phase(phase)

    lines = []
    lines.append(f"📅 Idag: {today.isoformat()}  |  Dag {d}/{profile.cycle_length}")
    lines.append(f"🧬 Fas: {phase}")
    lines.append("")
    lines.append(score_bar("Mood Stability", *stats["Mood Stability"]))
    lines.append(score_bar("Social Drive", *stats["Social Drive"]))
    lines.append(score_bar("Emotional Needs", *stats["Emotional Needs"]))
    lines.append(score_bar("Anxiety", *stats["Anxiety"]))
    lines.append(score_bar("Irritability", *stats["Irritability"]))
    lines.append(score_bar("Cravings", *stats["Cravings"]))
    lines.append(score_bar("Sexual Drive", *stats["Sexual Drive"]))
    lines.append(score_bar("Cognitive Focus", *stats["Focus"]))
    lines.append("")
    lines.append("🎒 Rekommendationer (MVP):")
    for t in stats["tips"]:
        lines.append(f"• {t}")

    return "\n".join(lines)

def build_forecast_message(profile: Profile) -> str:
    base = datetime.now(TZ).date()
    out = ["🔮 Prognos (7 dagar):"]
    for i in range(7):
        day = base + timedelta(days=i)
        d = cycle_day_for(profile, day)
        phase = phase_for(profile, d)
        out.append(f"• {day.isoformat()} — Dag {d}: {phase}")
    return "\n".join(out)

def profile_summary(profile: Profile) -> str:
    return (
        "⚙️ Dina inställningar:\n"
        f"• Partner: {profile.partner_name}\n"
        f"• DOB: {profile.partner_dob or 'skip'}\n"
        f"• Period start: {profile.period_start}\n"
        f"• Period end: {profile.period_end}\n"
        f"• Cykellängd: {profile.cycle_length}\n"
        f"• Notistid: {profile.notify_time}\n"
        f"• Pausad: {'ja' if profile.paused else 'nej'}"
    )

# =========================
# Onboarding flow
# =========================
def start_onboarding(chat_id: int):
    ONBOARDING_STATE[chat_id] = {"step": 1, "data": {}}

async def onboarding_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = ONBOARDING_STATE.get(chat_id)
    if not st:
        start_onboarding(chat_id)
        st = ONBOARDING_STATE[chat_id]

    step = st["step"]
    if step == 1:
        await update.message.reply_text(
            f"Welcome. Quick onboarding. 🧪 v{APP_VERSION}\n\n"
            "1/6 - Enter partner nickname (example: Anna)",
            reply_markup=MAIN_MENU
        )
    elif step == 2:
        await update.message.reply_text(
            "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'",
            reply_markup=MAIN_MENU
        )
    elif step == 3:
        await update.message.reply_text(
            "3/6 - Last period START date (YYYY-MM-DD)",
            reply_markup=MAIN_MENU
        )
    elif step == 4:
        await update.message.reply_text(
            "4/6 - Last period END date (YYYY-MM-DD)",
            reply_markup=MAIN_MENU
        )
    elif step == 5:
        await update.message.reply_text(
            "5/6 - Cycle length in days (21-35). Example: 28",
            reply_markup=MAIN_MENU
        )
    elif step == 6:
        await update.message.reply_text(
            "6/6 - Daily notification time (HH:MM). Example: 09:00",
            reply_markup=MAIN_MENU
        )

async def handle_onboarding_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    st = ONBOARDING_STATE.get(chat_id)
    if not st:
        start_onboarding(chat_id)
        st = ONBOARDING_STATE[chat_id]

    step = st["step"]
    data = st["data"]

    if step == 1:
        if len(txt) < 1:
            await update.message.reply_text("Skriv ett namn (minst 1 tecken).", reply_markup=MAIN_MENU)
            return
        data["partner_name"] = txt
        st["step"] = 2
        await onboarding_prompt(update, context)
        return

    if step == 2:
        if txt.lower() == "skip":
            data["partner_dob"] = None
        else:
            d = parse_yyyy_mm_dd(txt)
            if not d:
                await update.message.reply_text("Fel format. Ex: 1987-08-16 eller 'skip'", reply_markup=MAIN_MENU)
                return
            data["partner_dob"] = d.isoformat()
        st["step"] = 3
        await onboarding_prompt(update, context)
        return

    if step == 3:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Fel format. Ex: 2025-12-09", reply_markup=MAIN_MENU)
            return
        data["period_start"] = d.isoformat()
        st["step"] = 4
        await onboarding_prompt(update, context)
        return

    if step == 4:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Fel format. Ex: 2025-12-13", reply_markup=MAIN_MENU)
            return
        data["period_end"] = d.isoformat()
        st["step"] = 5
        await onboarding_prompt(update, context)
        return

    if step == 5:
        try:
            L = int(txt)
        except Exception:
            await update.message.reply_text("Skriv en siffra 21-35. Ex: 28", reply_markup=MAIN_MENU)
            return
        if L < 21 or L > 35:
            await update.message.reply_text("Cykellängd måste vara 21-35.", reply_markup=MAIN_MENU)
            return
        data["cycle_length"] = L
        st["step"] = 6
        await onboarding_prompt(update, context)
        return

    if step == 6:
        t = parse_hh_mm(txt)
        if not t:
            await update.message.reply_text("Fel format. Ex: 09:00", reply_markup=MAIN_MENU)
            return
        data["notify_time"] = t.strftime("%H:%M")

        # create profile
        profile = Profile(
            chat_id=chat_id,
            partner_name=data["partner_name"],
            partner_dob=data.get("partner_dob"),
            period_start=data["period_start"],
            period_end=data["period_end"],
            cycle_length=data["cycle_length"],
            notify_time=data["notify_time"],
            paused=False,
            last_sent_date=None
        )
        PROFILES[chat_id] = profile
        ONBOARDING_STATE.pop(chat_id, None)

        await update.message.reply_text(
            "✅ Klart! Meny är aktiv.\n\n" + profile_summary(profile),
            reply_markup=MAIN_MENU
        )
        # show today view instantly
        await update.message.reply_text(build_today_message(profile), reply_markup=MAIN_MENU)
        return

# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in PROFILES:
        start_onboarding(chat_id)
        await onboarding_prompt(update, context)
    else:
        await update.message.reply_text("🧭 Meny aktiv.", reply_markup=MAIN_MENU)
        await update.message.reply_text(build_today_message(PROFILES[chat_id]), reply_markup=MAIN_MENU)

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🧪 daycue v{APP_VERSION} running", reply_markup=MAIN_MENU)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Kommandon:\n"
        "/start – onboarding / meny\n"
        "/version – kolla vilken kod som kör\n",
        reply_markup=MAIN_MENU
    )

# =========================
# Menu routing
# =========================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    # If user is in onboarding, treat all text as onboarding input
    if chat_id in ONBOARDING_STATE:
        await handle_onboarding_input(update, context)
        return

    # If no profile yet, start onboarding
    if chat_id not in PROFILES:
        start_onboarding(chat_id)
        await onboarding_prompt(update, context)
        return

    profile = PROFILES[chat_id]

    if txt == "🧭 Idag":
        await update.message.reply_text(build_today_message(profile), reply_markup=MAIN_MENU)
        return

    if txt == "🔮 Prognos":
        await update.message.reply_text(build_forecast_message(profile), reply_markup=MAIN_MENU)
        return

    if txt == "⚙️ Inställningar":
        await update.message.reply_text(profile_summary(profile), reply_markup=SETTINGS_MENU)
        return

    if txt == "⬅️ Tillbaka":
        await update.message.reply_text("🧭 Meny aktiv.", reply_markup=MAIN_MENU)
        return

    if txt == "⏸️ Pausa":
        profile.paused = True
        await update.message.reply_text("⏸️ Pausad. Inga notiser skickas.", reply_markup=MAIN_MENU)
        return

    if txt == "▶️ Starta":
        profile.paused = False
        await update.message.reply_text("▶️ Startad igen. Notiser aktiva.", reply_markup=MAIN_MENU)
        return

    if txt == "🔔 Skicka nu":
        await send_daily_ping(context.application, profile, force=True)
        await update.message.reply_text("✅ Skickade en testnotis nu.", reply_markup=MAIN_MENU)
        return

    if txt == "✏️ Ändra cykeldata":
        # reuse onboarding steps 3-5 quickly
        ONBOARDING_STATE[chat_id] = {"step": 3, "data": {
            "partner_name": profile.partner_name,
            "partner_dob": profile.partner_dob,
        }}
        await update.message.reply_text("Okej – uppdatera cykeldata.", reply_markup=MAIN_MENU)
        await onboarding_prompt(update, context)
        return

    if txt == "🕒 Ändra notistid":
        ONBOARDING_STATE[chat_id] = {"step": 6, "data": {
            "partner_name": profile.partner_name,
            "partner_dob": profile.partner_dob,
            "period_start": profile.period_start,
            "period_end": profile.period_end,
            "cycle_length": profile.cycle_length,
        }}
        await update.message.reply_text("Okej – skriv ny notistid.", reply_markup=MAIN_MENU)
        await onboarding_prompt(update, context)
        return

    # Fallback: show menu
    await update.message.reply_text("Jag hänger med 👀 Välj i menyn.", reply_markup=MAIN_MENU)

# =========================
# Notifications loop
# =========================
async def send_daily_ping(app: Application, profile: Profile, force: bool = False):
    if profile.paused and not force:
        return

    now = datetime.now(TZ)
    today_str = now.date().isoformat()

    if not force and profile.last_sent_date == today_str:
        return

    msg = "🔔 Dagens cue\n\n" + build_today_message(profile)
    try:
        await app.bot.send_message(chat_id=profile.chat_id, text=msg, reply_markup=MAIN_MENU)
        if not force:
            profile.last_sent_date = today_str
    except Exception as e:
        # keep running even if one chat fails
        print(f"send_message failed for {profile.chat_id}: {e}")

async def notifier_loop(app: Application):
    print("BOOT: notifier loop started")
    while True:
        try:
            now = datetime.now(TZ)
            hhmm = now.strftime("%H:%M")

            for profile in list(PROFILES.values()):
                if profile.paused:
                    continue
                # send when time matches
                if profile.notify_time == hhmm:
                    await send_daily_ping(app, profile, force=False)

        except Exception as e:
            print(f"notifier_loop error: {e}")

        # tick each 30s to avoid missing minute boundary
        await asyncio.sleep(30)

# =========================
# Main
# =========================
async def post_init(app: Application):
    # start loop after app is initialized
    app.create_task(notifier_loop(app))
    print("BOOT: starting bot.py")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # All normal messages go through menu_router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
