"""Reminder Control Room: web UI + automatic sender in ONE process.

Run:  python server.py     then open http://127.0.0.1:8000
The sender thread checks for due reminders every few seconds, so there is no separate
scheduler to start. Keep this window open (and the PC awake) for messages to go out.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import io

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import reminder_store as rs
import scheduler  # reuses the tested send/claim logic (and its .env settings)
from schedule_loader import parse_schedule
from validator import validate_customers
from telegram_client import send_telegram_message
import whatsapp_client

BASE = Path(__file__).parent
POLL_SECONDS = max(2, int(os.getenv("SERVER_POLL_SECONDS", "5")))
MAX_UPLOAD = 20 * 1024 * 1024
log = logging.getLogger("control-room")
SENDER = {"last_tick": None, "error": None}
PENDING = {}  # token -> checked rows waiting for the Proceed click
VALIDATED = {}  # token -> validation report CSV bytes (for download)
_BOT = {"checked": False, "username": None}
_lock = threading.Lock()


def sender_loop(stop: threading.Event):
    while not stop.is_set():
        try:
            scheduler.process_due_once()
            SENDER["last_tick"], SENDER["error"] = time.time(), None
        except Exception as exc:  # keep the thread alive no matter what
            SENDER["error"] = str(exc)[:200]
            log.exception("sender loop error")
        stop.wait(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    rs.init_db()
    stuck = rs.recover_interrupted()
    if stuck:
        log.warning("%d reminder(s) were stuck in 'Sending'; marked Failed for review.", stuck)
    if scheduler.DRY_RUN:
        log.warning("*** DRY RUN IS ON: nothing will be sent. Set SCHEDULER_DRY_RUN=false in .env and restart. ***")
    stop = threading.Event()
    threading.Thread(target=sender_loop, args=(stop,), daemon=True, name="sender").start()
    yield
    stop.set()


app = FastAPI(lifespan=lifespan)


def _ts(iso):
    try:
        dt = datetime.fromisoformat(str(iso))
        return (dt if dt.tzinfo else dt.replace(tzinfo=rs.local_tz())).timestamp()
    except (TypeError, ValueError):
        return None


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/state")
def state():
    waiting, total = rs.waiting_reminders(300)
    for r in waiting:
        r["due_ts"] = _ts(r["reminder_at"])
        del r["id"]
    recent = rs.recent_outcomes(30)
    for r in recent:
        r["due_ts"], r["finished_ts"] = _ts(r["reminder_at"]), _ts(r["finished_at"])
        r["delay_s"] = round(r["finished_ts"] - r["due_ts"]) if r["due_ts"] and r["finished_ts"] else None
        del r["id"]
    last = SENDER["last_tick"]
    return {
        "now_ts": time.time(),
        "sender": {"dry_run": scheduler.DRY_RUN, "poll_seconds": POLL_SECONDS, "error": SENDER["error"],
                   "seconds_since_check": None if last is None else round(time.time() - last, 1)},
        "waiting": waiting, "waiting_total": total, "recent": recent,
        "today": rs.outcome_counts_today(),
    }


@app.post("/api/preview")
async def preview(request: Request):
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(400, "Empty file or file larger than 20 MB.")
    filename = unquote(request.headers.get("x-filename", "upload.csv"))
    try:
        result = parse_schedule(data, filename)
    except Exception as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Could not read this file.")
    token = secrets.token_urlsafe(8)
    with _lock:
        PENDING[token] = {"rows": result["rows"], "filename": filename}
        while len(PENDING) > 5:
            PENDING.pop(next(iter(PENDING)))
    shown = [{k: v for k, v in r.items() if k not in ("iso", "id")} for r in result["rows"][:500]]
    return {"token": token, "filename": filename, "counts": result["counts"], "rows": shown,
            "truncated": len(result["rows"]) > 500}


@app.post("/api/proceed")
async def proceed(request: Request):
    body = await request.json()
    with _lock:
        batch = PENDING.pop(body.get("token", ""), None)
    if batch is None:
        raise HTTPException(404, "This upload was already scheduled or has expired. Upload the file again.")
    keep = [r for r in batch["rows"] if r["state"] in ("ready", "past", "no_consent")]
    to_store = [{"name": r["name"], "telegram_chat_id": r["telegram_chat_id"], "reminder_at": r["iso"],
                 "message": r["message"], "consent": r["consent"]} for r in keep]
    new, archived = rs.add_reminders(to_store)
    schedulable = [r["id"] for r in keep if r["state"] in ("ready", "past")]
    approved = rs.approve_all(schedulable)
    return {"scheduled": approved, "duplicates_ignored": len(keep) - new, "saved_to_csv": archived,
            "no_consent": sum(r["state"] == "no_consent" for r in batch["rows"]),
            "invalid": sum(r["state"] == "invalid" for r in batch["rows"])}


@app.get("/api/all")
def all_records(status: str = "All", q: str = "", limit: int = 500):
    rows, total = rs.search_reminders(status, q, limit=max(1, min(limit, 500)))
    out = []
    for r in rows:
        shown = "No consent" if (not r["consent"] and r["status"] == "Pending approval") else r["status"]
        out.append({"name": r["name"], "telegram_chat_id": r["telegram_chat_id"], "reminder_at": r["reminder_at"],
                    "message": r["message"], "consent": r["consent"], "status": shown, "error": r["error"],
                    "finished_at": r["finished_at"]})
    return {"rows": out, "total": total, "counts": rs.status_counts()}


@app.get("/api/config")
def config():
    """Which integrations are configured (never returns secrets) + the bot's public username."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token and not _BOT["checked"]:
        _BOT["checked"] = True
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8).json()
            _BOT["username"] = (r.get("result") or {}).get("username") if r.get("ok") else None
        except requests.RequestException:
            _BOT["checked"] = False  # try again next time
    wa = all(os.getenv(k, "").strip() for k in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_API_VERSION"))
    return {"telegram_configured": bool(token), "bot_username": _BOT["username"], "whatsapp_configured": wa}


@app.post("/api/validate-customers")
async def validate_customers_api(request: Request):
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(400, "Empty file or file larger than 20 MB.")
    filename = unquote(request.headers.get("x-filename", "upload.csv"))
    try:
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
        elif filename.lower().endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(data), dtype=str).fillna("")
        else:
            raise ValueError("Please upload a .csv or .xlsx file.")
        result = validate_customers(df)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, "Could not process this spreadsheet. Check the file format and required columns.")
    token = secrets.token_urlsafe(8)
    VALIDATED[token] = result.to_csv(index=False).encode("utf-8")
    while len(VALIDATED) > 3:
        VALIDATED.pop(next(iter(VALIDATED)))
    ok = int(result["eligible_for_demo"].sum())
    rows = [{"line": i + 2, "name": r["name"], "phone": str(r["phone"]), "normalized_phone": "" if pd.isna(r["normalized_phone"]) else r["normalized_phone"],
             "status": r["validation_status"]} for i, r in enumerate(result.head(500).to_dict("records"))]
    return {"token": token, "total": len(result), "valid": ok, "needs_review": len(result) - ok,
            "rows": rows, "truncated": len(result) > 500}


@app.get("/download/validation/{token}")
def download_validation(token: str):
    data = VALIDATED.get(token)
    if data is None:
        raise HTTPException(404, "Report expired. Upload the file again.")
    return Response(data, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=validation_report.csv"})


class TelegramTest(BaseModel):
    chat_id: str = ""
    text: str = ""
    confirm: bool = False


class WhatsAppTest(BaseModel):
    recipient: str = ""
    confirm: bool = False


@app.post("/api/test-telegram")
def test_telegram(body: TelegramTest):
    if not body.confirm:
        raise HTTPException(400, "Please tick the confirmation box first.")
    try:
        return send_telegram_message(body.chat_id, body.text)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/test-whatsapp")
def test_whatsapp(body: WhatsAppTest):
    if not body.confirm:
        raise HTTPException(400, "Please tick the confirmation box first.")
    try:
        return whatsapp_client.send_test_template(body.recipient)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/download/{which}")
def download(which: str):
    path = {"customers": rs.CUSTOMERS_CSV, "delivery_log": rs.DELIVERY_LOG_CSV}.get(which)
    if path is None or not path.exists():
        raise HTTPException(404, "Nothing saved yet.")
    return FileResponse(path, media_type="text/csv", filename=path.name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
