"""
Escalation handoff — creates intervention records, pauses the agent,
and waits for the operator to signal resume.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PENDING_DIR = Path(__file__).parent / "pending"
_RESUME_EVENTS: dict[str, threading.Event] = {}


def create_intervention(
    reason: str,
    step: int,
    screenshot_path: str,
    cdp_url: str,
    run_id: str,
    auto_resume: bool = False,
) -> bool:
    """
    Write an intervention record to /escalation/pending/ and pause the main thread.

    Parameters
    ----------
    reason          : human-readable description of why we're pausing
    step            : step index where escalation triggered
    screenshot_path : path to the screenshot taken at pause time
    cdp_url         : CDP debugging URL the operator should connect to
    run_id          : discovery/replay run identifier
    auto_resume     : if True, log but don't actually block (for discovery auto-approve)

    Returns True when the operator signals resume, False if aborted.
    """
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)

    intervention_id = str(uuid.uuid4())[:8]
    record = {
        "intervention_id": intervention_id,
        "run_id": run_id,
        "step": step,
        "reason": reason,
        "screenshot_path": screenshot_path,
        "cdp_url": cdp_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    record_path = _PENDING_DIR / f"{intervention_id}.json"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(f"\n{'='*60}", flush=True)
    print(f"  ⚠️  ESCALATION TRIGGERED (run={run_id}, step={step})", flush=True)
    print(f"  Reason: {reason}", flush=True)
    print(f"  Screenshot: {screenshot_path}", flush=True)
    if cdp_url:
        print(f"  CDP URL: {cdp_url}", flush=True)
        print(f"  → Attach via: chrome://inspect or playwright connect_over_cdp('{cdp_url}')", flush=True)
    print(f"  Intervention ID: {intervention_id}", flush=True)
    print(f"{'='*60}\n", flush=True)

    if auto_resume:
        _mark_resolved(record_path, "auto_approved")
        return True

    # Block until operator signals resume via the CLI
    event = threading.Event()
    _RESUME_EVENTS[intervention_id] = event
    print("  Waiting for operator... Run: python -m escalation.operator", flush=True)
    resumed = event.wait(timeout=600)  # 10 min operator timeout

    if resumed:
        _mark_resolved(record_path, "operator_resumed")
    else:
        _mark_resolved(record_path, "timed_out")

    del _RESUME_EVENTS[intervention_id]
    return resumed


def wait_for_resume(intervention_id: str) -> bool:
    """Called by the operator CLI to signal that the human is done."""
    event = _RESUME_EVENTS.get(intervention_id)
    if event:
        event.set()
        return True
    # If the main process has already moved on, just mark it resolved
    path = _PENDING_DIR / f"{intervention_id}.json"
    if path.exists():
        _mark_resolved(path, "late_signal")
    return False


def _mark_resolved(path: Path, status: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = status
        data["resolved_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def list_pending() -> list[dict]:
    """Return all pending intervention records."""
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(_PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") == "pending":
                records.append(data)
        except Exception:
            continue
    return records
