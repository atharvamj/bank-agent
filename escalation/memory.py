"""
Escalation Memory — persists, redacts, and retrieves lessons learned from
human operator interventions and approvals across discovery and replay.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from guardrails.redactor import redact, redact_dict

_DEFAULT_STORAGE = Path(__file__).parent / "escalation_memory.json"


class EscalationMemoryItem(BaseModel):
    """One persisted record of a resolved escalation intervention."""

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    run_id: str = ""
    step: int = 0
    url_pattern: str = ""
    trigger_reason: str = ""
    target_description: str = ""
    resolution_note: str = ""
    resolution_type: str = "operator_resumed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EscalationMemoryStore:
    """Manages reading, writing, searching, and prompt formatting of escalation memories."""

    def __init__(self, storage_path: Path | str | None = None):
        if storage_path is None:
            self.storage_path = _DEFAULT_STORAGE
        else:
            self.storage_path = Path(storage_path)

    def record(
        self,
        trigger_reason: str,
        step: int = 0,
        url: str = "",
        target_description: str = "",
        resolution_note: str = "",
        resolution_type: str = "operator_resumed",
        run_id: str = "",
        sensitive_values: list[str] | None = None,
    ) -> EscalationMemoryItem:
        """
        Record a resolved escalation. Automatically redacts PII and account numbers
        before persisting to disk.
        """
        # Scrub all string fields to ensure no PII or sensitive patterns persist
        clean_url = redact(url, sensitive_values)
        clean_reason = redact(trigger_reason, sensitive_values)
        clean_target = redact(target_description, sensitive_values)
        clean_note = redact(resolution_note, sensitive_values)

        item = EscalationMemoryItem(
            run_id=run_id,
            step=step,
            url_pattern=clean_url,
            trigger_reason=clean_reason,
            target_description=clean_target,
            resolution_note=clean_note,
            resolution_type=resolution_type,
        )

        memories = self.load_all()
        memories.append(item)
        self._save_all(memories)
        return item

    def load_all(self) -> list[EscalationMemoryItem]:
        """Load all memories from storage file."""
        if not self.storage_path.exists():
            return []
        try:
            raw = self.storage_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return [EscalationMemoryItem.model_validate(d) for d in data]
        except Exception:
            return []

    def _save_all(self, items: list[EscalationMemoryItem]) -> None:
        """Persist items to disk formatted as JSON."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = [item.model_dump(mode="json") for item in items]
        self.storage_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")

    def clear(self) -> None:
        """Clear all stored memories."""
        if self.storage_path.exists():
            self.storage_path.unlink()

    def find_relevant(
        self,
        url: str = "",
        target: str = "",
        reason: str = "",
        limit: int = 3,
    ) -> list[EscalationMemoryItem]:
        """
        Find past escalation memories relevant to the current page state, target, or failure reason.
        Scores results using URL path match, target description match, and keyword overlap.
        """
        memories = self.load_all()
        if not memories:
            return []

        parsed_url = urlparse(url)
        url_path = parsed_url.path.strip("/") if url else ""

        scored: list[tuple[int, EscalationMemoryItem]] = []

        for item in memories:
            score = 0

            # 1. URL path matching (e.g. /account/ vs /account/[REDACTED])
            item_parsed = urlparse(item.url_pattern)
            item_path = item_parsed.path.strip("/")

            if url_path and item_path:
                if url_path == item_path:
                    score += 5
                elif any(seg and seg in item_path for seg in url_path.split("/")):
                    score += 3

            # 2. Target element match (e.g. "Reverse Fee")
            if target and item.target_description:
                t_lower = target.lower()
                m_lower = item.target_description.lower()
                if t_lower == m_lower:
                    score += 4
                elif t_lower in m_lower or m_lower in t_lower:
                    score += 2

            # 3. Reason or keyword match
            if reason and item.trigger_reason:
                r_words = set(reason.lower().split())
                m_words = set(item.trigger_reason.lower().split())
                overlap = r_words.intersection(m_words) - {"the", "a", "an", "at", "in", "to", "for", "is"}
                if overlap:
                    score += min(len(overlap), 3)

            # Any positive match qualifies
            if score > 0:
                scored.append((score, item))

        # Sort descending by score, then by recency
        scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [item for _, item in scored[:limit]]

    def format_for_prompt(self, memories: list[EscalationMemoryItem]) -> str:
        """Format matching memories into an actionable lessons-learned prompt block."""
        if not memories:
            return ""

        lines = ["[PREVIOUS ESCALATION LESSONS]:"]
        for mem in memories:
            loc_str = mem.url_pattern or "general"
            target_str = f" attempting '{mem.target_description}'" if mem.target_description else ""
            lines.append(f"- On {loc_str}{target_str}:")
            lines.append(f"  Trigger: {mem.trigger_reason}")
            note = mem.resolution_note or "Operator approved and resolved via CDP."
            lines.append(f"  Resolution ({mem.resolution_type}): {note}")

        return "\n".join(lines)
