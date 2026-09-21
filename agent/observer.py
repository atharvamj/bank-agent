"""
Accessibility observer — produces a compact, frame-aware list of interactive
elements using Playwright's aria_snapshot() API (Playwright >= 1.50).

Returns a list of ElementInfo dicts so the agent loop can build a text prompt
without touching the DOM directly. Works across top-frame and iframes.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Frame, Page

# Roles we consider interactive and want to surface in the prompt
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox",
    "checkbox", "radio", "menuitem", "tab", "option",
}

# Roles to include as context for the LLM but not act on
CONTEXT_ROLES = {"heading", "alert", "status", "text"}

# Matches a single aria-snapshot line: "- role \"name\":"
_LINE_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<role>\w+)(?:\s+\"(?P<name>[^\"]*)\")?\s*(?:\[.*?\])?\s*:?")


def _parse_aria_snapshot(text: str, frame_label: str) -> list[dict]:
    """
    Parse a Playwright aria_snapshot YAML-like string into a flat list of
    element dicts: {frame, role, name, nearby_text}.

    Handles:
    - Named roles:   - textbox "Account Number"
    - Anonymous:     - textbox              (no name — inherits label from sibling text:)
    - text: lines:   - text: Username       (used as label for next sibling element)
    """
    results: list[dict] = []
    lines_list = text.splitlines()

    # Pre-pass: for each line that is an anonymous interactive role,
    # look backwards within the same indentation block for a sibling text: label.
    def _find_nearby_label(idx: int, indent: int) -> str:
        """Search backwards from line idx for a text: at the same or parent indent."""
        for j in range(idx - 1, max(idx - 8, -1), -1):
            prev = lines_list[j]
            stripped = prev.lstrip()
            prev_indent = len(prev) - len(stripped)
            if abs(prev_indent - indent) <= 2:
                tm = re.match(r"-\s+text:\s+(.+)", stripped)
                if tm:
                    return tm.group(1).strip()
                # Also extract name from a cell/row label
                rm = re.match(r"-\s+(?:cell|row)\s+\"([^\"]+)\"", stripped)
                if rm:
                    return rm.group(1).strip()
        return ""

    context_stack: list[str] = []

    for idx, line in enumerate(lines_list):
        m = _LINE_RE.match(line)
        if not m:
            # Check for bare text: lines to update context
            stripped = line.lstrip()
            tm = re.match(r"-\s+text:\s+(.+)", stripped)
            if tm:
                indent = len(line) - len(stripped)
                level = indent // 2
                context_stack = context_stack[:level] + [tm.group(1).strip()]
            continue

        role = m.group("role").lower()
        name = (m.group("name") or "").strip()
        indent = len(line) - len(line.lstrip())
        level = indent // 2
        context_stack = context_stack[:level]

        if role in CONTEXT_ROLES and name:
            context_stack = context_stack[:level] + [name]

        if role in INTERACTIVE_ROLES:
            # If unnamed, try to infer a label from context
            if not name:
                name = _find_nearby_label(idx, indent)
            if not name:
                name = " / ".join(context_stack) if context_stack else role

            nearby = " / ".join(context_stack) if context_stack else ""
            results.append({
                "frame": frame_label,
                "role": role,
                "name": name,
                "nearby_text": nearby,
                "value": None,
                "disabled": False,
            })

    return results


def _snapshot_frame(frame: Page | Frame, frame_label: str) -> list[dict]:
    """Take an aria snapshot of one frame and parse it."""
    try:
        snap = frame.locator("body").aria_snapshot()
        return _parse_aria_snapshot(snap, frame_label)
    except Exception as exc:
        return [{"frame": frame_label, "role": "error", "name": str(exc)}]


def observe_page(page: Page) -> list[dict]:
    """
    Snapshot every frame (main + iframes) and return a flat list of
    interactive elements with role, name, frame label, and nearby context.
    """
    results: list[dict] = []

    # Main frame
    results.extend(_snapshot_frame(page, "main"))

    # All child frames (iframes)
    for i, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        frame_label = frame.name or f"frame_{i}"
        results.extend(_snapshot_frame(frame, frame_label))

    return results


def format_for_prompt(elements: list[dict], max_items: int = 60) -> str:
    """
    Format the element list as a compact numbered text block suitable for
    inclusion in an LLM prompt.
    """
    lines = ["[ACCESSIBLE ELEMENTS]"]
    seen: set[tuple] = set()
    count = 0

    for el in elements:
        if el.get("role") == "error":
            lines.append(f"  [ERR] frame={el['frame']}: {el['name']}")
            continue

        key = (el["frame"], el["role"], el["name"])
        if key in seen:
            continue
        seen.add(key)

        frame_tag = f"[{el['frame']}] " if el["frame"] != "main" else ""
        val_tag = f' value="{el["value"]}"' if el.get("value") else ""
        dis_tag = " (disabled)" if el.get("disabled") else ""
        ctx_tag = f' near="{el["nearby_text"]}"' if el.get("nearby_text") else ""
        lines.append(
            f"  {count}: {frame_tag}{el['role']} \"{el['name']}\"{val_tag}{dis_tag}{ctx_tag}"
        )
        count += 1
        if count >= max_items:
            remaining = len(elements) - count
            if remaining > 0:
                lines.append(f"  ... ({remaining} more elements truncated)")
            break

    return "\n".join(lines)


def quick_snapshot(url: str = "http://127.0.0.1:5001") -> None:
    """
    CLI helper: launch a headless browser, log in, go to accounts,
    and print the iframe-aware element list.
    Usage: python -c "from agent.observer import quick_snapshot; quick_snapshot()"
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        # Auto-login if on login page
        if page.url.rstrip("/") == url.rstrip("/"):
            try:
                page.fill("input[name=username]", "agent")
                page.fill("input[name=password]", "bankpass")
                page.click("button[type=submit]")
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
        elements = observe_page(page)
        print(f"Total elements: {len(elements)}\n")
        print(format_for_prompt(elements))
        browser.close()
