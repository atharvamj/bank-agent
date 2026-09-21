"""
Artifact recorder — accumulates Steps during a live discovery run and
serializes a Capability on success.

Wired directly into the agent loop: call append_step() after each
successful action, then finalize() on goal completion.
"""

from __future__ import annotations

from datetime import datetime, timezone

from artifacts.schema import (
    Capability,
    LocatorStrategy,
    Outcome,
    Step,
    Target,
)
from artifacts.store import save


class Recorder:
    """Accumulates steps and builds the Capability artifact on success."""

    STANDARD_OUTCOMES = [
        Outcome(
            label="success",
            match="REVERSAL COMPLETE",
            is_success=True,
        ),
        Outcome(
            label="not_found",
            match="No records found",
            is_success=False,
        ),
        Outcome(
            label="already_reversed",
            match="Fee already reversed",
            is_success=False,
        ),
        Outcome(
            label="threshold_hold",
            match="Supervisor Approval Required",
            is_success=False,
        ),
        Outcome(
            label="permission_denied",
            match="Incorrect supervisor password",
            is_success=False,
        ),
    ]

    def __init__(self, goal: str, model: str, target_url: str, run_id: str):
        self.goal = goal
        self.model = model
        self.target_url = target_url
        self.run_id = run_id
        self._steps: list[Step] = []

    def append_step(
        self,
        action: str,
        target_description: str,
        frame: str | None,
        value: str | None,
    ) -> None:
        """Record one executed action as a Step."""
        # Build a primary LocatorStrategy from the LLM's description
        primary = LocatorStrategy(
            strategy="role",
            value=target_description,
            accessible_name=target_description,
        )
        # Always include a text_near fallback
        fallbacks = [
            LocatorStrategy(
                strategy="text_near",
                value=target_description,
            ),
        ]
        target = Target(frame=frame, primary=primary, fallbacks=fallbacks)
        step = Step(
            action=action,  # type: ignore[arg-type]
            target=target,
            value_template=_to_template(value),
        )
        self._steps.append(step)

    def finalize(
        self,
        capability_name: str,
        checkpoint_description: str,
        checkpoint_frame: str | None,
        input_schema: dict,
    ) -> Capability:
        """Build and persist the Capability artifact. Returns it."""
        desc = checkpoint_description or "REVERSAL COMPLETE"
        checkpoint = Target(
            frame=checkpoint_frame,
            primary=LocatorStrategy(
                strategy="text_near",
                value=desc,
            ),
        )
        cap = Capability(
            name=capability_name,
            version=1,
            description=self.goal,
            input_schema=input_schema,
            output_schema={
                "type": "object",
                "properties": {
                    "confirmed_text": {"type": "string"},
                    "account_id": {"type": "string"},
                },
            },
            steps=self._steps,
            checkpoint=checkpoint,
            outcomes=self.STANDARD_OUTCOMES,
            metadata={
                "goal": self.goal,
                "model": self.model,
                "target_url": self.target_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "discovery_run_id": self.run_id,
                "step_count": len(self._steps),
            },
        )
        save(cap)
        return cap

    @property
    def step_count(self) -> int:
        return len(self._steps)


def _to_template(value: str | None) -> str | None:
    """
    Convert a literal value to a template placeholder if it looks like
    a user-supplied parameter (account IDs, dates).
    Heuristic: purely numeric strings or date-like strings become {{...}}.
    """
    if value is None:
        return None
    if value.isdigit() and len(value) >= 4:
        return "{{account_id}}"
    # crude date check: contains / and short
    if "/" in value and len(value) <= 8:
        return "{{fee_date}}"
    return value
