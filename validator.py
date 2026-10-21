
import re
import pandas as pd

REQUIRED_COLUMNS = {"name", "phone"}

def normalize_phone(value):
    if pd.isna(value):
        return None

    # Spreadsheet cells may contain numbers or text.
    phone = str(value).strip()

    if phone.endswith(".0") and phone[:-2].isdigit():
        phone = phone[:-2]

    # Permit common formatting characters.
    phone = re.sub(r"[\s().-]", "", phone)

    if phone.startswith("00"):
        phone = "+" + phone[2:]

    if phone.startswith("+"):
        digits = phone[1:]
    else:
        digits = phone

    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return None

    return "+" + digits


def validate_customers(df):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing_columns = REQUIRED_COLUMNS - set(df.columns)
    if missing_columns:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    df["name"] = df["name"].fillna("").astype(str).str.strip()
    df["normalized_phone"] = df["phone"].apply(normalize_phone)

    df["validation_status"] = "Valid"
    df.loc[df["name"] == "", "validation_status"] = "Missing name"

    invalid_phone = df["normalized_phone"].isna()
    df.loc[invalid_phone, "validation_status"] = "Invalid phone"

    duplicate = (
        df["normalized_phone"].notna()
        & df["normalized_phone"].duplicated(keep="first")
    )
    df.loc[duplicate, "validation_status"] = "Duplicate phone"

    df["eligible_for_demo"] = (
        df["validation_status"] == "Valid"
    )

    return df
