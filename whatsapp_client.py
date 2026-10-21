
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()


def send_test_template(recipient: str) -> dict:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    version = os.getenv("WHATSAPP_API_VERSION", "").strip()

    if not token or not phone_id or not version:
        raise ValueError(
            "Configure the token, Phone Number ID, "
            "and API version in your local .env file."
        )

    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("The Graph API version is invalid.")

    # Enter the country code followed by the phone number.
    # Example format: 919876543210 (digits only).
    recipient = recipient.strip().replace(" ", "")
    if not re.fullmatch(r"\d{8,15}", recipient):
        raise ValueError(
            "Enter a valid international number using digits only."
        )

    url = (
        f"https://graph.facebook.com/{version}/"
        f"{phone_id}/messages"
    )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {"code": "en_US"},
        },
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException:
        raise RuntimeError(
            "Could not connect to WhatsApp Cloud API. "
            "Check your internet connection and try again."
        ) from None

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok:
        error = data.get("error", {})
        # Do not log or return request headers or credentials.
        message = error.get("message", "Unknown API error")
        code = error.get("code", "unavailable")
        raise RuntimeError(
            f"WhatsApp API rejected the request "
            f"(HTTP {response.status_code}, code {code}): "
            f"{message}"
        )

    messages = data.get("messages", [])
    if not messages or not messages[0].get("id"):
        raise RuntimeError(
            "The API response did not contain a message ID."
        )

    return {
        "accepted": True,
        "message_id": messages[0]["id"],
        "recipient": recipient,
    }
