"""
Persistence helpers for Capability artifacts.
Storage: JSON files under /artifacts/saved/<name>_v<version>.json
"""

from __future__ import annotations

import json
from pathlib import Path

from artifacts.schema import Capability

_STORE_DIR = Path(__file__).parent / "saved"


def _ensure_dir() -> Path:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR


def save(capability: Capability) -> Path:
    """Persist a Capability to disk, returns the written path."""
    store = _ensure_dir()
    filename = f"{capability.name}_v{capability.version}.json"
    path = store / filename
    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")
    return path


def load(name: str, version: int = 1) -> Capability:
    """Load a Capability by name and version. Raises FileNotFoundError if missing."""
    path = _STORE_DIR / f"{name}_v{version}.json"
    if not path.exists():
        raise FileNotFoundError(f"Capability not found: {name} v{version} at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Capability.model_validate(data)


def load_latest(name: str) -> Capability:
    """Load the highest-versioned saved capability with the given name."""
    store = _ensure_dir()
    matches = sorted(store.glob(f"{name}_v*.json"))
    if not matches:
        raise FileNotFoundError(f"No saved capability named: {name}")
    data = json.loads(matches[-1].read_text(encoding="utf-8"))
    return Capability.model_validate(data)


def list_all() -> list[dict]:
    """Return summary dicts for all saved capabilities."""
    store = _ensure_dir()
    results = []
    for path in sorted(store.glob("*.json")):
        try:
            cap = Capability.model_validate_json(path.read_text(encoding="utf-8"))
            results.append({
                "name": cap.name,
                "version": cap.version,
                "description": cap.description,
                "input_schema": cap.input_schema,
                "steps": len(cap.steps),
                "created_at": cap.metadata.get("created_at"),
            })
        except Exception:  # noqa: BLE001
            continue
    return results
