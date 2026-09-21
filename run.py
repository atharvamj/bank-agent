#!/usr/bin/env python3
"""
run.py — CLI entry point for the bank agent system.

Commands:
  discover   Run the LLM-driven discovery loop (produces a capability artifact)
  replay     Execute a saved capability deterministically (zero LLM calls)
  observer   Print the accessibility tree snapshot for the running mock app

Examples:
  # Start the mock app first in another terminal:
  #   python -m flask --app mock_app.app run --host=127.0.0.1 --port 5001

  # Pull the target model (first time only):
  #   ollama pull qwen2.5:14b-instruct

  # Discovery run:
  python run.py discover

  # Replay the saved capability:
  python run.py replay

  # Trigger second-reversal escalation demo:
  python run.py replay --force-second-reversal

  # Print accessibility tree:
  python run.py observer
"""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from pathlib import Path

GOAL = (
    "Log in as agent / bankpass. "
    "Navigate to the Account Search. "
    "Look up account 88214. "
    "View the account detail. "
    "Find the overdraft fee dated 3/12 and click 'Reverse Fee'. "
    "Confirm you have reached the reversal confirmation screen."
)
CAPABILITY_NAME = "overdraft_fee_reversal"
TARGET_URL = "http://127.0.0.1:5001"
CDP_PORT = 9222


def cmd_discover(args: argparse.Namespace) -> None:
    """Run the LLM-driven discovery loop."""
    from playwright.sync_api import sync_playwright
    from agent.ollama_client import OllamaClient, select_model
    from agent.evidence import EvidenceLogger
    from agent.loop import run_discovery

    run_id = f"disc_{str(uuid.uuid4())[:8]}"
    model = args.model or None  # None triggers auto-selection

    print(f"\n{'='*60}")
    print(f"  DISCOVERY RUN  id={run_id}")
    print(f"  Goal: {GOAL[:80]}...")
    print(f"{'='*60}\n")

    logger = EvidenceLogger(run_id, run_type="discovery")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            args=[f"--remote-debugging-port={CDP_PORT}"],
        )
        page = browser.new_page()

        cdp_url = f"http://127.0.0.1:{CDP_PORT}"
        print(f"  Browser CDP URL: {cdp_url}", flush=True)

        page.goto(TARGET_URL, wait_until="networkidle")

        with OllamaClient(model=model) as llm:
            result = run_discovery(
                goal=GOAL,
                page=page,
                start_url=TARGET_URL,
                capability_name=CAPABILITY_NAME,
                input_schema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Member account number"},
                        "fee_date": {"type": "string", "description": "Fee date e.g. 3/12"},
                    },
                    "required": ["account_id", "fee_date"],
                },
                llm=llm,
                logger=logger,
                cdp_url=cdp_url,
            )

        browser.close()

    print(f"\n{'='*60}")
    if result.success:
        cap = result.capability
        
        import shutil
        src_path = Path("artifacts/saved") / f"{CAPABILITY_NAME}_v{cap.version}.json"
        if src_path.exists():
            shutil.copy(src_path, Path("evidence") / src_path.name)

        print(f"  ✅  DISCOVERY SUCCEEDED in {result.steps_taken} steps")
        print(f"  Capability saved: {CAPABILITY_NAME}_v{cap.version}.json (copied to evidence/)")
        print(f"  Evidence log: {logger.log_path}")
    else:
        print(f"  ❌  DISCOVERY FAILED: {result.failure_reason}")
        print(f"  Evidence log: {logger.log_path}")
    print(f"{'='*60}\n")


