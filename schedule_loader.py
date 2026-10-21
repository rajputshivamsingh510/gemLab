"""Read an uploaded schedule file and check every row (consent, chat id, date, message)."""
from __future__ import annotations

import io
import re
from datetime import datetime

import pandas as pd

from reminder_store import local_tz, make_reminder_id, to_iso

REQUIRED = ["name", "telegram_chat_id", "reminder_at", "message", "consent"]
TRUE_VALUES = {"true", "yes", "y", "1"}
CHAT_ID_RE = re.compile(r"^(-?\d{5,20}|@[A-Za-z0-9_]{4,})$")
ISO_START = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _parse_time(value):
    value = str(value).strip()
    if not value:
        return pd.NaT
    # ISO dates (2026-10-11 ...) must NOT be read day-first; 22/10/2026 style is day-first.
    return pd.to_datetime(value, errors="coerce", dayfirst=not ISO_START.match(value))


def parse_schedule(data: bytes, filename: str):
    """Returns {"rows": [...], "counts": {...}}. Row state: ready | past | no_consent | invalid."""
    name = filename.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    elif name.endswith(".xlsx"):
        df = pd.read_excel(io.BytesIO(data), dtype=str).fillna("")
    else:
        raise ValueError("Please upload a .csv or .xlsx file.")
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("Missing columns: " + ", ".join(missing))

    now = datetime.now(local_tz())
    rows, counts = [], {"total": 0, "ready": 0, "past": 0, "no_consent": 0, "invalid": 0}
    for i, r in enumerate(df.to_dict("records"), start=2):  # start=2 -> spreadsheet line numbers
        who, chat, msg = str(r["name"]).strip(), str(r["telegram_chat_id"]).strip(), str(r["message"]).strip()
        consent = str(r["consent"]).strip().lower() in TRUE_VALUES
        ts = _parse_time(r["reminder_at"])
        problems = []
        if not who:
            problems.append("name missing")
        if not CHAT_ID_RE.match(chat):
            problems.append("chat id is not a valid Telegram id")
        if pd.isna(ts):
            problems.append("date/time not understood")
        if not msg:
            problems.append("message empty")
        elif len(msg) > 4096:
            problems.append("message over 4096 characters")

        iso, note = None, ""
        if problems:
            state, note = "invalid", "; ".join(problems)
        else:
            iso = to_iso(ts)
            if not consent:
                state, note = "no_consent", "consent is 0 - will not be scheduled"
            elif datetime.fromisoformat(iso) <= now:
                state, note = "past", "time already passed - sends immediately (skipped if over 24h old)"
            else:
                state = "ready"
        counts["total"] += 1
        counts[state] += 1
        rows.append({
            "line": i, "name": who, "telegram_chat_id": chat, "message": msg, "consent": consent,
            "reminder_at": iso or str(r["reminder_at"]).strip(), "iso": iso, "state": state, "note": note,
            "id": make_reminder_id(chat, iso, msg) if iso else None,
        })
    return {"rows": rows, "counts": counts}
