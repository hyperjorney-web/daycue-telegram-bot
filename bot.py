import os
import sys
import asyncio
from dataclasses import dataclass
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
APP_VERSION = "0.9.1"
TZ = ZoneInfo("Europe/Stockholm")
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
    period_start: str
    period_end: str
    cycle_length: int
    notify_time: str
    paused: bool = False
    last_sent_date: str | None = None

PROFILES: dict[int, Profile] = {}
ONBOARDING_STATE: dict[int, dict] = {}  # chat_id -> {"step": int, "data": {...}}

# =========================
# UI: Persistent menu
# =========================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["🧭 Idag", "🔮 Prognos"],
            ["⚙️ Inställningar", "🔔 Skicka nu"],
            ["⏸️ Pausa", "▶️ Starta"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,              # IMPORTANT
        input_field_placeholder="Välj i menyn eller skriv /menu"
    )

def settings_menu():
    return ReplyKeyboardMarkup(
        [
            ["✏️ Ändra cykeldata", "🕒 Ändra notistid"],
            ["⬅️ Tillbaka"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
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
    return (delta % profile.cycle_length) + 1

def phase_for(profile: Profile, day_no: int) -> str:
    L = profile.cycle_length
    menstrual_end = max(4, round(L * 0.18))
    ovulation_start = round(L * 0.45)
    ovulation_end = min(L, ovulation_start + 2)
    follicular_end = ovulation_start - 1

    if day_no <= menstrual_end:
        return "🩸 Menstruation"
    if day_no <= follicular_end:
        return "🌱 Follikulär"
    if ovulation_start <= day_no <= ovulation_end:
        return "🔥 Ägglossning"
    return "🌙 Luteal"

def score_bar(label: str, emoji: str, value_0_100: int) -> str:
    blocks = int(round(value_0_100 / 10))
    bar = "█" * blocks + "░" * (10 - blocks)
    return f"{emoji} {label}: {bar} {value_0_100}/100"

def stats_for_phase(phase: str) -> dict:
    if "Menstruation" in phase:
        return {"Mood": ("🎭", 45), "Social": ("🗣️", 35), "Needs": ("❤️", 75), "Anxiety": ("🔥", 55),
                "Irritability": ("💢", 60), "Cravings": ("🍩", 80), "Libido": ("💕", 30), "Focus": ("🧠", 40),
                "tips": ["🫖 Värme + vila", "🧘 Lugn rörelse", "🤝 Ta lead på vardag"]}
    if "Follikulär" in phase:
        return {"Mood": ("🎭", 75), "Social": ("🗣️", 75), "Needs": ("❤️", 55), "Anxiety": ("🔥", 30),
                "Irritability": ("💢", 25), "Cravings": ("🍩", 30), "Libido": ("💕", 55), "Focus": ("🧠", 80),
                "tips": ["🎯 Planera + bygg momentum", "🏃 Aktivitet ihop", "💬 Pepp + framtidssnack"]}
    if "Ägglossning" in phase:
        return {"Mood": ("🎭", 85), "Social": ("🗣️", 90), "Needs": ("❤️", 60), "Anxiety": ("🔥", 20),
                "Irritability": ("💢", 15), "Cravings": ("🍩", 35), "Libido": ("💕", 90), "Focus": ("🧠", 70),
                "tips": ["🌹 Komplimanger (specifikt!)", "🍽️ Date / socialt", "🔥 Närhet + lekfullhet"]}
    return {"Mood": ("🎭", 40), "Social": ("🗣️", 40), "Needs": ("❤️", 80), "Anxiety": ("🔥", 60),
            "Irritability": ("💢", 75), "Cravings": ("🍩", 85), "Libido": ("💕", 50), "Focus": ("🧠", 35),
            "tips": ["🧩 Förenkla vardagen", "🥣 Comfort + snäll ton", "🛡️ Undvik onödiga konflikter"]}

def build_today_message(profile: Profile) -> str:
    today = datetime.now(TZ).date()
    d = cycle_day_for(profile, today)
    phase = phase_for(profile, d)
    s = stats_for_phase(phase)

    lines = [
        f"📅 Idag: {today.isoformat()} | Dag {d}/{profile.cycle_length}",
        f"🧬 Fas: {phase}",
        "",
        score_bar("Mood", *s["Mood"]),
        score_bar("Social Drive", *s["Social"]),
        score_bar("Emotional Needs", *s["Needs"]),
        score_bar("Anxiety", *s["Anxiety"]),
        score_bar("Irritability", *s["Irritability"]),
        score_bar("Cravings", *s["Cravings"]),
        score_bar("Sexual Drive", *s["Libido"]),
        score_bar("Focus", *s["Focus"]),
        "",
        "🎒 Tips:",
        *[f"• {t}" for t in s["tips"]],
    ]
    return "\n".join(lines)

def build_forecast_message(profile: Profile) -> str:
    base = datetime.now(TZ).date()
    out = ["🔮 Prognos (7 dagar):"]
    for i in range(7):
        day = base + timedelta(days=i)
        d = cycle_day_for(profile, day)
        out.append(f"• {day.isoformat()} — Dag {d}: {phase_for(profile, d)}")
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
# Onboarding
# =========================
def start_onboarding(chat_id: int):
    ONBOARDING_STATE[chat_id] = {"step": 1, "data": {}}

async def onboarding_prompt(update: Update):
    chat_id = update.effective_chat.id
    st = ONBOARDING_STATE.get(chat_id)
    if not st:
        start_onboarding(chat_id)
        st = ONBOARDING_STATE[chat_id]

    step = st["step"]
    prompts = {
        1: "1/6 - Enter partner nickname (example: Anna)",
        2: "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'",
        3: "3/6 - Last period START date (YYYY-MM-DD)",
        4: "4/6 - Last period END date (YYYY-MM-DD)",
        5: "5/6 - Cycle length in days (21-35). Example: 28",
        6: "6/6 - Daily notification time (HH:MM). Example: 09:00",
    }
    if step == 1:
        await update.message.reply_text(
            f"Welcome. Quick onboarding. 🧪 v{APP_VERSION}\n\n{prompts[1]}",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(prompts[step], reply_markup=main_menu())

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
        data["partner_name"] = txt if txt else "Partner"
        st["step"] = 2
        await onboarding_prompt(update)
        return

    if step == 2:
        if txt.lower() == "skip":
            data["partner_dob"] = None
        else:
            d = parse_yyyy_mm_dd(txt)
            if not d:
                await update.message.reply_text("Fel format. Ex: 1987-08-16 eller 'skip'", reply_markup=main_menu())
                return
            data["partner_dob"] = d.isoformat()
        st["step"] = 3
        await onboarding_prompt(update)
        return

    if step == 3:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Fel format. Ex: 2025-12-09", reply_markup=main_menu())
            return
        data["period_start"] = d.isoformat()
        st["step"] = 4
        await onboarding_prompt(update)
        return

    if step == 4:
        d = parse_yyyy_mm_dd(txt)
        if not d:
            await update.message.reply_text("Fel format. Ex: 2025-12-13", reply_markup=main_menu())
            return
        data["period_end"] = d.isoformat()
        st["step"] = 5
        await onboarding_prompt(update)
        return

    if step == 5:
        try:
            L = int(txt)
        except Exception:
            await update.message.reply_text("Skriv en siffra 21-35. Ex: 28", reply_markup=main_menu())
            return
        if L < 21 or L > 35:
            await update.message.reply_text("Cykellängd måste vara 21-35.", reply_markup=main_menu())
            return
        data["cycle_length"] = L
        st["step"] = 6
        await onboarding_prompt(update)
        return

    if step == 6:
        t = parse_hh_mm(txt)
        if not t:
            await update.message.reply_text("Fel format. Ex: 09:00", reply_markup=main_menu())
            return
        data["notify_time"] = t.strftime("%H:%M")

        PROFILES[chat_id] = Profile(
            chat_id=chat_id,
            partner_name=data["partner_name"],
            partner_dob=data.get("partner_dob"),
            period_start=data["period_start"],
            period_end=data["period_end"],
            cycle_length=data["cycle_length"],
            notify_time=data["notify_time"],
            paused=False,
            last_sent_date=None,
        )
        ONBOARDING_STATE.pop(chat_id, None)

        # Double-confirm menu (some clients hide it unless re-sent)
        await update.message.reply_text("✅ Setup complete. Menu is now active.", reply_markup=main_menu())
        await update.message.reply_text(profile_summary(PROFILES[chat_id]), reply_markup=main_menu())
        await update.message.reply_text(build_today_message(PROFILES[chat_id]), reply_markup=main_menu())
        return

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
        await app.bot.send_message(chat_id=profile.chat_id, text=msg, reply_markup=main_menu())
        if not force:
            profile.last_sent_date = today_str
    except Exception as e:
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
                if profile.notify_time == hhmm:
                    await send_daily_ping(app, profile, force=False)
        except Exception as e:
            print(f"notifier_loop error: {e}")
        await asyncio.sleep(30)

# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🧭 Menu active. If you don’t see it, type /menu.", reply_markup=main_menu())
    if chat_id not in PROFILES:
        start_onboarding(chat_id)
        await onboarding_prompt(update)
    else:
        await update.message.reply_text(build_today_message(PROFILES[chat_id]), reply_markup=main_menu())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧭 Here’s your menu.", reply_markup=main_menu())

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in PROFILES:
        await update.message.reply_text("Du behöver onboarding först. Skriv /start", reply_markup=main_menu())
        return
    await send_daily_ping(context.application, PROFILES[chat_id], force=True)
    await update.message.reply_text("✅ Sent a test notification now.", reply_markup=main_menu())

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🧪 daycue v{APP_VERSION} running", reply_markup=main_menu())

# =========================
# Menu routing
# =========================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    # onboarding mode
    if chat_id in ONBOARDING_STATE:
        await handle_onboarding_input(update, context)
        return

    # no profile yet => start onboarding
    if chat_id not in PROFILES:
        start_onboarding(chat_id)
        await onboarding_prompt(update)
        return

    profile = PROFILES[chat_id]

    if txt == "🧭 Idag":
        await update.message.reply_text(build_today_message(profile), reply_markup=main_menu())
        return

    if txt == "🔮 Prognos":
        await update.message.reply_text(build_forecast_message(profile), reply_markup=main_menu())
        return

    if txt == "⚙️ Inställningar":
        await update.message.reply_text(profile_summary(profile), reply_markup=settings_menu())
        return

    if txt == "⬅️ Tillbaka":
        await update.message.reply_text("🧭 Menu active.", reply_markup=main_menu())
        return

    if txt == "⏸️ Pausa":
        profile.paused = True
        await update.message.reply_text("⏸️ Paused.", reply_markup=main_menu())
        return

    if txt == "▶️ Starta":
        profile.paused = False
        await update.message.reply_text("▶️ Resumed.", reply_markup=main_menu())
        return

    if txt == "🔔 Skicka nu":
        await send_daily_ping(context.application, profile, force=True)
        await update.message.reply_text("✅ Sent now.", reply_markup=main_menu())
        return

    if txt == "✏️ Ändra cykeldata":
        ONBOARDING_STATE[chat_id] = {"step": 3, "data": {
            "partner_name": profile.partner_name,
            "partner_dob": profile.partner_dob,
        }}
        await update.message.reply_text("Okej – uppdatera cykeldata.", reply_markup=main_menu())
        await onboarding_prompt(update)
        return

    if txt == "🕒 Ändra notistid":
        ONBOARDING_STATE[chat_id] = {"step": 6, "data": {
            "partner_name": profile.partner_name,
            "partner_dob": profile.partner_dob,
            "period_start": profile.period_start,
            "period_end": profile.period_end,
            "cycle_length": profile.cycle_length,
        }}
        await update.message.reply_text("Okej – skriv ny notistid.", reply_markup=main_menu())
        await onboarding_prompt(update)
        return

    # fallback: always re-send menu so it never disappears
    await update.message.reply_text("🧭 Välj i menyn (eller skriv /menu).", reply_markup=main_menu())

# =========================
# Main
# =========================
async def post_init(app: Application):
    app.create_task(notifier_loop(app))
    print("BOOT: starting bot.py")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("version", version_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
