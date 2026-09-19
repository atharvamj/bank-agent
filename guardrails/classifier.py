"""
Risk classifier — shared by both agent loop and replay engine.
Never implement this check twice.
"""

from __future__ import annotations

from enum import Enum


class Risk(str, Enum):
    SAFE = "safe"
    IRREVERSIBLE = "irreversible"


# Verbs in a target name that flag an action as irreversible
_IRREVERSIBLE_VERBS = frozenset({
    "submit", "confirm", "delete", "transfer",
    "open account", "authorize", "approve", "reverse fee",
    "reverse",
    # NOTE: 'sign in' / 'login' intentionally excluded — login is a prerequisite,
    # not a destructive action. The threshold check handles the real risk boundary.
})


def classify_action(
    action: str,
    target_label: str,
    session_state: dict | None = None,
) -> Risk:
    """
    Return Risk.IRREVERSIBLE if this action should require explicit approval.

    Parameters
    ----------
    action        : str — one of click, type, wait_for, assert_text
    target_label  : str — accessible name or description of the target element
    session_state : dict — may include 'reversal_counts' {account_id: int}
    """
    label_lower = (target_label or "").lower()

    # Threshold check: second reversal on same account always irreversible
    if session_state and "reversal_counts" in session_state:
        for account_id, count in session_state["reversal_counts"].items():
            if count >= 1 and ("reverse" in label_lower or "reversal" in label_lower):
                return Risk.IRREVERSIBLE

    # Verb-based check
    if action == "click":
        for verb in _IRREVERSIBLE_VERBS:
            if verb in label_lower:
                return Risk.IRREVERSIBLE

    return Risk.SAFE
