"""SQLite-backed reminder queue + persistent customers.csv archive."""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).with_name("reminders.db")
CUSTOMERS_CSV = Path(__file__).with_name("customers.csv")
DELIVERY_LOG_CSV = Path(__file__).with_name("delivery_log.csv")
LOG_FIELDS = ["id", "status", "telegram_message_id", "error", "at"]
_CHUNK = 500  # keep SQL IN (...) lists well under SQLite's variable limit
CSV_FIELDS = ["id", "name", "telegram_chat_id", "reminder_at", "message", "consent", "imported_at"]


def local_tz():
    """Timezone used for naive timestamps (e.g. '2026-10-22 15:30' from a CSV).

    Set APP_TIMEZONE in .env. Defaults to India time so it behaves the same on your
    laptop and on a cloud server (which is usually UTC).
    """
    name = os.getenv("APP_TIMEZONE", "Asia/Kolkata").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo


def to_iso(value):
    """Normalise a datetime/Timestamp/string to a tz-aware ISO string (minute precision)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz()).isoformat(timespec="minutes")


def make_reminder_id(telegram_chat_id, reminder_at, message):
    identity = "|".join([str(telegram_chat_id).strip(), str(reminder_at).strip(), str(message).strip()])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                telegram_chat_id TEXT NOT NULL,
                reminder_at TEXT NOT NULL,
                message TEXT NOT NULL,
                consent INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Pending approval',
                approved_at TEXT,
                attempted_at TEXT,
                telegram_message_id TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        # Makes "approved & due" lookups fast even with lakhs of rows.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status_at ON reminders(status, reminder_at)")
        # finished_at = when a reminder reached Sent/Failed/Skipped (drives the live activity feed).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
        if "finished_at" not in cols:
            conn.execute("ALTER TABLE reminders ADD COLUMN finished_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_finished ON reminders(finished_at)")


def _append_to_customers_csv(rows_with_ids):
    """Append rows to customers.csv, skipping ids already stored. Returns number appended."""
    existing = set()
    if CUSTOMERS_CSV.exists():
        with open(CUSTOMERS_CSV, newline="", encoding="utf-8") as f:
            existing = {r.get("id") for r in csv.DictReader(f)}
    new_rows = [r for r in rows_with_ids if r["id"] not in existing]
    if not new_rows:
        return 0
    write_header = not CUSTOMERS_CSV.exists() or CUSTOMERS_CSV.stat().st_size == 0
    with open(CUSTOMERS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    return len(new_rows)


def add_reminders(rows):
    """Insert rows idempotently into the DB and append new ones to customers.csv.

    Expected keys: name, telegram_chat_id, reminder_at, message, consent.
    Returns (inserted_into_queue, appended_to_customers_csv).
    """
    init_db()
    inserted = 0
    now_iso = datetime.now(local_tz()).isoformat(timespec="seconds")
    archive = []
    with connect() as conn:
        for row in rows:
            reminder_id = make_reminder_id(row["telegram_chat_id"], row["reminder_at"], row["message"])
            cur = conn.execute("""
                INSERT OR IGNORE INTO reminders
                (id, name, telegram_chat_id, reminder_at, message, consent, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'Pending approval', ?)
            """, (
                reminder_id, str(row["name"]).strip(), str(row["telegram_chat_id"]).strip(),
                str(row["reminder_at"]).strip(), str(row["message"]).strip(),
                int(bool(row["consent"])), now_iso,
            ))
            inserted += cur.rowcount
            archive.append({
                "id": reminder_id, "name": str(row["name"]).strip(),
                "telegram_chat_id": str(row["telegram_chat_id"]).strip(),
                "reminder_at": str(row["reminder_at"]).strip(), "message": str(row["message"]).strip(),
                "consent": int(bool(row["consent"])), "imported_at": now_iso,
            })
    appended = _append_to_customers_csv(archive)
    return inserted, appended


def list_reminders(ids=None):
    """Reminders for the given ids (fetched in SQL chunks, never the whole table).

    ids=None returns everything - avoid that on big databases.
    """
    init_db()
    with connect() as conn:
        if ids is None:
            rows = conn.execute("SELECT * FROM reminders ORDER BY reminder_at, created_at").fetchall()
            return [dict(r) for r in rows]
        ids = list(ids)
        result = []
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            q = ",".join("?" * len(chunk))
            result += [dict(r) for r in conn.execute(f"SELECT * FROM reminders WHERE id IN ({q})", chunk)]
    return sorted(result, key=lambda r: (r["reminder_at"], r["created_at"]))


_STATUS_CASE = "CASE WHEN consent=0 AND status='Pending approval' THEN 'No consent' ELSE status END"


def status_counts(ids=None):
    """{display_status: count}. Done in SQL, so it is fast for the whole table too."""
    init_db()
    counts = {}
    with connect() as conn:
        if ids is None:
            for st_, n in conn.execute(f"SELECT {_STATUS_CASE} AS s, COUNT(*) FROM reminders GROUP BY s"):
                counts[st_] = n
        else:
            ids = list(ids)
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                q = ",".join("?" * len(chunk))
                for st_, n in conn.execute(
                        f"SELECT {_STATUS_CASE} AS s, COUNT(*) FROM reminders WHERE id IN ({q}) GROUP BY s", chunk):
                    counts[st_] = counts.get(st_, 0) + n
    return counts


def search_reminders(status=None, text=None, limit=500):
    """Newest-first slice of the WHOLE table for the 'All records' view.
    Returns (rows, total_matching). Only `limit` rows are ever loaded."""
    init_db()
    where, args = [], []
    if status and status != "All":
        where.append(f"({_STATUS_CASE}) = ?")
        args.append(status)
    if text and text.strip():
        like = f"%{text.strip()}%"
        where.append("(name LIKE ? OR telegram_chat_id LIKE ? OR message LIKE ?)")
        args += [like, like, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM reminders {clause}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM reminders {clause} ORDER BY created_at DESC, reminder_at DESC LIMIT ?",
            [*args, int(limit)]).fetchall()
    return [dict(r) for r in rows], total


def waiting_reminders(limit=300):
    """Consented reminders still waiting to be sent (the 'current board'). Returns (rows, total)."""
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE status IN ('Approved','Sending') AND consent=1").fetchone()[0]
        rows = conn.execute("""
            SELECT id, name, telegram_chat_id, reminder_at, message, status FROM reminders
            WHERE status IN ('Approved','Sending') AND consent=1
            ORDER BY reminder_at LIMIT ?""", (int(limit),)).fetchall()
    return [dict(r) for r in rows], total


def recent_outcomes(limit=30):
    """Newest finished reminders (Sent / Failed / Skipped) for the live activity feed."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, name, telegram_chat_id, reminder_at, status, error, finished_at FROM reminders
            WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT ?""", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def outcome_counts_today():
    day = datetime.now(local_tz()).strftime("%Y-%m-%d")
    with connect() as conn:
        return {st_: n for st_, n in conn.execute(
            "SELECT status, COUNT(*) FROM reminders WHERE finished_at >= ? GROUP BY status", (day,))}


def due_candidates(now=None):
    """Approved+consented rows that could be due. Indexed SQL prefilter by date (generous by
    one day to cover timezone formats), then the exact check is done by is_due()."""
    now = now or datetime.now(local_tz())
    cutoff = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    with connect() as conn:
        rows = conn.execute("""
            SELECT * FROM reminders
            WHERE status='Approved' AND consent=1 AND substr(reminder_at,1,10) <= ?
            ORDER BY reminder_at, created_at
        """, (cutoff,)).fetchall()
    return [r for r in rows if is_due(r["reminder_at"], now)]


def approve_all(ids):
    """Approve every consented, pending reminder among ids. Returns how many were approved."""
    ids = list(ids)
    when = datetime.now(local_tz()).isoformat(timespec="seconds")
    n = 0
    with connect() as conn:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            q = ",".join("?" * len(chunk))
            cur = conn.execute(f"""
                UPDATE reminders SET status='Approved', approved_at=?
                WHERE status='Pending approval' AND consent=1 AND id IN ({q})
            """, [when, *chunk])
            n += cur.rowcount
    return n


def _log_outcome(reminder_id, status, message_id, error):
    """Append-only delivery log (never rewritten, so it stays fast at any size)."""
    write_header = not DELIVERY_LOG_CSV.exists() or DELIVERY_LOG_CSV.stat().st_size == 0
    with open(DELIVERY_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({"id": reminder_id, "status": status, "telegram_message_id": message_id or "",
                    "error": error or "", "at": datetime.now(local_tz()).isoformat(timespec="seconds")})


def approve_reminder(reminder_id):
    with connect() as conn:
        conn.execute("""
            UPDATE reminders SET status='Approved', approved_at=?
            WHERE id=? AND status='Pending approval' AND consent=1
        """, (datetime.now(local_tz()).isoformat(timespec="seconds"), reminder_id))


def update_status(reminder_id, status, *, message_id=None, error=None, attempted=False):
    terminal = status in {"Sent", "Failed", "Skipped"}
    now_iso = datetime.now(local_tz()).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("""
            UPDATE reminders
            SET status=?, telegram_message_id=COALESCE(?, telegram_message_id), error=?,
                attempted_at=CASE WHEN ? THEN ? ELSE attempted_at END,
                attempts=attempts + CASE WHEN ? THEN 1 ELSE 0 END,
                finished_at=CASE WHEN ? THEN ? ELSE finished_at END
            WHERE id=?
        """, (status, message_id, error, int(attempted), now_iso, int(attempted),
              int(terminal), now_iso, reminder_id))
    if terminal:
        _log_outcome(reminder_id, status, message_id, error)


def mark_skipped(reminder_id, reason):
    update_status(reminder_id, "Skipped", error=reason)


def recover_interrupted():
    """Rows left in 'Sending' by a crashed worker would be stuck forever. Flag them for review."""
    with connect() as conn:
        cur = conn.execute("""
            UPDATE reminders SET status='Failed',
                error='Worker stopped while sending; check Telegram before re-approving.'
            WHERE status='Sending'
        """)
        return cur.rowcount


def is_due(reminder_at, now=None):
    """Timezone-aware ISO timestamps are used as-is; naive ones are read in APP_TIMEZONE."""
    now = now or datetime.now(local_tz())
    try:
        scheduled = datetime.fromisoformat(str(reminder_at))
    except ValueError:
        return False
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=local_tz())
    return scheduled <= now
