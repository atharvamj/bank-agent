"""
Locator resolver — tries the primary locator strategy, then fallbacks.
Logs which strategy actually succeeded (drift-detection signal).
"""

from __future__ import annotations

from datetime import datetime, timezone

from playwright.sync_api import Frame, Locator, Page

from artifacts.schema import LocatorStrategy, Target


class LocatorExhaustedError(Exception):
    """Raised when all locator strategies for a Target have been exhausted."""
    def __init__(self, target: Target, tried: list[str]):
        self.target = target
        self.tried = tried
        super().__init__(
            f"All locator strategies exhausted. Tried: {tried}. "
            f"Primary: {target.primary}"
        )


def _resolve_input_by_name(frame: Page | Frame, label: str) -> Locator | None:
    """
    Resolve an input field by label string using the same multi-strategy
    chain used in _execute_action during discovery — CSS name/id first,
    then positional. This is the reliable path for hostile DOMs where inputs
    lack accessible names.
    """
    d_lower = label.lower()
    slug = d_lower.replace(" ", "").replace("-", "").replace("_", "")

    for attr in ("name", "id"):
        for s in (slug, d_lower, d_lower.replace(" ", "_")):
            loc = frame.locator(f"input[{attr}*='{s}']")
            if loc.count() > 0:
                return loc.first

    # Accessible name via ARIA
    loc = frame.get_by_role("textbox", name=label, exact=False)
    if loc.count() > 0:
        return loc.first

    # Placeholder text
    loc = frame.get_by_placeholder(label, exact=False)
    if loc.count() > 0:
        return loc.first

    # Span proximity
    loc = frame.locator(f"span:has-text('{label}') + input, span:has-text('{label}') ~ input")
    if loc.count() > 0:
        return loc.first

    # Positional keyword fallback
    inputs = frame.locator("input:visible")
    n = inputs.count()
    if n > 0:
        if "username" in d_lower or ("user" in d_lower and "password" not in d_lower):
            return inputs.nth(0)
        if "password" in d_lower or "pass" in d_lower:
            return inputs.nth(1) if n > 1 else inputs.nth(0)
        if any(k in d_lower for k in ("account", "number", "search", "q")):
            return inputs.nth(0)
        if "supervisor" in d_lower:
            return inputs.nth(0)

    return None


def _resolve_strategy(
    frame: Page | Frame,
    strategy: LocatorStrategy,
    action: str = "click",
    timeout_ms: int = 8000,
) -> Locator | None:
    """
    Try one strategy. Returns a Locator if at least one element is found,
    None otherwise.

    For 'type' actions on 'role' targets, delegates to _resolve_input_by_name
    to handle hostile-DOM anonymous textboxes (no accessible name in HTML).
    """
    try:
        if strategy.strategy == "role":
            name = strategy.accessible_name or strategy.value

            # For fill actions: try input[name=...] CSS first (hostile DOM safe)
            if action == "type":
                loc = _resolve_input_by_name(frame, name)
                if loc is not None:
                    return loc

            # For click actions: try declared role, then button/link fallbacks
            role = strategy.role or ""
            if role:
                loc = frame.get_by_role(role, name=name, exact=False)
                if loc.count() > 0:
                    return loc.first

            # Try common interactive roles
            for r in ("button", "link", "textbox", "checkbox", "radio", "tab"):
                loc = frame.get_by_role(r, name=name, exact=False)
                if loc.count() > 0:
                    return loc.first

        elif strategy.strategy == "text_near":
            # For type actions: avoid matching span labels as fill targets
            if action == "type":
                loc = _resolve_input_by_name(frame, strategy.value)
                if loc is not None:
                    return loc

            # For click actions: find by text, but filter to interactive elements
            for selector in (
                f"button:has-text('{strategy.value}')",
                f"a:has-text('{strategy.value}')",
                f"[role='button']:has-text('{strategy.value}')",
            ):
                loc = frame.locator(selector)
                if loc.count() > 0:
                    return loc.first

            # Accessible name match
            loc = frame.locator(f'[aria-label*="{strategy.value}"]')
            if loc.count() > 0:
                return loc.first

            # Generic text as last resort (may hit span labels — acceptable for clicks)
            loc = frame.get_by_text(strategy.value, exact=False)
            if loc.count() > 0:
                return loc.first

        elif strategy.strategy == "css":
            css_value = strategy.value
            loc = frame.locator(css_value)
            if loc.count() > 0:
                return loc.first

    except Exception:
        pass

    return None


def resolve_target(
    page: Page,
    target: Target,
    action: str = "click",
    timeout_ms: int = 8000,
) -> tuple[Locator, str]:
    """
    Resolve a Target to a Playwright Locator.

    Returns (locator, strategy_used) where strategy_used is one of:
      "primary", "fallback_0", "fallback_1", ...

    Raises LocatorExhaustedError if no strategy finds the element.
    """
    # Determine the right frame
    if target.frame:
        acting_frame: Page | Frame = page.main_frame
        for f in page.frames:
            if f.name == target.frame:
                acting_frame = f
                break
    else:
        acting_frame = page

    tried: list[str] = []

    # Try primary
    loc = _resolve_strategy(acting_frame, target.primary, action=action, timeout_ms=timeout_ms)
    if loc is not None:
        return loc, "primary"
    tried.append(f"primary:{target.primary.strategy}:{target.primary.value}")

    # Try fallbacks
    for i, fallback in enumerate(target.fallbacks):
        loc = _resolve_strategy(acting_frame, fallback, action=action, timeout_ms=timeout_ms)
        if loc is not None:
            return loc, f"fallback_{i}"
        tried.append(f"fallback_{i}:{fallback.strategy}:{fallback.value}")

    raise LocatorExhaustedError(target=target, tried=tried)
