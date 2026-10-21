"""Background worker: automatically sends approved, due Telegram reminders.

Run separately from Streamlit:  python scheduler.py
Keep this process and the host computer/server running.
"""
from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
from reminder_store import (
    init_db, connect, is_due, update_status, mark_skipped,
    recover_interrupted, local_tz, due_candidates,
)
from telegram_client import send_telegram_message

load_dotenv()
POLL_SECONDS = max(5, int(os.getenv("SCHEDULER_POLL_SECONDS", "15")))
DRY_RUN = os.getenv("SCHEDULER_DRY_RUN", "true").strip().lower() in {"1", "true", "yes", "on"}
MAX_ATTEMPTS = max(1, int(os.getenv("SCHEDULER_MAX_ATTEMPTS", "3")))
# Reminders overdue by more than this are skipped instead of being sent late (0 = never skip).
MAX_LATE_MINUTES = max(0, int(os.getenv("SCHEDULER_MAX_LATE_MINUTES", "1440")))
HEARTBEAT_EVERY = 20  # polls (~5 min at 15s)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminder-scheduler")

_dry_logged = set()


def claim_due_reminders():
    """Atomically claim due reminders so two workers don't process the same row."""
    claimed = []
    with connect() as conn:
        for row in due_candidates():
            cur = conn.execute("""
                UPDATE reminders SET status='Sending', error=NULL
                WHERE id=? AND status='Approved' AND consent=1
            """, (row["id"],))
            if cur.rowcount == 1:
                claimed.append(dict(row))
    return claimed


def process_due_once():
    now = datetime.now(local_tz())

    if DRY_RUN:
        # Dry-run must NOT consume reminders: leave them 'Approved' so they still send
        # once you switch SCHEDULER_DRY_RUN=false. Just report what would be sent.
        for row in due_candidates(now):
            if row["id"] not in _dry_logged:
                _dry_logged.add(row["id"])
                log.warning("DRY RUN: would send reminder %s to chat %s (nothing sent)",
                            row["id"], row["telegram_chat_id"])
        return

    for reminder in claim_due_reminders():
        rid = reminder["id"]

        if MAX_LATE_MINUTES:
            late_by = now - _as_aware(reminder["reminder_at"])
            if late_by > timedelta(minutes=MAX_LATE_MINUTES):
                mark_skipped(rid, f"Too late: due {reminder['reminder_at']}, skipped to avoid a stale message.")
                log.warning("Reminder %s skipped: overdue by %s", rid, late_by)
                continue

        try:
            result = send_telegram_message(reminder["telegram_chat_id"], reminder["message"])
            update_status(rid, "Sent", message_id=str(result.get("message_id", "")), attempted=True)
            log.info("Telegram accepted reminder %s (message_id=%s)", rid, result.get("message_id"))
        except Exception as exc:
            update_status(rid, "Failed", error=str(exc)[:1000], attempted=True)
            log.error("Reminder %s failed; review in dashboard: %s", rid, exc)


def _as_aware(value):
    from datetime import datetime as _dt
    dt = _dt.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


def heartbeat():
    now = datetime.now(local_tz())
    with connect() as conn:
        n, earliest = conn.execute(
            "SELECT COUNT(*), MIN(reminder_at) FROM reminders WHERE status='Approved' AND consent=1").fetchone()
    log.info("Alive. now=%s | approved & not yet sent=%d | earliest=%s",
             now.isoformat(timespec="seconds"), n, earliest or "none")


def main():
    init_db()
    stuck = recover_interrupted()
    log.info("Scheduler started. Poll=%ss; dry_run=%s; timezone=%s", POLL_SECONDS, DRY_RUN, local_tz())
    if DRY_RUN:
        log.warning("*** DRY RUN IS ON: NOTHING WILL BE SENT. Set SCHEDULER_DRY_RUN=false in .env and restart. ***")
    if stuck:
        log.warning("%d reminder(s) were stuck in 'Sending' from a previous run; marked Failed for review.", stuck)
    polls = 0
    while True:
        try:
            process_due_once()
            if polls % HEARTBEAT_EVERY == 0:
                heartbeat()
        except Exception:
            log.exception("Unexpected scheduler error; will continue after polling interval")
        polls += 1
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
