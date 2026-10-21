
import pandas as pd
import pytest
from validator import normalize_phone, validate_customers


def test_normalize_indian_number():
    assert normalize_phone("+91 98765-43210") == "+919876543210"


def test_invalid_phone():
    assert normalize_phone("abc") is None


def test_duplicate_numbers():
    df = pd.DataFrame({
        "name": ["A", "B"],
        "phone": ["+919876543210", "+919876543210"],
    })

    result = validate_customers(df)

    assert result["eligible_for_demo"].tolist() == [True, False]


def test_missing_required_column():
    df = pd.DataFrame({"name": ["A"]})

    with pytest.raises(ValueError):
        validate_customers(df)