def cmd_replay(args: argparse.Namespace) -> None:
    """Replay a saved capability deterministically."""
    from playwright.sync_api import sync_playwright
    from artifacts.store import load_latest
    from agent.evidence import EvidenceLogger
    from replay.executor import replay, Success, BusinessOutcome, Failure
    from escalation.handoff import create_intervention

    run_id = f"rply_{str(uuid.uuid4())[:8]}"
    logger = EvidenceLogger(run_id, run_type="replay")

    try:
        cap = load_latest(CAPABILITY_NAME)
    except FileNotFoundError:
        print(f"ERROR: No saved capability '{CAPABILITY_NAME}'. Run discovery first.")
        sys.exit(1)

    # If forcing the second-reversal escalation demo, reset the data first
    if args.force_second_reversal:
        _reset_and_prime_reversal()

    inputs = {
        "account_id": args.account_id or "88214",
        "fee_date": args.fee_date or "3/12",
    }

    print(f"\n{'='*60}")
    print(f"  REPLAY RUN  id={run_id}  capability={CAPABILITY_NAME}")
    print(f"  Inputs: {inputs}")
    if args.force_second_reversal:
        print("  Mode: FORCE SECOND REVERSAL (escalation demo)")
    print(f"{'='*60}\n")

    session_state: dict = {"reversal_counts": {}}

    def approval_callback(step_num: int, acting_page=None) -> bool:
        p = acting_page or page
        screenshot_path = logger.evidence_dir / f"blocked_{step_num}.png"
        try:
            p.screenshot(path=str(screenshot_path))
            print(f"  📸 Escalation screenshot captured: {screenshot_path}", flush=True)
        except Exception as exc:
            print(f"  [warn] Failed to capture escalation screenshot: {exc}", flush=True)

        cdp_url = f"http://127.0.0.1:{CDP_PORT}" if not args.no_cdp else ""
        resumed = create_intervention(
            reason="Irreversible step requires supervisor approval",
            step=step_num,
            screenshot_path=str(screenshot_path),
            cdp_url=cdp_url,
            run_id=run_id,
            auto_resume=False,  # genuine pause in replay
            url=p.url,
            target_description="Reverse Fee",
        )
        return resumed

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            args=[f"--remote-debugging-port={CDP_PORT}"],
        )
        page = browser.new_page()
        page.goto(TARGET_URL, wait_until="networkidle")

        result = replay(
            capability=cap,
            inputs=inputs,
            page=page,
            logger=logger,
            session_state=session_state,
            approval_callback=approval_callback if args.force_second_reversal else None,
        )
        browser.close()

    print(f"\n{'='*60}")
    if isinstance(result, Success):
        print(f"  ✅  REPLAY SUCCESS")
        print(f"  Outputs: {result.outputs}")
    elif isinstance(result, BusinessOutcome):
        print(f"  ℹ️   BUSINESS OUTCOME: {result.label}")
        print(f"  Data: {result.data}")
    else:
        print(f"  ❌  REPLAY FAILURE at step {result.step}")
        print(f"  Expected: {result.expected}")
        print(f"  Observed: {result.observed}")
        print(f"  Evidence: {result.evidence_path}")
    print(f"  Log: {logger.log_path}")
    print(f"{'='*60}\n")


def cmd_observer(args: argparse.Namespace) -> None:
    """Print the accessibility tree snapshot for the mock app."""
    from playwright.sync_api import sync_playwright
    from agent.observer import observe_page, format_for_prompt

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        # Login first
        page.goto(TARGET_URL)
        page.fill("input[name=username]", "agent")
        page.fill("input[name=password]", "bankpass")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        elements = observe_page(page)
        print(format_for_prompt(elements))
        browser.close()


def _reset_and_prime_reversal() -> None:
    """
    Reset mock app state and prime account 88214 so the next reversal
    triggers the threshold hold (simulates a second reversal).
    """
    from mock_app.data import REVERSAL_COUNTS
    REVERSAL_COUNTS["88214"] = 1
    print("  [demo] Primed REVERSAL_COUNTS['88214'] = 1 (next reversal will trigger threshold hold)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bank agent CLI")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed (visible) mode")
    parser.add_argument("--no-cdp", action="store_true", help="Disable CDP port (escalation demo only)")
    sub = parser.add_subparsers(dest="command")

    # discover
    disc = sub.add_parser("discover", help="Run LLM discovery loop")
    disc.add_argument("--model", default=None, help="Ollama model to use (default: auto-select)")

    # replay
    rply = sub.add_parser("replay", help="Replay saved capability")
    rply.add_argument("--account-id", default="88214")
    rply.add_argument("--fee-date", default="3/12")
    rply.add_argument("--force-second-reversal", action="store_true",
                      help="Prime state so next reversal triggers escalation demo")

    # observer
    sub.add_parser("observer", help="Print accessibility tree snapshot")

    args = parser.parse_args()

    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "observer":
        cmd_observer(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
