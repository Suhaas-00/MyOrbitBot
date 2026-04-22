import os
import re
import json
import hashlib
import asyncio
from datetime import datetime, timedelta

from flask import Flask, request
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from groq import Groq
import dateparser

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ================= CONFIG =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

TIMEZONE = "Asia/Kolkata"
CALENDAR_ID = "primary"
SCOPES = ['https://www.googleapis.com/auth/calendar']

FALLBACK_START = 20
FALLBACK_END = 21

# Validate env
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN not set")
if not GROQ_API_KEY:
    raise ValueError("❌ GROQ_API_KEY not set")

client = Groq(api_key=GROQ_API_KEY)

app = Flask(__name__)

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()


# ================= CLEAN =================
def clean_text(text):
    return re.sub(r'https?://\S+', '', text)


def extract_link(text):
    url = re.search(r'(https?://\S+)', text)
    return url.group(0) if url else ""


# ================= AI PARSE =================
def ai_parse(text):
    prompt = f"""
Extract event details from this text:

{text}

Return ONLY JSON:
{{
  "title": "...",
  "start": "ISO datetime",
  "end": "ISO datetime"
}}
"""

    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}]
        )

        content = response.choices[0].message.content.strip()

        # Clean markdown if present
        content = content.replace("```json", "").replace("```", "").strip()

        data = json.loads(content)

        return (
            data.get("title", "Event"),
            datetime.fromisoformat(data["start"]),
            datetime.fromisoformat(data["end"])
        )
    except Exception as e:
        print("⚠️ AI parse failed:", e)
        return None


# ================= FALLBACK =================
def fallback_parse(text):
    dt = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future"})
    if dt:
        return text, dt, dt + timedelta(hours=1)
    return None


def parse_event(text):
    cleaned = clean_text(text)

    # 1️⃣ Try AI
    result = ai_parse(cleaned)
    if result:
        return result

    # 2️⃣ Try dateparser
    result = fallback_parse(cleaned)
    if result:
        return result

    # 3️⃣ Final fallback
    now = datetime.now()
    return (
        text,
        now.replace(hour=FALLBACK_START, minute=0),
        now.replace(hour=FALLBACK_END, minute=0)
    )


# ================= GOOGLE CALENDAR =================
def create_event(title, link, start, end, uid):
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    service = build('calendar', 'v3', credentials=creds)

    # Duplicate prevention
    existing = service.events().list(
        calendarId=CALENDAR_ID,
        q=uid
    ).execute().get('items', [])

    if existing:
        return "⚠️ Duplicate skipped"

    event = {
        'summary': title,
        'description': link + f"\nID:{uid}",
        'start': {
            'dateTime': start.isoformat(),
            'timeZone': TIMEZONE
        },
        'end': {
            'dateTime': end.isoformat(),
            'timeZone': TIMEZONE
        },
    }

    service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

    return f"✅ Scheduled: {title}\n🕒 {start} → {end}"


# ================= TELEGRAM HANDLER =================
async def handle(update: Update, context):
    try:
        text = update.message.text

        title, start, end = parse_event(text)
        link = extract_link(text)

        uid = hashlib.md5(text.encode()).hexdigest()

        result = create_event(title, link, start, end, uid)

        await update.message.reply_text(result)

    except Exception as e:
        print("❌ Handler error:", e)
        await update.message.reply_text("❌ Error processing request")


telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
)


# ================= WEBHOOK =================
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        asyncio.run(telegram_app.process_update(update))
        return "ok"
    except Exception as e:
        print("❌ Webhook error:", e)
        return "error", 500


@app.route("/")
def home():
    return "Bot is running 🚀"


# ================= START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
