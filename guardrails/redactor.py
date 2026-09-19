"""
Redactor — masks sensitive patterns before writing to evidence logs or files.

Patterns masked:
  - Account-number-like sequences (5+ consecutive digits)
  - Any field name listed in `sensitive_fields` (exact value match in text)

Replacement: [REDACTED]
"""

from __future__ import annotations

import re

_ACCOUNT_PATTERN = re.compile(r"\b\d{5,}\b")
_PLACEHOLDER = "[REDACTED]"


def redact(text: str, sensitive_values: list[str] | None = None) -> str:
    """
    Return a copy of `text` with sensitive patterns replaced by [REDACTED].

    Parameters
    ----------
    text             : str — the text to scrub
    sensitive_values : list[str] — explicit values to mask (e.g. typed passwords)
    """
    result = _ACCOUNT_PATTERN.sub(_PLACEHOLDER, text)

    if sensitive_values:
        for val in sensitive_values:
            if val and len(val) >= 3:  # avoid masking trivially short strings
                result = result.replace(val, _PLACEHOLDER)

    return result


def redact_dict(data: dict, sensitive_values: list[str] | None = None) -> dict:
    """Recursively redact all string values in a dict."""
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = redact(v, sensitive_values)
        elif isinstance(v, dict):
            out[k] = redact_dict(v, sensitive_values)
        elif isinstance(v, list):
            out[k] = [
                redact(item, sensitive_values) if isinstance(item, str)
                else redact_dict(item, sensitive_values) if isinstance(item, dict)
                else item
                for item in v
            ]
        else:
            out[k] = v
    return out
