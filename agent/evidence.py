"""
Evidence logger — writes structured JSONL to /evidence/runs/<run_id>/.
All entries are redacted before write.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guardrails.redactor import redact_dict

_EVIDENCE_ROOT = Path(__file__).parent.parent / "evidence" / "runs"


class EvidenceLogger:
    def __init__(self, run_id: str, run_type: str = "discovery"):
        self.run_id = run_id
        self.run_type = run_type
        self._dir = _EVIDENCE_ROOT / run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._dir / f"{run_type}.jsonl"
        self._sensitive: list[str] = []
        self._step = 0

    def set_sensitive(self, values: list[str]) -> None:
        """Register values to be masked in all future log entries."""
        self._sensitive = [v for v in values if v]

    def log(self, entry_type: str, data: dict[str, Any]) -> None:
        """Append one redacted entry to the JSONL file."""
        self._step += 1
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "step_index": self._step,
            "type": entry_type,
            **data,
        }
        safe_record = redact_dict(record, self._sensitive)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(safe_record) + "\n")

    def save_screenshot(self, page_or_frame, label: str = "failure") -> Path:
        """Save a screenshot and return its path."""
        path = self._dir / f"{label}_{self._step}.png"
        page_or_frame.screenshot(path=str(path))
        return path

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def evidence_dir(self) -> Path:
        return self._dir
