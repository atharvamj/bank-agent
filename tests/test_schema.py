"""Tests for artifact schema round-trip and storage."""

import json
import os

import pytest

from artifacts.schema import Capability, LocatorStrategy, Outcome, Step, Target
from artifacts.store import load, save


def _make_capability(name: str = "test") -> Capability:
    return Capability(
        name=name,
        description="Test capability",
        steps=[
            Step(
                action="click",
                target=Target(
                    primary=LocatorStrategy(
                        strategy="role",
                        value="button",
                        role="button",
                        accessible_name="Sign In",
                    ),
                    fallbacks=[
                        LocatorStrategy(strategy="text_near", value="Sign In"),
                        LocatorStrategy(strategy="css", value="button[type=submit]"),
                    ],
                ),
            ),
            Step(
                action="type",
                target=Target(
                    primary=LocatorStrategy(
                        strategy="role",
                        value="textbox",
                        role="textbox",
                        accessible_name="Account Number",
                    )
                ),
                value_template="{{account_id}}",
            ),
        ],
        checkpoint=Target(
            primary=LocatorStrategy(strategy="text_near", value="REVERSAL COMPLETE")
        ),
        outcomes=[
            Outcome(label="success", match="REVERSAL COMPLETE", is_success=True),
            Outcome(label="not_found", match="No records found", is_success=False),
        ],
        input_schema={
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "properties": {"confirmed_text": {"type": "string"}},
        },
        metadata={"goal": "Test", "model": "test_model"},
    )


def test_schema_round_trip():
    cap = _make_capability()
    j = cap.model_dump_json()
    cap2 = Capability.model_validate_json(j)

    assert cap2.name == cap.name
    assert cap2.version == 1
    assert len(cap2.steps) == 2
    assert cap2.steps[0].target.primary.strategy == "role"
    assert cap2.steps[0].target.fallbacks[0].strategy == "text_near"
    assert cap2.steps[1].value_template == "{{account_id}}"
    assert cap2.checkpoint.primary.value == "REVERSAL COMPLETE"
    assert len(cap2.outcomes) == 2
    assert cap2.outcomes[0].is_success is True


def test_schema_save_load(tmp_path, monkeypatch):
    import artifacts.store as store_module
    monkeypatch.setattr(store_module, "_STORE_DIR", tmp_path / "saved")

    cap = _make_capability("save_test")
    path = save(cap)
    assert path.exists()

    loaded = load("save_test", 1)
    assert loaded.name == "save_test"
    assert len(loaded.steps) == 2
    assert loaded.outcomes[1].label == "not_found"


def test_schema_forbidden_action():
    """Step action must be one of the permitted literals."""
    with pytest.raises(Exception):
        Step(
            action="navigate",  # type: ignore
            target=Target(
                primary=LocatorStrategy(strategy="css", value="a")
            ),
        )
