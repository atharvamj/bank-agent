"""
Agent loop — observe → decide (Ollama) → act → record → repeat.

Runs a full discovery session and returns a DiscoveryResult.
Wires in: observer, ollama_client, recorder, guardrails, evidence logger.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Frame, Page

from agent.evidence import EvidenceLogger
from agent.observer import format_for_prompt, observe_page
from agent.ollama_client import AgentAction, LLMParseError, OllamaClient
from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.recorder import Recorder
from artifacts.schema import Capability
from guardrails import GuardrailViolation, Risk, check_action
from escalation.handoff import create_intervention, wait_for_resume
from escalation.memory import EscalationMemoryStore

MAX_STEPS = 30
WALL_TIMEOUT = 360  # seconds


@dataclass
class DiscoveryResult:
    success: bool
    capability: Capability | None = None
    failure_reason: str = ""
    steps_taken: int = 0
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


def _get_frame(page: Page, frame_name: str | None) -> Page | Frame:
    """Return the right frame/page to act on."""
    if not frame_name:
        return page
    for f in page.frames:
        if f.name == frame_name:
            return f
    return page  # fall back to main if not found


def _execute_action(page: Page, action: AgentAction) -> str:
    """
    Execute one AgentAction via Playwright.
    Returns a one-line description of what happened (for history).

    Strategy order for each action type is designed for hostile DOMs where
    accessible names may be missing or inferred from nearby text only.
    """
    frame = _get_frame(page, action.frame)
    desc = action.target_description
    val = action.value

    if action.action == "click":
        # Try multiple roles, then text, then partial-match
        for role in ("button", "link", "checkbox", "radio", "tab", "menuitem"):
            loc = frame.get_by_role(role, name=desc, exact=False)
            if loc.count() > 0:
                loc.first.click(timeout=10000)
                return f"click '{desc}' (role={role})"
        # Fall back to text-contains
        loc = frame.get_by_text(desc, exact=False)
        if loc.count() > 0:
            loc.first.click(timeout=10000)
            return f"click '{desc}' (text)"
        # Last: aria-label attribute
        frame.locator(f"[aria-label*='{desc}']").first.click(timeout=10000)
        return f"click '{desc}' (aria-label)"

    elif action.action == "type":
        d_lower = desc.lower()
        slug = d_lower.replace(" ", "").replace("-", "").replace("_", "")

        def _resolve_input():
            """Try strategies in order, return the first locator with count > 0."""
            # 1. input[name=<slug>] or input[id=<slug>] — most reliable on hostile DOM
            for attr in ("name", "id"):
                for s in (slug, d_lower, d_lower.replace(" ", "_")):
                    loc = frame.locator(f"input[{attr}*='{s}']")
                    if loc.count() > 0:
                        return loc
            # 2. Accessible name / label
            loc = frame.get_by_role("textbox", name=desc, exact=False)
            if loc.count() > 0:
                return loc
            # 3. Placeholder text
            loc = frame.get_by_placeholder(desc, exact=False)
            if loc.count() > 0:
                return loc
            # 4. Span/label proximity
            loc = frame.locator(f"span:has-text('{desc}') + input, span:has-text('{desc}') ~ input")
            if loc.count() > 0:
                return loc
            # 5. Positional — keyword-based ordinal mapping
            inputs = frame.locator("input:visible")
            n = inputs.count()
            if n > 0:
                if "username" in d_lower or "user" in d_lower and "password" not in d_lower:
                    return inputs.nth(0)
                if "password" in d_lower or "pass" in d_lower:
                    return inputs.nth(1) if n > 1 else inputs.nth(0)
                if "account" in d_lower or "number" in d_lower or "search" in d_lower or "q" == d_lower:
                    return inputs.nth(0)
                if "supervisor" in d_lower:
                    return inputs.nth(0)
                return inputs.first
            return None

        target_loc = _resolve_input()
        if target_loc is None:
            raise RuntimeError(f"Could not find any input field for '{desc}'")
        target_loc.first.fill(val or "", timeout=8000)
        return f"type '{val}' into '{desc}'"

    elif action.action == "wait_for":
        page.wait_for_timeout(400)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        if val:
            try:
                page.wait_for_selector(f"text={val}", timeout=10000)
            except Exception:
                try:
                    page.wait_for_selector(f":has-text('{val}')", timeout=5000)
                except Exception:
                    pass
        return f"wait_for '{val}'"

    elif action.action == "assert_text":
        content = page.content()
        if val and val not in content:
            raise AssertionError(f"Expected text not found: '{val}'")
        return f"assert_text '{val}' — found"

    return f"unknown action {action.action}"


def run_discovery(
    goal: str,
    page: Page,
    start_url: str,
    capability_name: str,
    input_schema: dict[str, Any],
    llm: OllamaClient,
    logger: EvidenceLogger,
    cdp_url: str = "",
    session_state: dict | None = None,
    memory_store: EscalationMemoryStore | None = None,
) -> DiscoveryResult:
    """
    Run the full discover → record loop.

    Parameters
    ----------
    goal             : str — the natural-language goal
    page             : Playwright Page
    start_url        : str — where to begin (already navigated before call)
    capability_name  : str — name for the saved artifact
    input_schema     : dict — JSON Schema for the capability's inputs
    llm              : OllamaClient instance
    logger           : EvidenceLogger
    cdp_url          : str — CDP URL for escalation handoff
    session_state    : dict — mutable state shared with guardrails
    memory_store     : EscalationMemoryStore | None — memory store for past escalation lessons
    """
    run_id = logger.run_id
    recorder = Recorder(
        goal=goal,
        model=llm.model,
        target_url=start_url,
        run_id=run_id,
    )

    memory_store = memory_store or EscalationMemoryStore()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history: list[str] = []
    session_state = session_state or {"reversal_counts": {}}
    start_time = time.time()

    logger.log("RUN_START", {"goal": goal, "model": llm.model, "start_url": start_url})

    for step_num in range(1, MAX_STEPS + 1):
        # Wall-clock timeout
        if time.time() - start_time > WALL_TIMEOUT:
            logger.log("TIMEOUT", {"step": step_num, "elapsed": time.time() - start_time})
            return DiscoveryResult(
                success=False,
                failure_reason=f"Wall-clock timeout after {WALL_TIMEOUT}s at step {step_num}",
                steps_taken=step_num,
                run_id=run_id,
            )

        # 1. Observe
        elements = observe_page(page)
        elements_text = format_for_prompt(elements)
        current_url = page.url

        logger.log("OBSERVE", {
            "step": step_num,
            "url": current_url,
            "element_count": len(elements),
        })

        # Check escalation memory for lessons learned
        relevant_memories = memory_store.find_relevant(url=current_url)
        memories_text = ""
        if relevant_memories:
            logger.log("MEMORY_HIT", {
                "step": step_num,
                "count": len(relevant_memories),
                "memory_ids": [m.memory_id for m in relevant_memories],
            })
            memories_text = memory_store.format_for_prompt(relevant_memories)

        # 2. Build prompt and call LLM
        user_msg = build_user_message(
            goal,
            elements_text,
            history,
            step_num,
            escalation_memories_text=memories_text,
        )
        messages_for_call = messages + [{"role": "user", "content": user_msg}]

        t0 = time.time()
        try:
            action = llm.get_action(messages_for_call)
        except LLMParseError as exc:
            logger.log("LLM_PARSE_ERROR", {"step": step_num, "error": str(exc)})
            screenshot_path = logger.save_screenshot(page, "llm_error")
            return DiscoveryResult(
                success=False,
                failure_reason=f"LLM parse failure at step {step_num}: {exc}",
                steps_taken=step_num,
                run_id=run_id,
            )

        llm_duration_ms = int((time.time() - t0) * 1000)
        logger.log("LLM_ACTION", {
            "step": step_num,
            "action": action.action,
            "target": action.target_description,
            "value": action.value,
            "frame": action.frame,
            "reasoning": action.reasoning,
            "duration_ms": llm_duration_ms,
        })

        # 3. Terminal conditions
        if action.action == "done":
            logger.log("GOAL_ACHIEVED", {"step": step_num, "target": action.target_description})
            cap = recorder.finalize(
                capability_name=capability_name,
                checkpoint_description=action.target_description or "REVERSAL COMPLETE",
                checkpoint_frame=action.frame,
                input_schema=input_schema,
            )
            return DiscoveryResult(
                success=True,
                capability=cap,
                steps_taken=step_num,
                run_id=run_id,
            )

        if action.action == "escalate":
            logger.log("ESCALATION_TRIGGERED", {"step": step_num, "reason": action.target_description})
            screenshot_path = logger.save_screenshot(page, "escalation")
            resumed = create_intervention(
                reason=action.target_description,
                step=step_num,
                screenshot_path=str(screenshot_path),
                cdp_url=cdp_url,
                run_id=run_id,
                url=current_url,
                target_description=action.target_description,
            )
            if resumed:
                history.append(f"[MANUAL INTERVENTION at step {step_num}] {action.target_description}")
                logger.log("MANUAL_ACTION", {"step": step_num, "resumed": True})
                continue
            return DiscoveryResult(
                success=False,
                failure_reason=f"Escalated at step {step_num}: {action.target_description}",
                steps_taken=step_num,
                run_id=run_id,
            )

        # 4. Guardrail check
        try:
            risk = check_action(
                action=action.action,
                target_label=action.target_description,
                current_url=current_url,
                session_state=session_state,
            )
        except GuardrailViolation as exc:
            logger.log("GUARDRAIL_VIOLATION", {"step": step_num, "violation": str(exc)})
            return DiscoveryResult(
                success=False,
                failure_reason=f"Guardrail violation at step {step_num}: {exc}",
                steps_taken=step_num,
                run_id=run_id,
            )

        if risk == Risk.IRREVERSIBLE:
            logger.log("IRREVERSIBLE_STEP", {"step": step_num, "action": action.action, "target": action.target_description})
            screenshot_path = logger.save_screenshot(page, "irreversible")
            create_intervention(
                reason=f"IRREVERSIBLE action requires approval: {action.target_description}",
                step=step_num,
                screenshot_path=str(screenshot_path),
                cdp_url=cdp_url,
                run_id=run_id,
                auto_resume=True,  # In discovery: auto-approve after logging
                url=current_url,
                target_description=action.target_description,
            )

        logger.log("GUARDRAIL_CHECK", {"step": step_num, "risk": risk.value})

        # 5. Execute action
        t0 = time.time()
        try:
            result_desc = _execute_action(page, action)
        except Exception as exc:
            logger.log("EXECUTE_ERROR", {"step": step_num, "error": str(exc)})
            screenshot_path = logger.save_screenshot(page, "execute_error")
            history.append(f"FAILED: {action.action} '{action.target_description}' — {exc}")
            # Don't abort — let the LLM try a different approach
            messages = messages + [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": action.model_dump_json()},
                {"role": "user", "content": f"That action failed: {exc}. Please try a different approach."},
            ]
            continue

        exec_duration_ms = int((time.time() - t0) * 1000)
        logger.log("EXECUTE_SUCCESS", {
            "step": step_num,
            "result": result_desc,
            "duration_ms": exec_duration_ms,
        })

        # 6. Record the step in the artifact
        recorder.append_step(
            action=action.action,
            target_description=action.target_description,
            frame=action.frame,
            value=action.value,
        )

        # Track reversal count for guardrails
        if "reverse" in action.target_description.lower() and action.action == "click":
            for acc_id in ["88214", "77301"]:  # known accounts
                if acc_id in page.url:
                    session_state["reversal_counts"][acc_id] = (
                        session_state["reversal_counts"].get(acc_id, 0) + 1
                    )

        # Append to conversation history
        history.append(f"{result_desc}")
        messages = messages + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": action.model_dump_json()},
        ]

        # Small yield to let page settle
        page.wait_for_timeout(400)

    # Exhausted step limit
    logger.log("STEP_LIMIT_EXCEEDED", {"max_steps": MAX_STEPS})
    return DiscoveryResult(
        success=False,
        failure_reason=f"Step limit ({MAX_STEPS}) exceeded without reaching goal.",
        steps_taken=MAX_STEPS,
        run_id=run_id,
    )
