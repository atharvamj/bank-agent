"""
Pydantic v2 artifact schema for saved capabilities.
Matches the brief spec exactly, with minor additions for drift tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LocatorStrategy(BaseModel):
    """One strategy for finding an element on the page."""
    strategy: Literal["role", "text_near", "css"]
    value: str
    role: str | None = None             # for role strategy: e.g. "button"
    accessible_name: str | None = None  # for role strategy


class Target(BaseModel):
    """A ranked list of ways to locate one UI element."""
    frame: str | None = None            # iframe name/index, None = top frame
    primary: LocatorStrategy
    fallbacks: list[LocatorStrategy] = Field(default_factory=list)


class Step(BaseModel):
    """One recorded action within a capability."""
    action: Literal["click", "type", "wait_for", "assert_text"]
    target: Target
    value_template: str | None = None   # e.g. "{{member_id}}", "{{date}}"

    # Drift tracking — set by replay engine, never by recorder
    last_strategy_used: str | None = None   # "primary" | "fallback_0" | "fallback_1" ...
    last_replayed_at: datetime | None = None


class Outcome(BaseModel):
    """A known terminal condition the replay engine checks after each step."""
    label: str          # "success" | "not_found" | "already_reversed" | "threshold_hold"
    match: str          # text substring or CSS selector signal
    is_success: bool


class Capability(BaseModel):
    """
    A fully-recorded, replayable automation capability.
    Produced by the agent discovery loop; consumed by the replay engine.
    """
    name: str
    version: int = 1
    description: str

    # JSON Schema dicts for the FastAPI capability_api typed contract
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    steps: list[Step]
    checkpoint: Target          # element confirming the final state was reached
    outcomes: list[Outcome]     # all known terminal conditions, checked at each step

    metadata: dict[str, Any] = Field(default_factory=dict)
    # metadata keys used internally:
    #   goal: str
    #   model: str
    #   target_url: str
    #   created_at: str (ISO-8601)
    #   discovery_run_id: str

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )
