"""
Deterministic replay executor.
Zero LLM calls. Executes a saved Capability step-by-step with:
  - Guardrail enforcement (shared module)
  - Fallback locator resolution
  - Outcome pattern matching after each step
  - Bounded retry/backoff for recoverable conditions
  - Three sealed result types: Success, BusinessOutcome, Failure
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Union

from playwright.sync_api import Page

from artifacts.schema import Capability, Outcome, Step
from guardrails import GuardrailViolation, Risk, check_action
from replay.locator import LocatorExhaustedError, resolve_target

# Recoverable wait: 3 attempts, exponential backoff
MAX_RETRIES = 3
BACKOFF_BASE = 1.0   # seconds


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Success:
    outputs: dict[str, Any]


@dataclass
class BusinessOutcome:
    label: str
    data: dict[str, Any]


@dataclass
class Failure:
    step: int
    expected: str
    observed: str
    evidence_path: str


ReplayResult = Union[Success, BusinessOutcome, Failure]


# ---------------------------------------------------------------------------
# Outcome detection
# ---------------------------------------------------------------------------

def _check_outcomes(page: Page, outcomes: list[Outcome]) -> Outcome | None:
    """
    Check if any declared Outcome pattern is present on the current page.
    Returns the first matching Outcome, or None.
    """
    try:
        content = page.content()
    except Exception:
        return None

    for outcome in outcomes:
        if outcome.match and outcome.match.lower() in content.lower():
            return outcome
    return None


def _check_checkpoint(page: Page, capability: Capability) -> bool:
    """Check if the checkpoint element/text is present."""
    cp = capability.checkpoint
    val = cp.primary.value if cp.primary else ""
    if not val:
        return True
    try:
        content = page.content()
        return val.lower() in content.lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step execution helpers
# ---------------------------------------------------------------------------

def _execute_step(page: Page, step: Step, inputs: dict[str, Any]) -> None:
    """
    Execute one Step using Playwright. No return value — raises on failure.
    Applies template substitution to value_template before acting.
    """
    value = step.value_template
    if value:
        for k, v in inputs.items():
            value = value.replace(f"{{{{{k}}}}}", str(v))

    locator, strategy_used = resolve_target(page, step.target, action=step.action)

    # Record which strategy fired (for drift tracking)
    step.last_strategy_used = strategy_used
    step.last_replayed_at = datetime.now(timezone.utc)

    if step.action == "click":
        locator.click(timeout=10000)

    elif step.action == "type":
        locator.fill(value or "", timeout=8000)

    elif step.action == "wait_for":
        if value:
            page.wait_for_selector(f"text={value}", timeout=12000)
        else:
            page.wait_for_load_state("networkidle", timeout=15000)

    elif step.action == "assert_text":
        content = page.content()
        if value and value not in content:
            raise AssertionError(f"Expected '{value}' not found on page.")


# ---------------------------------------------------------------------------
# Main replay function
# ---------------------------------------------------------------------------

def replay(
    capability: Capability,
    inputs: dict[str, Any],
    page: Page,
    logger=None,               # EvidenceLogger | None
    session_state: dict | None = None,
    approval_callback=None,    # callable(step_index) -> bool | None
) -> ReplayResult:
    """
    Execute a saved Capability deterministically.

    Parameters
    ----------
    capability        : the loaded Capability artifact
    inputs            : caller-supplied parameter values (substituted into templates)
    page              : live Playwright Page (already on start URL)
    logger            : optional EvidenceLogger for JSONL output
    session_state     : dict shared with guardrails
    approval_callback : called before IRREVERSIBLE steps; return False to abort

    Returns one of: Success, BusinessOutcome, Failure
    """
    session_state = session_state or {"reversal_counts": {}}
    current_url = page.url

    for step_idx, step in enumerate(capability.steps):
        step_num = step_idx + 1

        # ---- Guardrail check -------------------------------------------
        target_label = (
            step.target.primary.accessible_name
            or step.target.primary.value
            or ""
        )
        try:
            risk = check_action(
                action=step.action,
                target_label=target_label,
                current_url=current_url,
                session_state=session_state,
            )
        except GuardrailViolation as exc:
            if logger:
                logger.log("REPLAY_GUARDRAIL_VIOLATION", {
                    "step": step_num,
                    "violation": str(exc),
                })
            ev = logger.save_screenshot(page, f"guardrail_{step_num}") if logger else ""
            return Failure(
                step=step_num,
                expected="allowed action",
                observed=f"GuardrailViolation: {exc}",
                evidence_path=str(ev),
            )

        if risk == Risk.IRREVERSIBLE:
            if logger:
                logger.log("REPLAY_IRREVERSIBLE_STEP", {"step": step_num, "target": target_label})
            # Ask for approval
            approved = True
            if approval_callback:
                approved = approval_callback(step_num)
            if not approved:
                ev = logger.save_screenshot(page, f"blocked_{step_num}") if logger else ""
                return Failure(
                    step=step_num,
                    expected="supervisor approval",
                    observed="approval denied or missing",
                    evidence_path=str(ev),
                )

        # ---- Execute with retry/backoff ---------------------------------
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _execute_step(page, step, inputs)
                last_exc = None

                if logger:
                    logger.log("REPLAY_STEP_OK", {
                        "step": step_num,
                        "action": step.action,
                        "target": target_label,
                        "strategy_used": step.last_strategy_used,
                        "attempt": attempt,
                    })
                break

            except LocatorExhaustedError as exc:
                # Hard failure — no point retrying
                if logger:
                    logger.log("REPLAY_LOCATOR_EXHAUSTED", {
                        "step": step_num,
                        "tried": exc.tried,
                    })
                ev = logger.save_screenshot(page, f"locator_fail_{step_num}") if logger else ""
                return Failure(
                    step=step_num,
                    expected=f"element '{target_label}'",
                    observed="all locator fallbacks exhausted",
                    evidence_path=str(ev),
                )

            except Exception as exc:
                last_exc = exc
                # Check for known business outcomes before retrying
                outcome = _check_outcomes(page, capability.outcomes)
                if outcome and not outcome.is_success:
                    if logger:
                        logger.log("REPLAY_BUSINESS_OUTCOME", {
                            "step": step_num,
                            "label": outcome.label,
                            "match": outcome.match,
                        })
                    return BusinessOutcome(
                        label=outcome.label,
                        data={"step": step_num, "page_url": page.url},
                    )

                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE * (2 ** (attempt - 1))
                    if logger:
                        logger.log("REPLAY_RETRY", {
                            "step": step_num,
                            "attempt": attempt,
                            "wait_s": wait,
                            "error": str(exc),
                        })
                    time.sleep(wait)

        if last_exc is not None:
            if logger:
                logger.log("REPLAY_STEP_FAILED", {"step": step_num, "error": str(last_exc)})
            ev = logger.save_screenshot(page, f"step_fail_{step_num}") if logger else ""
            return Failure(
                step=step_num,
                expected=f"action {step.action} on '{target_label}' to succeed",
                observed=str(last_exc),
                evidence_path=str(ev),
            )

        # After each step: check all outcome patterns
        outcome = _check_outcomes(page, capability.outcomes)
        if outcome:
            if outcome.is_success:
                break   # success — will verify checkpoint below
            else:
                if logger:
                    logger.log("REPLAY_BUSINESS_OUTCOME", {
                        "step": step_num,
                        "label": outcome.label,
                    })
                return BusinessOutcome(
                    label=outcome.label,
                    data={"step": step_num, "page_url": page.url},
                )

        # Update URL for domain check
        try:
            current_url = page.url
        except Exception:
            pass

        page.wait_for_timeout(300)

    # All steps done — verify checkpoint
    if _check_checkpoint(page, capability):
        if logger:
            logger.log("REPLAY_SUCCESS", {
                "checkpoint": capability.checkpoint.primary.value,
                "url": page.url,
            })
        return Success(
            outputs={
                "confirmed_text": capability.checkpoint.primary.value,
                "account_id": inputs.get("account_id", ""),
                "page_url": page.url,
            }
        )

    # Checkpoint not found
    ev = logger.save_screenshot(page, "checkpoint_fail") if logger else ""
    return Failure(
        step=len(capability.steps),
        expected=capability.checkpoint.primary.value,
        observed="checkpoint not found on page",
        evidence_path=str(ev),
    )
