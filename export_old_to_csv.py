"""One-time: copy every reminder already in reminders.db into customers.csv.

Run once:  python export_old_to_csv.py
Safe to re-run: rows already in customers.csv are skipped. Nothing is deleted from the DB.
"""
from reminder_store import list_reminders, init_db, _append_to_customers_csv, CUSTOMERS_CSV

init_db()
rows = [{
    "id": r["id"], "name": r["name"], "telegram_chat_id": r["telegram_chat_id"],
    "reminder_at": r["reminder_at"], "message": r["message"],
    "consent": r["consent"], "imported_at": r["created_at"],
} for r in list_reminders()]
added = _append_to_customers_csv(rows)
print(f"{len(rows)} reminder(s) in the database; {added} newly added to {CUSTOMERS_CSV.name}.")
