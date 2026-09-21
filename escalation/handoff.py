"""
Escalation handoff — creates intervention records, pauses the agent,
and waits for the operator to signal resume.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from escalation.memory import EscalationMemoryStore

_PENDING_DIR = Path(__file__).parent / "pending"
_RESUME_EVENTS: dict[str, threading.Event] = {}
_MEMORY_STORE = EscalationMemoryStore()


def create_intervention(
    reason: str,
    step: int,
    screenshot_path: str,
    cdp_url: str,
    run_id: str,
    auto_resume: bool = False,
    url: str = "",
    target_description: str = "",
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
        "url": url,
        "target_description": target_description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "resolution_note": "",
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
        _mark_resolved(record_path, "auto_approved", resolution_note="Auto-approved irreversible action during discovery")
        return True

    # Block until operator signals resume via the CLI or in-process event
    event = threading.Event()
    _RESUME_EVENTS[intervention_id] = event
    print("  Waiting for operator... Run: python -m escalation.operator", flush=True)

    start_time = time.time()
    resumed = False
    timeout = 600
    while time.time() - start_time < timeout:
        if event.is_set():
            resumed = True
            break
        if record_path.exists():
            try:
                data = json.loads(record_path.read_text(encoding="utf-8"))
                if data.get("status") == "operator_resumed":
                    resumed = True
                    break
                elif data.get("status") in ("aborted", "rejected"):
                    resumed = False
                    break
            except Exception:
                pass
        time.sleep(0.5)

    if resumed:
        _mark_resolved(record_path, "operator_resumed")
    else:
        _mark_resolved(record_path, "timed_out")

    _RESUME_EVENTS.pop(intervention_id, None)
    return resumed


def wait_for_resume(intervention_id: str, resolution_note: str = "") -> bool:
    """Called by the operator CLI to signal that the human is done."""
    path = _PENDING_DIR / f"{intervention_id}.json"
    if path.exists():
        _mark_resolved(path, "operator_resumed", resolution_note=resolution_note)

    event = _RESUME_EVENTS.get(intervention_id)
    if event:
        event.set()
        return True
    return path.exists()


def _mark_resolved(path: Path, status: str, resolution_note: str = "") -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        already_resolved = data.get("status") in ("operator_resumed", "auto_approved") and status == data.get("status")
        data["status"] = status
        data["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if resolution_note:
            data["resolution_note"] = resolution_note
        final_note = data.get("resolution_note") or (
            "Auto-approved irreversible action during discovery"
            if status == "auto_approved"
            else "Operator approved intervention via CDP"
            if status == "operator_resumed"
            else status
        )
        data["resolution_note"] = final_note
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Record into escalation memory store if resolved and not already recorded
        if status in ("operator_resumed", "auto_approved") and not already_resolved:
            _MEMORY_STORE.record(
                trigger_reason=data.get("reason", ""),
                step=data.get("step", 0),
                url=data.get("url", ""),
                target_description=data.get("target_description", ""),
                resolution_note=final_note,
                resolution_type=status,
                run_id=data.get("run_id", ""),
            )
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
