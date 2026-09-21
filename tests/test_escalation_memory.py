"""
Tests for Escalation Memory — persistence, redaction, retrieval, and prompt injection.
"""

import json
from pathlib import Path
import pytest

from escalation.memory import EscalationMemoryItem, EscalationMemoryStore
from escalation.handoff import create_intervention, wait_for_resume
from agent.prompts import build_user_message


def test_record_and_load_memory(tmp_path: Path):
    store_file = tmp_path / "test_memory.json"
    store = EscalationMemoryStore(storage_path=store_file)

    assert store.load_all() == []

    item = store.record(
        trigger_reason="Supervisor approval required for reversal threshold",
        step=8,
        url="http://127.0.0.1:5001/account/88214",
        target_description="Reverse Fee",
        resolution_note="Operator approved override after verifying customer request",
        resolution_type="operator_resumed",
        run_id="run_123",
    )

    assert item.step == 8
    assert item.resolution_type == "operator_resumed"
    # Account 88214 should be redacted
    assert "88214" not in item.url_pattern
    assert "[REDACTED]" in item.url_pattern

    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].memory_id == item.memory_id
    assert loaded[0].trigger_reason == item.trigger_reason


def test_redaction_of_sensitive_data(tmp_path: Path):
    store_file = tmp_path / "test_memory.json"
    store = EscalationMemoryStore(storage_path=store_file)

    store.record(
        trigger_reason="User typed bankpass123 into Password for account 77301",
        step=2,
        url="http://127.0.0.1:5001/accounts/77301",
        target_description="Sign In with bankpass123",
        resolution_note="Account 77301 verified by operator",
        resolution_type="operator_resumed",
        sensitive_values=["bankpass123"],
    )

    items = store.load_all()
    assert len(items) == 1
    raw_json = store_file.read_text(encoding="utf-8")
    assert "77301" not in raw_json
    assert "bankpass123" not in raw_json
    assert "[REDACTED]" in raw_json


def test_find_relevant_ranking(tmp_path: Path):
    store_file = tmp_path / "test_memory.json"
    store = EscalationMemoryStore(storage_path=store_file)

    store.record(
        trigger_reason="Supervisor approval threshold",
        step=8,
        url="http://127.0.0.1:5001/account/[REDACTED]",
        target_description="Reverse Fee",
        resolution_note="Supervisor approved second reversal",
    )
    store.record(
        trigger_reason="Invalid credentials error",
        step=1,
        url="http://127.0.0.1:5001/login",
        target_description="Sign In",
        resolution_note="Re-entered credentials",
    )

    # Search from account page
    account_matches = store.find_relevant(
        url="http://127.0.0.1:5001/account/88214",
        target="Reverse Fee",
    )
    assert len(account_matches) >= 1
    assert account_matches[0].target_description == "Reverse Fee"

    # Search from login page
    login_matches = store.find_relevant(
        url="http://127.0.0.1:5001/login",
        target="Sign In",
    )
    assert len(login_matches) >= 1
    assert login_matches[0].target_description == "Sign In"

    # Search for non-existent page
    other_matches = store.find_relevant(
        url="http://127.0.0.1:5001/unknown/path",
        target="NonExistentButton",
    )
    assert len(other_matches) == 0


def test_format_for_prompt(tmp_path: Path):
    store_file = tmp_path / "test_memory.json"
    store = EscalationMemoryStore(storage_path=store_file)

    assert store.format_for_prompt([]) == ""

    item = store.record(
        trigger_reason="Supervisor approval required",
        step=8,
        url="http://127.0.0.1:5001/account/[REDACTED]",
        target_description="Reverse Fee",
        resolution_note="Approved supervisor override",
        resolution_type="operator_resumed",
    )

    formatted = store.format_for_prompt([item])
    assert "[PREVIOUS ESCALATION LESSONS]:" in formatted
    assert "Reverse Fee" in formatted
    assert "Approved supervisor override" in formatted
    assert "operator_resumed" in formatted


def test_build_user_message_includes_memory():
    elements_text = "0: button \"Reverse Fee\""
    history = ["click 'Member Accounts'", "type '88214'"]
    memories_text = "[PREVIOUS ESCALATION LESSONS]:\n- On /account: Approved supervisor override"

    msg = build_user_message(
        goal="Reverse fee",
        elements_text=elements_text,
        history=history,
        step_num=3,
        escalation_memories_text=memories_text,
    )

    assert "[PREVIOUS ESCALATION LESSONS]:" in msg
    assert "Approved supervisor override" in msg
    assert "GOAL: Reverse fee" in msg
    assert "STEP: 3" in msg


def test_handoff_wait_for_resume_records_memory(tmp_path: Path, monkeypatch):
    test_store = EscalationMemoryStore(storage_path=tmp_path / "handoff_memory.json")
    import escalation.handoff as handoff_mod
    monkeypatch.setattr(handoff_mod, "_MEMORY_STORE", test_store)

    # Trigger auto-resumed intervention (discovery pattern)
    resumed = create_intervention(
        reason="Test irreversible action",
        step=5,
        screenshot_path="/tmp/test.png",
        cdp_url="",
        run_id="run_test",
        auto_resume=True,
        url="http://127.0.0.1:5001/account/88214",
        target_description="Reverse Fee",
    )
    assert resumed is True

    # Memory should be automatically recorded
    memories = test_store.load_all()
    assert len(memories) == 1
    assert memories[0].resolution_type == "auto_approved"
    assert "88214" not in memories[0].url_pattern
    assert "[REDACTED]" in memories[0].url_pattern
