print("BOOT: bot.py v0.9.2 - 2025-12-13", flush=True)

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

# call this once on startup:
threading.Thread(target=start_health_server, daemon=True).start()


import os
import sys
import re
import json
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Config
# =========================
VERSION = "0.9.2"
TZ = ZoneInfo("Europe/Stockholm")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing (set it as a Fly secret)")
    sys.exit(1)

# In-memory storage (MVP)
PROFILES: dict[int, dict] = {}        # chat_id -> profile dict
ONBOARDING: dict[int, dict] = {}      # chat_id -> {step:int, temp:dict}
LAST_SENT: dict[int, str] = {}        # chat_id -> YYYY-MM-DD last daily sent
JOBS_STARTED: set[int] = set()        # chat_ids that have job loop attached


# =========================
# UI helpers
# =========================
def menu_markup() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📅 Today"), KeyboardButton("🔮 Forecast")],
        [KeyboardButton("🔔 Send now"), KeyboardButton("📊 Status")],
        [KeyboardButton("⏸ Pause"), KeyboardButton("▶️ Resume")],
        [KeyboardButton("♻️ Reset"), KeyboardButton("⚙️ Settings")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def send_text(update: Update, text: str, context: ContextTypes.DEFAULT_TYPE):
    # Always attach the menu so it never “disappears”
    await update.effective_chat.send_message(text=text, reply_markup=menu_markup())


def parse_date(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def parse_time(s: str) -> dt.time | None:
    try:
        return dt.datetime.strptime(s.strip(), "%H:%M").time()
    except Exception:
        return None


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def bar(value_1_to_5: int) -> str:
    v = clamp(value_1_to_5, 1, 5)
    return "▰" * v + "▱" * (5 - v)


# =========================
# Cycle logic (simple MVP)
# =========================
def cycle_day(profile: dict, today: dt.date) -> int:
    start: dt.date = profile["period_start"]
    length: int = profile["cycle_length"]
    delta = (today - start).days
    return (delta % length) + 1


def phase_for(day: int, length: int) -> str:
    # MVP ranges, reasonable default:
    # Menstrual: 1-5
    # Follicular: 6-13
    # Ovulatory: 14-17
    # Luteal: 18-end
    if day <= 5:
        return "Menstrual"
    if 6 <= day <= 13:
        return "Follicular"
    if 14 <= day <= 17:
        return "Ovulatory"
    return "Luteal"


def phase_emoji(phase: str) -> str:
    return {
        "Menstrual": "🩸",
        "Follicular": "🌱",
        "Ovulatory": "🔥",
        "Luteal": "🌙",
    }.get(phase, "✨")


def phase_payload(phase: str) -> dict:
    # Values are 1-5 for easy “game HUD” bars
    if phase == "Menstrual":
        return {
            "prognosis": "Low energy window",
            "stats": {
                "Mood stability": (2, "🎭"),
                "Social drive": (2, "🗣️"),
                "Emotional needs": (4, "❤️"),
                "Anxiety": (3, "🔥"),
                "Irritability": (3, "💢"),
                "Cravings": (5, "🍫"),
                "Sexual drive": (2, "💕"),
                "Cognitive focus": (2, "🧠"),
            },
            "actions": [
                "🤝 Keep plans light. Offer help + warmth.",
                "🫶 Ask: comfort or space?",
                "🍲 Food: soup, tea, chocolate, cozy dinner.",
                "🛌 Protect sleep and downtime.",
            ],
        }
    if phase == "Follicular":
        return {
            "prognosis": "Energy rising + optimistic tone",
            "stats": {
                "Mood stability": (4, "🎭"),
                "Social drive": (4, "🗣️"),
                "Emotional needs": (3, "❤️"),
                "Anxiety": (2, "🔥"),
                "Irritability": (2, "💢"),
                "Cravings": (2, "🍫"),
                "Sexual drive": (3, "💕"),
                "Cognitive focus": (4, "🧠"),
            },
            "actions": [
                "🎯 Plan active stuff: walk, gym, mini-adventure.",
                "🗓️ Great for decisions + planning.",
                "🌿 Food: fresh, protein, crunchy snacks.",
                "😄 Keep it playful. Encourage new experiences.",
            ],
        }
    if phase == "Ovulatory":
        return {
            "prognosis": "Peak charisma + connection window",
            "stats": {
                "Mood stability": (5, "🎭"),
                "Social drive": (5, "🗣️"),
                "Emotional needs": (4, "❤️"),
                "Anxiety": (1, "🔥"),
                "Irritability": (1, "💢"),
                "Cravings": (2, "🍫"),
                "Sexual drive": (5, "💕"),
                "Cognitive focus": (4, "🧠"),
            },
            "actions": [
                "💬 Deep talk + appreciation hits hard (in a good way).",
                "🌇 Date night energy. Compliments = critical hit.",
                "📸 Social activities: friends, events, fun plans.",
                "🔥 Intimacy & closeness are boosted today.",
            ],
        }
    # Luteal
    return {
        "prognosis": "Sensitivity rising - protect calm",
        "stats": {
            "Mood stability": (2, "🎭"),
            "Social drive": (2, "🗣️"),
            "Emotional needs": (4, "❤️"),
            "Anxiety": (3, "🔥"),
            "Irritability": (4, "💢"),
            "Cravings": (4, "🍫"),
            "Sexual drive": (3, "💕"),
            "Cognitive focus": (2, "🧠"),
        },
        "actions": [
            "🧘 Reduce friction: fewer decisions, more clarity.",
            "🧩 Be steady. Don’t debate small stuff.",
            "🍞 Food: comfort + magnesium (dark choc, nuts).",
            "🧊 If conflict starts: pause, validate, soften tone.",
        ],
    }


def build_today_card(profile: dict, today: dt.date) -> str:
    day = cycle_day(profile, today)
    length = profile["cycle_length"]
    phase = phase_for(day, length)
    p = phase_payload(phase)
    pe = phase_emoji(phase)

    lines = []
    lines.append(f"👋 Welcome back.")
    lines.append(f"🧑‍🤝‍🧑 Partner: {profile['partner_name']}")
    lines.append(f"{pe} Phase: {phase} (Day {day}/{length})")
    lines.append(f"🧭 Prognosis: {p['prognosis']}")
    lines.append("")
    lines.append("🎮 Stats (HUD)")
    for k, (v, em) in p["stats"].items():
        lines.append(f"{em} {k}: {bar(v)}")
    lines.append("")
    lines.append("✅ Recommended actions")
    for a in p["actions"]:
        lines.append(f"- {a}")
    return "\n".join(lines)


# =========================
# Notifications (stable MVP)
# =========================
async def ensure_job_loop(app, chat_id: int):
    # Attach a repeating loop once per chat
    if chat_id in JOBS_STARTED:
        return

    app.job_queue.run_repeating(
        check_and_send_daily,
        interval=30,   # every 30s
        first=5,
        data={"chat_id": chat_id},
        name=f"daily-loop-{chat_id}",
    )
    JOBS_STARTED.add(chat_id)


async def check_and_send_daily(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    profile = PROFILES.get(chat_id)
    if not profile:
        return
    if profile.get("paused"):
        return

    now = dt.datetime.now(TZ)
    today = now.date()

    # Match user's HH:MM in Stockholm time
    notify_time: dt.time = profile["notify_time"]
    if now.strftime("%H:%M") != notify_time.strftime("%H:%M"):
        return

    # Avoid duplicates same day
    key = today.isoformat()
    if LAST_SENT.get(chat_id) == key:
        return

    text = build_today_card(profile, today)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=menu_markup())
        LAST_SENT[chat_id] = key
    except Exception:
        # If Telegram fails, don't mark as sent
        pass


# =========================
# Commands
# =========================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(update, f"🏓 pong (v{VERSION})")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(update, f"🧩 Daycue bot version: v{VERSION}")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(update, "🧭 Menu ready. Pick an action below.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ONBOARDING.pop(chat_id, None)
    await send_text(update, "✅ Onboarding cancelled. Menu is active.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # If profile exists, show today + menu
    if chat_id in PROFILES:
        await ensure_job_loop(context.application, chat_id)
        today = dt.datetime.now(TZ).date()
        await send_text(update, build_today_card(PROFILES[chat_id], today))
        return

    # Start onboarding
    ONBOARDING[chat_id] = {"step": 1, "temp": {}}
    await send_text(update,
        "👋 Welcome. Quick onboarding.\n\n"
        "1/6 - Enter partner nickname (example: Anna)\n\n"
        "Tip: type /cancel anytime to exit."
    )


# =========================
# Menu actions
# =========================
async def handle_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    profile = PROFILES.get(chat_id)

    if text == "♻️ Reset":
        PROFILES.pop(chat_id, None)
        ONBOARDING.pop(chat_id, None)
        LAST_SENT.pop(chat_id, None)
        await send_text(update, "♻️ Reset done. Type /start to onboard again.")
        return

    if not profile:
        await send_text(update, "🧪 No profile yet. Type /start to onboard.")
        return

    if text == "📅 Today":
        today = dt.datetime.now(TZ).date()
        await send_text(update, build_today_card(profile, today))
        return

    if text == "📊 Status":
        paused = "⏸ Paused" if profile.get("paused") else "▶️ Active"
        await send_text(update,
            "📊 Status\n"
            f"- Partner: {profile['partner_name']}\n"
            f"- Cycle length: {profile['cycle_length']} days\n"
            f"- Period start: {profile['period_start'].isoformat()}\n"
            f"- Notify time: {profile['notify_time'].strftime('%H:%M')} (Stockholm)\n"
            f"- Mode: {paused}"
        )
        return

    if text == "⏸ Pause":
        profile["paused"] = True
        await send_text(update, "⏸ Paused. No daily notifications will be sent.")
        return

    if text == "▶️ Resume":
        profile["paused"] = False
        await ensure_job_loop(context.application, chat_id)
        await send_text(update, "▶️ Resumed. Daily notifications are active.")
        return

    if text == "🔔 Send now":
        today = dt.datetime.now(TZ).date()
        await send_text(update, "🔔 Sending now…")
        await send_text(update, build_today_card(profile, today))
        return

    if text == "🔮 Forecast":
        # Simple 4-day outlook (MVP)
        today = dt.datetime.now(TZ).date()
        lines = ["🔮 Forecast (next 4 days)"]
        for i in range(0, 4):
            d = today + dt.timedelta(days=i)
            daynum = cycle_day(profile, d)
            ph = phase_for(daynum, profile["cycle_length"])
            lines.append(f"- {d.isoformat()} → {phase_emoji(ph)} {ph} (Day {daynum})")
        await send_text(update, "\n".join(lines))
        return

    if text == "⚙️ Settings":
        await send_text(update,
            "⚙️ Settings (MVP)\n"
            "- To change data: use /start (re-onboard) or ♻️ Reset.\n"
            "- To stop onboarding: /cancel"
        )
        return

    # Unknown button text
    await send_text(update, "🤖 I didn’t recognize that button. Try /menu.")


# =========================
# Onboarding handler
# =========================
async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    chat_id = update.effective_chat.id
    state = ONBOARDING.get(chat_id)
    if not state:
        return

    step = state["step"]
    temp = state["temp"]

    # Step 1: nickname
    if step == 1:
        name = user_text.strip()
        if len(name) < 2:
            await send_text(update, "❌ Nickname too short. Try again (example: Anna).")
            return
        temp["partner_name"] = name
        state["step"] = 2
        await send_text(update, "2/6 - Partner DOB (YYYY-MM-DD) or type 'skip'")
        return

    # Step 2: dob or skip
    if step == 2:
        if user_text.strip().lower() == "skip":
            temp["partner_dob"] = None
        else:
            dob = parse_date(user_text)
            if not dob:
                await send_text(update, "❌ Invalid date. Use YYYY-MM-DD or type 'skip'")
                return
            temp["partner_dob"] = dob.isoformat()
        state["step"] = 3
        await send_text(update, "3/6 - Last period START date (YYYY-MM-DD)")
        return

    # Step 3: period start
    if step == 3:
        d = parse_date(user_text)
        if not d:
            await send_text(update, "❌ Invalid date. Use YYYY-MM-DD.")
            return
        temp["period_start"] = d
        state["step"] = 4
        await send_text(update, "4/6 - Last period END date (YYYY-MM-DD)")
        return

    # Step 4: period end
    if step == 4:
        d = parse_date(user_text)
        if not d:
            await send_text(update, "❌ Invalid date. Use YYYY-MM-DD.")
            return
        if d < temp["period_start"]:
            await send_text(update, "❌ End date can’t be before start date. Try again (YYYY-MM-DD).")
            return
        temp["period_end"] = d
        state["step"] = 5
        await send_text(update, "5/6 - Cycle length in days (21-35). Example: 28")
        return

    # Step 5: cycle length
    if step == 5:
        m = re.match(r"^\d+$", user_text.strip())
        if not m:
            await send_text(update, "❌ Enter a number (21-35). Example: 28")
            return
        length = int(user_text.strip())
        if length < 21 or length > 35:
            await send_text(update, "❌ Cycle length must be 21-35. Try again.")
            return
        temp["cycle_length"] = length
        state["step"] = 6
        await send_text(update, "6/6 - Daily notification time (HH:MM). Example: 09:00")
        return

    # Step 6: notify time
    if step == 6:
        t = parse_time(user_text)
        if not t:
            await send_text(update, "❌ Invalid time. Use HH:MM. Example: 09:00")
            return

        # Save profile
        profile = {
            "partner_name": temp["partner_name"],
            "partner_dob": temp.get("partner_dob"),
            "period_start": temp["period_start"],
            "period_end": temp["period_end"],
            "cycle_length": temp["cycle_length"],
            "notify_time": t,
            "paused": False,
        }
        PROFILES[chat_id] = profile
        ONBOARDING.pop(chat_id, None)

        await ensure_job_loop(context.application, chat_id)

        await send_text(update,
            "🎉 Setup complete!\n"
            "✅ Menu is active below.\n"
            "🔔 Daily notifications will arrive at your chosen time (Stockholm).\n\n"
            "Tip: press 🔔 Send now to test instantly."
        )
        today = dt.datetime.now(TZ).date()
        await send_text(update, build_today_card(profile, today))
        return


# =========================
# Router (very important)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # 1) Global commands should always work (Telegram sends commands to CommandHandlers too,
    # but in case user typed like normal text "/menu" etc.)
    if user_text in ("/menu",):
        await cmd_menu(update, context)
        return
    if user_text in ("/cancel",):
        await cmd_cancel(update, context)
        return
    if user_text in ("/start",):
        await cmd_start(update, context)
        return
    if user_text in ("/ping",):
        await cmd_ping(update, context)
        return
    if user_text in ("/version",):
        await cmd_version(update, context)
        return

    # 2) If onboarding active, handle onboarding BUT do not "kill" menu forever:
    # the menu is still attached to replies, and /cancel works.
    if chat_id in ONBOARDING:
        await handle_onboarding(update, context, user_text)
        return

    # 3) Otherwise treat as menu press
    await handle_menu_action(update, context, user_text)


def main():
    print("BOOT: starting bot.py")
    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("version", cmd_version))

    # Text router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # Also handle commands typed as plain text in some clients
    app.add_handler(MessageHandler(filters.TEXT, on_text))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
