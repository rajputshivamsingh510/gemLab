"""Print chat IDs from recent messages sent to your bot (after you press /start)."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Configure it in .env first.")
try:
    response = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        timeout=20,
    )
    data = response.json()
except requests.RequestException:
    raise SystemExit("Could not connect to Telegram. Check your internet connection.") from None
if not response.ok or not data.get("ok"):
    raise SystemExit(f"Telegram API error: {data.get('description', 'Request failed')}")
chats = {}
for update in data.get("result", []):
    message = update.get("message") or update.get("edited_message") or update.get("my_chat_member")
    if not message:
        continue
    chat = message.get("chat") or {}
    if chat.get("id") is not None:
        chats[str(chat["id"])] = {
            "chat_id": str(chat["id"]),
            "type": chat.get("type", "unknown"),
            "username": chat.get("username", ""),
            "first_name": chat.get("first_name", ""),
        }
if not chats:
    print("No chats found. Open your bot in Telegram, press Start/send /start, then run this script again.")
else:
    print("Recent chat IDs (keep these private if they identify users):")
    for chat in chats.values():
        print(f"chat_id={chat['chat_id']} | type={chat['type']} | username=@{chat['username']} | first_name={chat['first_name']}")
