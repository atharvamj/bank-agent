"""Tests for the redactor module."""

from guardrails.redactor import redact, redact_dict


def test_account_number_masked():
    assert redact("Account 88214 has a fee") == "Account [REDACTED] has a fee"


def test_short_number_not_masked():
    # 4-digit numbers are NOT account-number-like
    assert redact("Step 1234 of 5") == "Step 1234 of 5"


def test_sensitive_value_masked():
    result = redact("Password is bankpass here", sensitive_values=["bankpass"])
    assert "bankpass" not in result
    assert "[REDACTED]" in result


def test_multiple_account_numbers():
    result = redact("Accounts 88214 and 77301 are affected")
    assert "88214" not in result
    assert "77301" not in result


def test_dict_redaction():
    data = {"account": "88214", "name": "Maria", "balance": -42.5}
    result = redact_dict(data)
    assert result["account"] == "[REDACTED]"
    assert result["name"] == "Maria"  # short string, no digit pattern


def test_nested_dict_redaction():
    data = {"user": {"account_id": "88214", "pin": "1234"}}
    result = redact_dict(data, sensitive_values=["1234"])
    assert result["user"]["account_id"] == "[REDACTED]"
    # pin is 4 digits — below threshold — but listed as sensitive_value
    assert result["user"]["pin"] == "[REDACTED]"
