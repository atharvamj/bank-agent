"""
FastAPI capability API — stretch goal.
Exposes saved capabilities and invokes them via the same replay engine.

Endpoints:
  GET  /capabilities               → list all saved capabilities
  POST /capabilities/{name}/invoke → invoke with typed inputs, returns ReplayResult
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from artifacts.store import list_all, load_latest
from agent.evidence import EvidenceLogger


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="Bank Agent Capability API",
    description="Lists and invokes recorded automation capabilities.",
    version="1.0.0",
    lifespan=lifespan,
)


class InvokeRequest(BaseModel):
    inputs: dict[str, Any]
    headless: bool = True


class InvokeResponse(BaseModel):
    result_type: str          # "success" | "business_outcome" | "failure"
    outputs: dict[str, Any] | None = None
    label: str | None = None
    data: dict[str, Any] | None = None
    step: int | None = None
    expected: str | None = None
    observed: str | None = None
    evidence_path: str | None = None
    run_id: str | None = None


@app.get("/capabilities", response_model=list[dict])
async def list_capabilities():
    """List all saved capabilities with their schemas."""
    return list_all()


@app.post("/capabilities/{name}/invoke", response_model=InvokeResponse)
async def invoke_capability(name: str, request: InvokeRequest):
    """
    Invoke a saved capability by name. Launches Playwright, runs replay engine.
    This calls the SAME replay code path as the CLI — no duplication.
    """
    # Import here to avoid importing Playwright at module load time
    from playwright.sync_api import sync_playwright
    from replay.executor import replay, Success, BusinessOutcome, Failure

    try:
        capability = load_latest(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Capability '{name}' not found.")

    run_id = f"api_{str(uuid.uuid4())[:8]}"
    logger = EvidenceLogger(run_id, run_type="api_replay")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=request.headless)
            page = browser.new_page()
            page.goto(capability.metadata.get("target_url", "http://127.0.0.1:5001"))
            page.wait_for_load_state("networkidle", timeout=15000)

            result = replay(
                capability=capability,
                inputs=request.inputs,
                page=page,
                logger=logger,
            )
            browser.close()

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Replay error: {exc}")

    if isinstance(result, Success):
        return InvokeResponse(result_type="success", outputs=result.outputs, run_id=run_id)
    elif isinstance(result, BusinessOutcome):
        return InvokeResponse(result_type="business_outcome", label=result.label, data=result.data, run_id=run_id)
    else:  # Failure
        return InvokeResponse(
            result_type="failure",
            step=result.step,
            expected=result.expected,
            observed=result.observed,
            evidence_path=result.evidence_path,
            run_id=run_id,
        )
