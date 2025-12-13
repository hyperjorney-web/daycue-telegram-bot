import os
import json
import logging
from datetime import datetime, timedelta, time

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

# =========================
# CONFIG
# =========================
VERSION = "0.9.4"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

DATA_FILE = "data.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daycue")

# =========================
# STATES
# =========================
(
    NICK,
    DOB,
    START,
    END,
    CYCLE,
    NOTIFY,
) = range(6)

# =========================
# STORAGE
# =========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

DATA = load_data()

# =========================
# UI
# =========================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["📅 Today", "📊 Status"],
            ["🔔 Send now"],
            ["⏸ Pause", "▶ Resume"],
            ["♻ Reset"],
        ],
        resize_keyboard=True,
    )

async def send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
    )

# =========================
# LOGIC
# =========================
def cycle_day(profile):
    start = datetime.fromisoformat(profile["period_start"])
    today = datetime.utcnow()
    return (today - start).days + 1

def today_report(profile):
    day = cycle_day(profile)
    return (
        f"🌸 *Partner:* {profile['nickname']}\n"
        f"🩸 *Day {day}/{profile['cycle']} – Menstrual*\n\n"
        f"📊 *Stats*\n"
        f"😴 Energy: Low\n"
        f"💬 Social: Low\n"
        f"❤️ Needs: Comfort\n"
        f"🍫 Cravings: High\n\n"
        f"🫶 *Tips*\n"
        f"• Be gentle\n"
        f"• Warm food\n"
        f"• Ask before plans"
    )

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id in DATA:
        await send(update, context, "👋 Welcome back!")
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 *Welcome to Daycue*\n\n"
        "1/6 – Enter partner nickname (example: Anna)",
        parse_mode="Markdown",
    )
    return NICK

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update, context, f"🧩 Daycue version *v{VERSION}*")

# =========================
# ONBOARDING
# =========================
async def set_nick(update: Update, context):
    context.user_data["nickname"] = update.message.text.strip()
    await update.message.reply_text("2/6 – Partner DOB (YYYY-MM-DD) or type `skip`")
    return DOB

async def set_dob(update: Update, context):
    context.user_data["dob"] = update.message.text.strip()
    await update.message.reply_text("3/6 – Last period START date (YYYY-MM-DD)")
    return START

async def set_start(update: Update, context):
    context.user_data["period_start"] = update.message.text.strip()
    await update.message.reply_text("4/6 – Last period END date (YYYY-MM-DD)")
    return END

async def set_end(update: Update, context):
    context.user_data["period_end"] = update.message.text.strip()
    await update.message.reply_text("5/6 – Cycle length (21–35)")
    return CYCLE

async def set_cycle(update: Update, context):
    context.user_data["cycle"] = int(update.message.text.strip())
    await update.message.reply_text("6/6 – Daily notification time (HH:MM)")
    return NOTIFY

async def set_notify(update: Update, context):
    notify_time = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    DATA[chat_id] = {
        "nickname": context.user_data["nickname"],
        "dob": context.user_data["dob"],
        "period_start": context.user_data["period_start"],
        "period_end": context.user_data["period_end"],
        "cycle": context.user_data["cycle"],
        "notify": notify_time,
        "paused": False,
    }

    save_data(DATA)

    await send(update, context, "✅ Setup complete!")
    await send(update, context, today_report(DATA[chat_id]))
    return ConversationHandler.END

# =========================
# BUTTON HANDLERS
# =========================
async def on_menu(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id not in DATA:
        await send(update, context, "⚠ Please run /start first")
        return

    text = update.message.text

    if text == "📅 Today":
        await send(update, context, today_report(DATA[chat_id]))

    elif text == "🔔 Send now":
        await send(update, context, today_report(DATA[chat_id]))

    elif text == "♻ Reset":
        DATA.pop(chat_id, None)
        save_data(DATA)
        await update.message.reply_text("♻ Reset done. Type /start")

# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            NICK: [MessageHandler(filters.TEXT, set_nick)],
            DOB: [MessageHandler(filters.TEXT, set_dob)],
            START: [MessageHandler(filters.TEXT, set_start)],
            END: [MessageHandler(filters.TEXT, set_end)],
            CYCLE: [MessageHandler(filters.TEXT, set_cycle)],
            NOTIFY: [MessageHandler(filters.TEXT, set_notify)],
        },
        fallbacks=[],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(MessageHandler(filters.TEXT, on_menu))

    log.info("🚀 Daycue started")
    app.run_polling()

if __name__ == "__main__":
    main()
