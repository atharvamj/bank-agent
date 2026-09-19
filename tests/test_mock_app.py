"""Tests for the mock bank app routes."""

import pytest
from mock_app.app import app
from mock_app.data import ACCOUNTS, REVERSAL_COUNTS, Fee


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["logged_in"] = True
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert b"ok" in r.data


def test_login_failure(client):
    # Start a fresh client without pre-set session
    with app.test_client() as c:
        r = c.post("/", data={"username": "wrong", "password": "wrong"})
        assert b"Invalid credentials" in r.data


def test_login_success():
    with app.test_client() as c:
        r = c.post(
            "/",
            data={"username": "agent", "password": "bankpass"},
            follow_redirects=True,
        )
        assert r.status_code == 200


def test_account_search_found(client):
    r = client.get("/accounts/frame?q=88214")
    assert b"Maria Delgado" in r.data


def test_account_search_not_found(client):
    r = client.get("/accounts/frame?q=99999")
    assert b"No records found" in r.data


def test_account_search_unknown(client):
    r = client.get("/accounts/frame?q=00000")
    assert b"No records found" in r.data


def test_account_detail(client):
    r = client.get("/accounts/88214")
    assert b"Overdraft Fee" in r.data
    assert b"Maria Delgado" in r.data


def test_reversal_success(client):
    # Reset state
    REVERSAL_COUNTS.clear()
    ACCOUNTS["88214"].fees[0].reversed = False
    r = client.post("/accounts/88214/reverse/F001")
    assert b"REVERSAL COMPLETE" in r.data


def test_reversal_already_reversed(client):
    REVERSAL_COUNTS.clear()
    ACCOUNTS["88214"].fees[0].reversed = True
    r = client.post("/accounts/88214/reverse/F001")
    assert b"already reversed" in r.data.lower()
    ACCOUNTS["88214"].fees[0].reversed = False  # restore


def test_reversal_threshold_hold(client):
    REVERSAL_COUNTS["88214"] = 1
    r = client.post("/accounts/88214/reverse/F002")
    assert b"Supervisor Approval" in r.data
    REVERSAL_COUNTS.clear()


def test_supervisor_approve_wrong_password(client):
    REVERSAL_COUNTS["88214"] = 1
    ACCOUNTS["88214"].fees[1].reversed = False
    r = client.post(
        "/accounts/88214/reverse/F002/approve",
        data={"supervisor_password": "wrong"},
    )
    assert b"Incorrect supervisor password" in r.data
    REVERSAL_COUNTS.clear()


def test_supervisor_approve_correct_password(client):
    REVERSAL_COUNTS["88214"] = 1
    ACCOUNTS["88214"].fees[1].reversed = False
    r = client.post(
        "/accounts/88214/reverse/F002/approve",
        data={"supervisor_password": "sup3rvisor"},
    )
    assert b"REVERSAL COMPLETE" in r.data
    REVERSAL_COUNTS.clear()
    ACCOUNTS["88214"].fees[1].reversed = False
