"""
Guardrail enforcement — wraps action execution for both agent loop and replay engine.
Single entry point: check_and_enforce(action, target_label, url, session_state)
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from guardrails.classifier import Risk, classify_action

_ALLOWLIST_PATH = Path(__file__).parent / "allowlist.yaml"
_allowlist: dict | None = None


def _load_allowlist() -> dict:
    global _allowlist  # noqa: PLW0603
    if _allowlist is None:
        with _allowlist_path_open() as f:
            _allowlist = yaml.safe_load(f)
    return _allowlist


def _allowlist_path_open():
    return open(_ALLOWLIST_PATH, encoding="utf-8")  # noqa: WPS515


class GuardrailViolation(Exception):
    """Raised when an action violates the allowlist or risk policy."""


def check_action(
    action: str,
    target_label: str,
    current_url: str = "",
    session_state: dict | None = None,
) -> Risk:
    """
    Validate action against allowlist and classify risk.

    Returns Risk.SAFE or Risk.IRREVERSIBLE.
    Raises GuardrailViolation on hard policy violations.
    """
    al = _load_allowlist()

    # 1. Action type must be in allowlist
    permitted = al.get("permitted_actions", [])
    if action not in permitted:
        raise GuardrailViolation(
            f"Action '{action}' is not in the permitted actions list: {permitted}"
        )

    # 2. Current URL domain must be in allowlist
    if current_url:
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        permitted_domains = al.get("permitted_domains", [])
        if not any(d in hostname for d in permitted_domains):
            raise GuardrailViolation(
                f"Domain '{hostname}' is not in permitted_domains: {permitted_domains}"
            )

        # 3. Route must not be blocked
        path = parsed.path
        for blocked in al.get("blocked_routes", []):
            if path.startswith(blocked):
                raise GuardrailViolation(f"Route '{path}' is blocked.")

    # 4. Risk classification
    return classify_action(action, target_label, session_state)
