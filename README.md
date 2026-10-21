# Reminder Control Room

    python -m pip install -r requirements.txt
    copy .env.example .env      (then put your TELEGRAM_BOT_TOKEN in it, keep SCHEDULER_DRY_RUN=false)
    python server.py            -> open http://127.0.0.1:8000

One process does everything: the web page AND the automatic sender. Do not also run scheduler.py.

Tabs: Current (upload -> schedule -> live board) | All (full history) | Validate list (name/phone check) | Connections (Telegram test, WhatsApp panel).

Flow: Current tab -> upload CSV/XLSX -> consent/validity check -> Proceed -> messages wait on the board with a
live countdown -> sent automatically at the scheduled minute -> leave the board, appear in Live activity,
and are kept in the All tab, customers.csv (every imported row) and delivery_log.csv (sent/failed/skipped).

- Top-right badge: green LIVE = sender is running; amber DRY RUN = nothing is sent; red = sender stopped.
- Times are minute-precise (15:30 sends at 15:30:00, within the SERVER_POLL_SECONDS check interval).
- The PC must stay on and awake with server.py running. For always-on use, host it on a small server.
- Keep .env, reminders.db, customers.csv private. The server listens on 127.0.0.1 only (no login).
- The old Streamlit UI (app.py) still works but is no longer needed.

## Telegram and phone numbers
A Telegram bot cannot message a phone number. It needs the customer's numeric chat ID, and that only exists after the
customer opens your bot (t.me/<bot username>) and presses Start. The page shows your bot's link on the Current and
Connections tabs. Run `python get_telegram_chat_id.py` after a customer presses Start to see their chat ID.
