"""Small official Telegram Bot API client for the development demo."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_telegram_message(chat_id: str, text: str) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing. Add it to your local .env file.")
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise ValueError("Telegram chat ID is required.")
    text = str(text).strip()
    if not text:
        raise ValueError("Message text cannot be empty.")
    if len(text) > 4096:
        raise ValueError("Telegram messages must be 4096 characters or fewer.")

    try:
        response = requests.post(
            API_BASE.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=20,
        )
    except requests.RequestException:
        raise RuntimeError("Could not connect to Telegram. Check your internet connection.") from None

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if not response.ok or not payload.get("ok"):
        # Avoid exposing the request URL, which contains the bot token.
        description = payload.get("description", "Telegram rejected the request.")
        error_code = payload.get("error_code", response.status_code)
        raise RuntimeError(f"Telegram API error ({error_code}): {description}")

    result = payload.get("result", {})
    return {
        "accepted": True,
        "message_id": result.get("message_id"),
        "chat_id": str(result.get("chat", {}).get("id", chat_id)),
        "date": result.get("date"),
    }
