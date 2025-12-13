import os
import sys

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing")
    sys.exit(1)

import os
user_state = {}  # chat_id -> data
from datetime import datetime, date
from dateutil.parser import isoparse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "daycue.db"

ASK_NAME, ASK_START_DATE, ASK_CYCLE_LEN, ASK_TIME = range(4)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            partner_name TEXT,
            period_start TEXT,
            cycle_length INTEGER,
            send_time TEXT,
            paused INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            day_number INTEGER,
            rating TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def upsert_user(chat_id, **fields):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,))
    exists = cur.fetchone()

    if not exists:
        conn.execute(
            "INSERT INTO users(chat_id, partner_name, period_start, cycle_length, send_time, paused) VALUES (?, ?, ?, ?, ?, 0)",
            (chat_id, fields.get("partner_name"), fields.get("period_start"),
             fields.get("cycle_length"), fields.get("send_time"))
        )
    else:
        sets = ", ".join([f"{k}=?" for k in fields.keys()])
        vals = list(fields.values()) + [chat_id]
        conn.execute(f"UPDATE users SET {sets} WHERE chat_id=?", vals)

    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, partner_name, period_start, cycle_length, send_time, paused FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "chat_id": row[0],
        "partner_name": row[1],
        "period_start": row[2],
        "cycle_length": row[3],
        "send_time": row[4],
        "paused": bool(row[5]),
    }

def list_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, partner_name, period_start, cycle_length, send_time, paused FROM users")
    rows = cur.fetchall()
    conn.close()
    return [{
        "chat_id": r[0],
        "partner_name": r[1],
        "period_start": r[2],
        "cycle_length": r[3],
        "send_time": r[4],
        "paused": bool(r[5]),
    } for r in rows]

def compute_cycle_day(start, length):
    s = datetime.strptime(start, "%Y-%m-%d").date()
    d = (date.today() - s).days
    return (d % length) + 1

def phase_for_day(day):
    if day <= 5: return "menstrual"
    if day <= 13: return "follicular"
    if day <= 16: return "ovulatory"
    return "luteal"

def today_message(phase):
    return {
        "menstrual": ("Low energy day",
                      "She may feel more tired or sensitive today.",
                      "Keep things calm. Offer warmth and don’t push decisions."),
        "follicular": ("Momentum day",
                       "Energy and optimism often rise in this phase.",
                       "Suggest something light together. Encouragement lands well."),
        "ovulatory": ("Connection day",
                      "Many feel more social and open to closeness now.",
                      "Prioritize quality time and sincere appreciation."),
        "luteal": ("Steady support day",
                   "Stress sensitivity may be higher today.",
                   "Be steady and kind. Avoid criticism and pressure.")
    }[phase]

def feedback_kb(day):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Helpful", callback_data=f"fb:yes:{day}"),
        InlineKeyboardButton("Neutral", callback_data=f"fb:mid:{day}"),
        InlineKeyboardButton("Not helpful", callback_data=f"fb:no:{day}")
    ]])

async def send_today(app, chat_id):
    user = get_user(chat_id)
    if not user or user["paused"]:
        return

    day = compute_cycle_day(user["period_start"], user["cycle_length"])
    phase = phase_for_day(day)
    label, context, action = today_message(phase)

    text = (
        f"Today – {label}\n"
        f"For {user['partner_name']}\n\n"
        f"{context}\n\n"
        f"What helps today:\n{action}\n\n"
        f"Cycle day {day}"
    )

    await app.bot.send_message(chat_id, text, reply_markup=feedback_kb(day))

async def scheduler_tick(app):
    now = datetime.now().strftime("%H:%M")
    for u in list_users():
        if not u["paused"] and u["send_time"] == now:
            await send_today(app, u["chat_id"])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Partner name?")
    return ASK_NAME

async def on_name(update, context):
    context.user_data["partner_name"] = update.message.text
    await update.message.reply_text("First day of last period (YYYY-MM-DD)?")
    return ASK_START_DATE

async def on_date(update, context):
    try:
        context.user_data["period_start"] = isoparse(update.message.text).date().strftime("%Y-%m-%d")
    except:
        await update.message.reply_text("Use YYYY-MM-DD")
        return ASK_START_DATE
    await update.message.reply_text("Cycle length? (default 28)")
    return ASK_CYCLE_LEN

async def on_len(update, context):
    try:
        context.user_data["cycle_length"] = int(update.message.text)
    except:
        context.user_data["cycle_length"] = 28
    await update.message.reply_text("Daily time? HH:MM")
    return ASK_TIME

async def on_time(update, context):
    context.user_data["send_time"] = update.message.text
    upsert_user(update.effective_chat.id, **context.user_data)
    await update.message.reply_text("Setup done. Use /today anytime.")
    return ConversationHandler.END

async def today(update, context):
    await send_today(context.application, update.effective_chat.id)

async def feedback(update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)

def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT, on_name)],
            ASK_START_DATE: [MessageHandler(filters.TEXT, on_date)],
            ASK_CYCLE_LEN: [MessageHandler(filters.TEXT, on_len)],
            ASK_TIME: [MessageHandler(filters.TEXT, on_time)],
        },
        fallbacks=[]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CallbackQueryHandler(feedback))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: scheduler_tick(app), "interval", minutes=1)
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main()
