"""
Operator CLI — lists pending interventions and signals resume.

Usage: python -m escalation.operator
"""

from __future__ import annotations

import sys

from escalation.handoff import list_pending, wait_for_resume


def main() -> None:
    pending = list_pending()

    if not pending:
        print("No pending interventions.", flush=True)
        return

    print(f"\nPending interventions ({len(pending)}):", flush=True)
    for i, rec in enumerate(pending):
        print(f"\n  [{i}] ID: {rec['intervention_id']}", flush=True)
        print(f"      Run:    {rec['run_id']}", flush=True)
        print(f"      Step:   {rec['step']}", flush=True)
        print(f"      Reason: {rec['reason']}", flush=True)
        print(f"      Since:  {rec['created_at']}", flush=True)
        if rec.get("cdp_url"):
            print(f"      CDP:    {rec['cdp_url']}", flush=True)
            print("      → Open chrome://inspect in a browser to attach", flush=True)
        if rec.get("screenshot_path"):
            print(f"      Screenshot: {rec['screenshot_path']}", flush=True)

    print("\nEnter intervention ID to resume (or 'all' to resume all, 'q' to quit): ", end="", flush=True)
    choice = input().strip()

    if choice == "q":
        return

    targets = []
    if choice == "all":
        targets = [r["intervention_id"] for r in pending]
    elif any(r["intervention_id"] == choice for r in pending):
        targets = [choice]
    else:
        print(f"Unknown intervention ID: {choice}", flush=True)
        return

    for iid in targets:
        result = wait_for_resume(iid)
        print(f"  Signaled resume for {iid}: {'OK' if result else 'not found (may have timed out)'}", flush=True)

    print("\nOperator CLI done. The automation will continue from the next step.", flush=True)


if __name__ == "__main__":
    main()
