"""
Ollama client — calls the local Ollama OpenAI-compatible chat API in JSON mode.
Validates every response against a strict Pydantic action schema.
Retries on parse failure up to MAX_RETRIES times.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:14b-instruct"
FALLBACK_MODELS = ["qwen2.5:7b-instruct", "gemma:latest"]
MAX_RETRIES = 2
REQUEST_TIMEOUT = 120  # seconds


class AgentAction(BaseModel):
    """
    The exact JSON structure the LLM must return for every step.
    One action per response — no multi-step planning.
    """
    action: Literal["click", "type", "wait_for", "assert_text", "done", "escalate"]
    target_description: str          # human-readable description of what to interact with
    value: str | None = None         # text to type, text to assert, or null
    frame: str | None = None         # "account-search" if target is inside iframe, else null
    reasoning: str                   # why this action moves toward the goal


class LLMParseError(Exception):
    """Raised when the LLM fails to produce valid JSON after all retries."""


def _available_models() -> list[str]:
    """Return list of locally available Ollama model names."""
    try:
        resp = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def probe_model(model: str) -> bool:
    """
    Send a simple JSON-mode probe to confirm the model can return valid JSON.
    Returns True on success.
    """
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            'Return ONLY this exact JSON, no other text: '
                            '{"action":"click","target_description":"test","value":null,'
                            '"frame":null,"reasoning":"probe"}'
                        ),
                    }
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        parsed = json.loads(content)
        # Validate it at least has action field
        return "action" in parsed
    except Exception:
        return False


def select_model() -> str:
    """
    Try DEFAULT_MODEL first, then fallbacks. Return first working model.
    Raises RuntimeError if none work.
    """
    available = set(_available_models())
    candidates = [DEFAULT_MODEL] + FALLBACK_MODELS

    for model in candidates:
        if model not in available:
            continue
        print(f"  [ollama] Probing model: {model} ...", flush=True)
        if probe_model(model):
            print(f"  [ollama] Selected model: {model}", flush=True)
            return model
        print(f"  [ollama] Model {model} failed JSON probe, trying next...", flush=True)

    raise RuntimeError(
        f"No working Ollama model found. Tried: {candidates}. "
        f"Available: {available}. Run: ollama pull qwen2.5:14b-instruct"
    )


class OllamaClient:
    def __init__(self, model: str | None = None):
        self.model = model or select_model()
        self._client = httpx.Client(timeout=REQUEST_TIMEOUT)

    def _call(self, messages: list[dict[str, str]]) -> str:
        """Raw API call, returns the content string."""
        resp = self._client.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.1},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def get_action(self, messages: list[dict[str, str]]) -> AgentAction:
        """
        Call the LLM and parse an AgentAction.
        Retries up to MAX_RETRIES times on parse failure.
        """
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = self._call(messages)
                data = json.loads(raw)
                return AgentAction.model_validate(data)
            except (json.JSONDecodeError, ValidationError, KeyError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    # Append a correction nudge to the conversation
                    messages = messages + [
                        {
                            "role": "assistant",
                            "content": raw if "raw" in dir() else "",
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Your response was not valid JSON matching the required schema. "
                                f"Error: {exc}. "
                                "Return ONLY a JSON object with keys: "
                                "action, target_description, value, frame, reasoning."
                            ),
                        },
                    ]
                    time.sleep(1)

        raise LLMParseError(
            f"LLM failed to produce valid AgentAction after {MAX_RETRIES + 1} attempts. "
            f"Last error: {last_exc}"
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
