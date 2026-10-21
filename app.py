from datetime import datetime

import pandas as pd
import streamlit as st

from validator import validate_customers
from whatsapp_client import send_test_template
from telegram_client import send_telegram_message
from reminder_store import (
    add_reminders, approve_reminder, init_db, is_due, list_reminders,
    mark_skipped, update_status, make_reminder_id, to_iso, CUSTOMERS_CSV,
    DELIVERY_LOG_CSV, approve_all, status_counts, search_reminders,
)

st.set_page_config(page_title="Customer Communication Automation", page_icon="💬", layout="wide")
APP_VERSION = "queue-v3 (current/all toggle)"
st.title("💬 Customer Communication Automation")
st.caption("Customer data validation with a temporary Telegram reminder demo; WhatsApp is preserved for later.")
init_db()
st.caption(f"App version: {APP_VERSION}")

with st.expander("1. Validate customer spreadsheet", expanded=True):
    st.write("Upload a CSV or Excel file with `name` and `phone` columns.")
    uploaded_file = st.file_uploader("Upload customer spreadsheet", type=["csv", "xlsx"], key="customer_upload")
    if uploaded_file is not None:
        try:
            if uploaded_file.name.lower().endswith(".csv"):
                data = pd.read_csv(uploaded_file, dtype={"phone": "string"})
            else:
                data = pd.read_excel(uploaded_file, dtype={"phone": "string"})
            result = validate_customers(data)
            c1, c2, c3 = st.columns(3)
            c1.metric("Total records", len(result))
            c2.metric("Valid records", int(result["eligible_for_demo"].sum()))
            c3.metric("Needs review", int((~result["eligible_for_demo"]).sum()))
            st.dataframe(result, use_container_width=True, hide_index=True)
            st.download_button("Download validation report", result.to_csv(index=False).encode(), "validation_report.csv", "text/csv")
            st.info("Phone validation checks format only. It does not confirm the number exists or has consented to messages.")
        except (ValueError, KeyError) as exc:
            st.error(f"Spreadsheet validation failed: {exc}")
        except Exception:
            st.error("Could not process this spreadsheet. Check the file format and required columns.")

st.divider()
st.header("2. Telegram bot connection test")
st.caption("Send one test message to your own Telegram account. You must start the bot first.")
test_chat_id = st.text_input("Your Telegram chat ID", placeholder="e.g. 123456789", key="telegram_test_chat")
test_message = st.text_input("Test message", value="Hello! This is a test from my reminder automation app.")
confirm_test = st.checkbox("I confirm this is my own Telegram chat or a recipient who agreed to this test.")
if st.button("Send Telegram test message", type="primary", disabled=not (test_chat_id.strip() and confirm_test)):
    try:
        with st.spinner("Sending Telegram test..."):
            result = send_telegram_message(test_chat_id, test_message)
        st.success(f"Telegram accepted the message. Message ID: {result.get('message_id')}")
        st.caption("API acceptance does not guarantee that the recipient read the message.")
    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))

st.divider()
st.header("3. Schedule Telegram reminders")
st.markdown("Upload a CSV/XLSX with these columns: `name`, `telegram_chat_id`, `reminder_at`, `message`, `consent`. Use ISO date/time such as `2026-10-22 15:30`. Consent must be true/yes/1 for a reminder to be eligible.")
reminder_file = st.file_uploader("Upload reminder schedule", type=["csv", "xlsx"], key="reminder_upload")
current_ids = None  # ids of the rows in the file uploaded right now (drives the queue view)
if reminder_file is not None:
    try:
        if reminder_file.name.lower().endswith(".csv"):
            reminder_df = pd.read_csv(reminder_file, dtype={"telegram_chat_id": "string"})
        else:
            reminder_df = pd.read_excel(reminder_file, dtype={"telegram_chat_id": "string"})
        reminder_df.columns = [str(c).strip().lower() for c in reminder_df.columns]
        required = {"name", "telegram_chat_id", "reminder_at", "message", "consent"}
        missing = required - set(reminder_df.columns)
        if missing:
            st.error("Missing columns: " + ", ".join(sorted(missing)))
        else:
            reminder_df["reminder_at"] = pd.to_datetime(reminder_df["reminder_at"], errors="coerce")
            reminder_df["consent"] = reminder_df["consent"].astype(str).str.strip().str.lower().isin(["true", "yes", "y", "1"])
            reminder_df["telegram_chat_id"] = reminder_df["telegram_chat_id"].fillna("").astype(str).str.strip()
            reminder_df["name"] = reminder_df["name"].fillna("").astype(str).str.strip()
            reminder_df["message"] = reminder_df["message"].fillna("").astype(str).str.strip()
            reminder_df["row_valid"] = (
                reminder_df["name"].ne("") & reminder_df["telegram_chat_id"].ne("") &
                reminder_df["reminder_at"].notna() & reminder_df["message"].ne("")
            )
            valid_df = reminder_df[reminder_df["row_valid"]]
            current_ids = {
                make_reminder_id(r["telegram_chat_id"], to_iso(r["reminder_at"]), r["message"])
                for _, r in valid_df.iterrows()
            }
            st.dataframe(reminder_df, use_container_width=True, hide_index=True)
            st.caption("Only valid rows with explicit consent can be approved. Importing the same recipient/time/message again will not create a duplicate reminder.")
            if st.button("Import valid reminders"):
                rows = []
                for _, row in reminder_df[reminder_df["row_valid"]].iterrows():
                    rows.append({
                        "name": row["name"], "telegram_chat_id": row["telegram_chat_id"],
                        "reminder_at": to_iso(row["reminder_at"]),
                        "message": row["message"], "consent": bool(row["consent"]),
                    })
                count, archived = add_reminders(rows)
                st.success(f"Queued {count} new reminder(s); added {archived} new row(s) to customers.csv. Duplicates were ignored.")
    except Exception:
        st.error("Could not read the reminder file. Check the required columns and date format.")

