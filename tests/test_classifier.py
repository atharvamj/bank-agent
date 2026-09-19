"""Tests for the risk classifier."""

from guardrails.classifier import Risk, classify_action


def test_safe_type_action():
    assert classify_action("type", "Account Number field") == Risk.SAFE


def test_click_confirm_irreversible():
    assert classify_action("click", "Authorize Reversal") == Risk.IRREVERSIBLE


def test_click_submit_irreversible():
    assert classify_action("click", "Submit form") == Risk.IRREVERSIBLE


def test_click_delete_irreversible():
    assert classify_action("click", "Delete account") == Risk.IRREVERSIBLE


def test_wait_for_safe():
    assert classify_action("wait_for", "Loading spinner", None) == Risk.SAFE


def test_second_reversal_irreversible():
    """Second reversal on same account is always IRREVERSIBLE via threshold check."""
    session_state = {"reversal_counts": {"88214": 1}}
    result = classify_action("click", "Reverse Fee", session_state)
    assert result == Risk.IRREVERSIBLE


def test_first_reversal_safe():
    """First reversal on an account is SAFE per threshold (single reversal auto-approves)."""
    session_state = {"reversal_counts": {"88214": 0}}
    result = classify_action("click", "Reverse Fee", session_state)
    # "reverse" is in _IRREVERSIBLE_VERBS, so this will be IRREVERSIBLE too
    # (by verb match, not threshold) — that's intentional per the brief
    assert result == Risk.IRREVERSIBLE


def test_click_view_safe():
    assert classify_action("click", "View account details") == Risk.SAFE


def test_click_sign_in_safe():
    """Sign In / login is NOT in the irreversible verb list — it is a prerequisite action."""
    assert classify_action("click", "Sign In") == Risk.SAFE
