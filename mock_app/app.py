"""
Flask mock bank application — deliberately hostile DOM.

Hostile features:
  - Nested <table> layout, zero data-testid attributes
  - Account search rendered inside an <iframe> (/accounts/frame)
  - No <label for=...> wiring on the login form
  - Fee rows built from nested tables with no IDs
  - Confirmation appears as an in-place div replacement (no page reload)
  - ?slow=1 query param sleeps 8 s to simulate timeout
"""

import time
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from mock_app.data import (
    get_account,
    get_fee,
    reverse_fee,
    reverse_fee_with_supervisor,
    ACCOUNTS,
)

app = Flask(__name__, template_folder="templates")
app.secret_key = "dev-only-secret-not-for-prod"  # noqa: S105


# ---------------------------------------------------------------------------
# Middleware: slow-mode simulation
# ---------------------------------------------------------------------------
@app.before_request
def maybe_slow():
    if request.args.get("slow") == "1":
        time.sleep(8)


# ---------------------------------------------------------------------------
# Login screen  (GET /)
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == "agent" and password == "bankpass":
            session["logged_in"] = True
            return redirect(url_for("accounts_shell"))
        error = "Invalid credentials. Try agent / bankpass."
    return render_template("login.html", error=error)


# ---------------------------------------------------------------------------
# Account search shell  (GET /accounts) — contains the iframe
# ---------------------------------------------------------------------------
@app.route("/accounts")
def accounts_shell():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return render_template("accounts_shell.html")


# ---------------------------------------------------------------------------
# Account search iframe content  (GET /accounts/frame)
# ---------------------------------------------------------------------------
@app.route("/accounts/frame")
def accounts_frame():
    query = request.args.get("q", "").strip()
    results = []
    error = None

    if query:
        if query == "99999":
            error = "No records found for account 99999."
        else:
            acct = get_account(query)
            if acct:
                results = [acct]
            else:
                error = f"No records found for account {query}."

    return render_template("accounts_frame.html", query=query, results=results, error=error)


# ---------------------------------------------------------------------------
# Account detail  (GET /accounts/<id>)
# ---------------------------------------------------------------------------
@app.route("/accounts/<account_id>")
def account_detail(account_id: str):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    acct = get_account(account_id)
    if not acct:
        return render_template("error.html", message=f"Account {account_id} not found."), 404
    return render_template("account_detail.html", account=acct)


# ---------------------------------------------------------------------------
# Reversal endpoint  (POST /accounts/<id>/reverse/<fee_id>)
# ---------------------------------------------------------------------------
@app.route("/accounts/<account_id>/reverse/<fee_id>", methods=["POST"])
def reverse(account_id: str, fee_id: str):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    success, reason = reverse_fee(account_id, fee_id)

    if reason == "threshold_hold":
        # Show the supervisor approval screen
        acct = get_account(account_id)
        fee = get_fee(account_id, fee_id)
        return render_template(
            "supervisor_approval.html",
            account=acct,
            fee=fee,
            account_id=account_id,
            fee_id=fee_id,
        )

    if reason == "already_reversed":
        acct = get_account(account_id)
        fee = get_fee(account_id, fee_id)
        return render_template(
            "reversal_error.html",
            account=acct,
            fee=fee,
            message="Fee already reversed. No action taken.",
        )

    if reason == "not_found":
        return render_template("error.html", message="Fee or account not found."), 404

    # success
    acct = get_account(account_id)
    fee = get_fee(account_id, fee_id)
    return render_template("reversal_confirmation.html", account=acct, fee=fee)


# ---------------------------------------------------------------------------
# Supervisor approval submission  (POST /accounts/<id>/reverse/<fee_id>/approve)
# ---------------------------------------------------------------------------
@app.route("/accounts/<account_id>/reverse/<fee_id>/approve", methods=["POST"])
def supervisor_approve(account_id: str, fee_id: str):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    password = request.form.get("supervisor_password", "")
    success, reason = reverse_fee_with_supervisor(account_id, fee_id, password)

    if reason == "permission_denied":
        acct = get_account(account_id)
        fee = get_fee(account_id, fee_id)
        return render_template(
            "supervisor_approval.html",
            account=acct,
            fee=fee,
            account_id=account_id,
            fee_id=fee_id,
            error="Incorrect supervisor password.",
        )

    if not success:
        return render_template("error.html", message=f"Supervisor approval failed: {reason}."), 400

    acct = get_account(account_id)
    fee = get_fee(account_id, fee_id)
    return render_template("reversal_confirmation.html", account=acct, fee=fee)


# ---------------------------------------------------------------------------
# Health check (used by tests and startup probe)
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(port=5000, debug=True)