st.subheader("Reminder queue")
d1, d2 = st.columns(2)
if CUSTOMERS_CSV.exists():
    d1.download_button("Download customers.csv (all imported rows)", CUSTOMERS_CSV.read_bytes(),
                       "customers.csv", "text/csv", key="dl_customers")
if DELIVERY_LOG_CSV.exists():
    d2.download_button("Download delivery_log.csv (sent / failed)", DELIVERY_LOG_CSV.read_bytes(),
                       "delivery_log.csv", "text/csv", key="dl_log")


def display_status(r):
    """Rows without consent can never be approved or sent, so say that instead of 'Pending approval'."""
    if not r["consent"] and r["status"] == "Pending approval":
        return "No consent"
    return r["status"]


STATUS_OPTIONS = ["All", "Pending approval", "Approved", "Sent", "Failed", "Skipped", "No consent"]
TABLE_COLS = ["name", "telegram_chat_id", "reminder_at", "message", "consent", "status", "attempts", "error"]


def show_metrics(counts):
    cols = st.columns(5)
    cols[0].metric("Pending approval", counts.get("Pending approval", 0))
    cols[1].metric("Approved (waiting)", counts.get("Approved", 0) + counts.get("Sending", 0))
    cols[2].metric("Sent", counts.get("Sent", 0))
    cols[3].metric("Failed / skipped", counts.get("Failed", 0) + counts.get("Skipped", 0))
    cols[4].metric("No consent", counts.get("No consent", 0))


def current_upload_view(ids):
    if ids is None:
        st.info("No file uploaded. Upload a reminder schedule above to work on it here "
                "(switch to 'All records' to look at everything imported so far).")
        return
    reminders = list_reminders(ids)
    if not reminders:
        st.info("This file's rows are not imported yet. Click 'Import valid reminders' above.")
        return
    for r in reminders:
        r["status"] = display_status(r)
    view = pd.DataFrame(reminders)
    show_metrics(view["status"].value_counts().to_dict())
    show = st.selectbox("Show", STATUS_OPTIONS, key="queue_filter")
    shown = view if show == "All" else view[view["status"] == show]
    st.dataframe(shown[TABLE_COLS], use_container_width=True, hide_index=True)

    pending = [r for r in reminders if r["status"] == "Pending approval" and r["consent"]]
    if st.button(f"Approve ALL {len(pending)} consented pending reminders in this file",
                 disabled=not pending, type="primary", key="approve_all_btn"):
        approve_all([r["id"] for r in pending])
        st.rerun(scope="fragment")
    choices = {f"{r['name']} | {r['reminder_at']} | {r['id']}": r["id"] for r in pending}
    selected = st.multiselect("Or select specific reminders to approve", list(choices.keys()), key="approve_pick")
    if st.button("Approve selected reminders", disabled=not selected, key="approve_sel_btn"):
        for label in selected:
            approve_reminder(choices[label])
        st.rerun(scope="fragment")
    if (view["status"] == "No consent").any():
        st.caption("Rows marked 'No consent' (consent = 0) are kept for your records but can never be approved or sent.")


def all_records_view():
    show_metrics(status_counts())
    c1, c2 = st.columns([1, 2])
    status = c1.selectbox("Show", STATUS_OPTIONS, key="all_filter")
    text = c2.text_input("Search name / chat id / message", key="all_search")
    rows, total = search_reminders(status, text, limit=500)
    for r in rows:
        r["status"] = display_status(r)
    if rows:
        st.dataframe(pd.DataFrame(rows)[TABLE_COLS], use_container_width=True, hide_index=True)
    st.caption(f"Showing the newest {len(rows)} of {total} matching record(s). "
               "Download customers.csv above for the complete list. Approving is only available in 'Current upload'.")


@st.fragment(run_every=10)  # re-reads statuses from the database every 10 seconds
def queue_view(ids):
    mode = st.radio("View", ["Current upload", "All records"], horizontal=True, key="queue_mode")
    st.caption(f"Live: refreshes every 10 s (last {datetime.now().strftime('%H:%M:%S')}). "
               "Sending is done by `python scheduler.py`.")
    if mode == "Current upload":
        current_upload_view(ids)
    else:
        all_records_view()


queue_view(current_ids)

st.divider()
st.header("4. WhatsApp integration (preserved, currently blocked)")
st.caption("The existing WhatsApp sender is retained for future authorized access. Your current Meta test WABA is restricted, so this panel is disabled by default.")
if st.checkbox("Show WhatsApp test panel (only if you later have authorized working access)"):
    recipient = st.text_input("Approved WhatsApp test recipient", placeholder="919876543210")
    confirmed = st.checkbox("I confirm this is my own approved WhatsApp test recipient.")
    if st.button("Send WhatsApp hello_world template", disabled=not (confirmed and recipient.strip())):
        try:
            response = send_test_template(recipient)
            st.success("Meta accepted the request. Delivery is not guaranteed.")
            st.code(response["message_id"])
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))

st.caption("Development demo. Use only consenting recipients. Telegram chat IDs are not WhatsApp phone numbers.")
