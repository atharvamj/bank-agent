# Bank Agent — Technical Report

> **Discovery goal**: "Log in as agent / bankpass, navigate to Account Search inside the iframe, look up account 88214, view the account detail, find the overdraft fee dated 3/12, click Reverse Fee, and confirm you have reached the reversal confirmation screen."

---

## Architecture

The system is a single synchronous Python process divided into eight cohesive layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│  mock_app/   Flask target — hostile DOM, iframe, 4 failure modes     │
├─────────────────────────────────────────────────────────────────────┤
│  agent/      Discovery loop: observe → LLM decide → act → record    │
│    observer.py      Playwright a11y snapshot, all frames             │
│    ollama_client.py JSON-mode chat, model probe, 2-retry parse       │
│    prompts.py       System + user prompt templates                   │
│    loop.py          30-step bounded loop, guardrail + escalation     │
│    recorder.py      Accumulates Steps, serializes Capability         │
│    evidence.py      Redacted JSONL logger, screenshots               │
├─────────────────────────────────────────────────────────────────────┤
│  artifacts/  Pydantic v2 schema + JSON persistence                   │
├─────────────────────────────────────────────────────────────────────┤
│  guardrails/ Allowlist YAML, risk classifier, redactor (SHARED)      │
├─────────────────────────────────────────────────────────────────────┤
│  replay/     Deterministic executor, locator fallback chain          │
│    locator.py       Tries primary → fallback_0 → fallback_1 …       │
│    executor.py      3 sealed result shapes, bounded retry            │
├─────────────────────────────────────────────────────────────────────┤
│  escalation/ threading.Event pause, CDP URL, operator CLI            │
├─────────────────────────────────────────────────────────────────────┤
│  capability_api/  FastAPI — lists + invokes via same replay path     │
└─────────────────────────────────────────────────────────────────────┘
```

### Discovery flow

1. `run.py discover` launches Chromium with `--remote-debugging-port=9222` and navigates to `http://localhost:5000`.
2. The agent loop calls `observe_page()`, which walks Playwright's accessibility snapshot across all frames (main + iframe named `account-search`), returning a flat list of `{role, name, frame, nearby_text}` dicts.
3. That list is formatted into a numbered text block and appended to a chat message sent to the local Ollama model in `format: "json"` mode.
4. The model returns one `AgentAction` (one action per turn). The response is validated against a strict Pydantic schema; on parse failure it is retried up to 2× with a correction nudge before hard-failing.
5. The action passes through the shared guardrail check (`check_action`) — allowlist enforcement then risk classification. `IRREVERSIBLE` actions log an intervention and, in discovery mode, auto-approve after logging.
6. The action is executed via Playwright. Errors feed back into the conversation as a correction message; the loop continues (LLM tries a different approach).
7. On `action == "done"`, the `Recorder` serializes a `Capability` JSON artifact and the loop returns `DiscoveryResult(success=True)`.

### Replay flow

`replay/executor.py::replay()` reads the saved `Capability`, iterates its `Step` list, resolves each `Target` through its fallback chain, checks every declared `Outcome` pattern after each step, and returns exactly one of `Success`, `BusinessOutcome`, or `Failure` — zero LLM calls.

---

## Artifact Schema

```
Capability
  name            str — machine-readable identifier
  version         int — monotonically increasing
  description     str — the original natural-language goal
  input_schema    dict — JSON Schema for caller-supplied params
  output_schema   dict — JSON Schema for what Success.outputs contains
  steps           list[Step]
    action          click | type | wait_for | assert_text
    target          Target
      frame           iframe name or null (top frame)
      primary         LocatorStrategy (strategy, value, role, accessible_name)
      fallbacks       list[LocatorStrategy] — tried in order at replay time
    value_template  str with {{placeholder}} substitution, or null
  checkpoint      Target — element/text confirming final state
  outcomes        list[Outcome]
    label           "success" | "not_found" | "already_reversed" | "threshold_hold" | …
    match           substring present on page when this outcome is active
    is_success      bool
  metadata        dict — goal, model, target_url, created_at, discovery_run_id
```

**Design rationale.** `Target` carries a ranked locator list rather than a single selector for two reasons: (1) hostile DOMs with no `data-testid` require multiple fallback strategies to be robust, and (2) the strategy that fires at replay time is logged per run — a rising fallback rate is the drift signal for multi-tenant reuse (see Heterogeneity section). The `value_template` convention (`{{account_id}}`) decouples the recorded flow from concrete parameter values, making the capability directly reusable across calls with different inputs without re-recording.

Three locator strategies cover the space of hostile markup:
- `role` — most stable; uses Playwright `get_by_role(role, name=...)`, mapping directly to the accessibility tree
- `text_near` — `get_by_text(value)` plus `[aria-label*=value]`; works when the element has visible text but no semantic role
- `css` — last resort; breaks on DOM refactors, so always listed last

---

## Determinism & Error Handling

The replay engine is deterministic: given the same capability artifact and the same inputs, it produces the same result every time the UI is in the expected state. There are no LLM calls, no random seeds, no mutable global state beyond what the target application itself changes.

### Three sealed result types

```python
Success(outputs: dict)
BusinessOutcome(label: str, data: dict)
Failure(step: int, expected: str, observed: str, evidence_path: str)
```

The caller — a future AI agent — receives exactly one of these and can branch deterministically on `isinstance`. There is no exception to catch and no ambiguous string to parse.

### Error taxonomy

| Category | Detection | Response |
|---|---|---|
| **Business outcome** | `Outcome.match` substring found on page | Return `BusinessOutcome(label)` immediately — not an error |
| **Recoverable condition** | Step action raises a non-locator exception | Retry up to 3×, exponential backoff (1 s, 2 s, 4 s), log each retry |
| **Hard failure — locator** | `LocatorExhaustedError` after all fallbacks | Abort, return `Failure`, save screenshot |
| **Hard failure — checkpoint** | Checkpoint text absent after all steps succeed | Return `Failure` with `step=len(steps)` |
| **Guardrail violation** | `GuardrailViolation` raised by `check_action` | Hard stop, return `Failure`, save screenshot |

### LLM parse robustness (discovery only)

The Ollama client validates every response against `AgentAction` via Pydantic. On parse failure it appends a correction turn and retries up to 2×. After 3 total attempts it raises `LLMParseError`, which the agent loop converts to a `DiscoveryResult(success=False)`. A startup probe validates the selected model before the discovery loop starts.

---

## Heterogeneity & Multi-Tenant

The current implementation covers a single mock surface. The design is intentionally layered to extend without re-recording:

**Surface abstraction.** `Target` encodes *how to find an element* (role, accessible name, fallback strategies) not *where it is in the DOM*. The observer and executor are the only components that touch Playwright. Swapping them for a desktop accessibility API (Windows UI Automation, macOS AXUIElement) or a different web framework requires no changes to the schema or the replay engine — only a new observer and a new locator resolver implementation.

**Multi-tenant reuse.** Steps use `{{account_id}}`, `{{fee_date}}` templates, not literal values. A capability recorded against Tenant A's vendor instance runs against Tenant B's instance by passing different `inputs`. When the primary locator fails on Tenant B but a fallback succeeds, that is logged as a per-tenant signal, not a hard failure. In production this would accumulate into per-tenant locator overrides stored alongside the shared capability — the artifact already carries the `fallbacks` list to hold them.

**Drift detection.** `Step.last_strategy_used` (set at replay time) and `Step.last_replayed_at` are stored in the artifact on each replay. A monotone increase in `fallback_N` usage across replays for one tenant signals locator drift — that variant gets flagged for human review. This is the designed signal; the monitoring layer that reads it is not built (see Cuts).

**Iframe support.** The mock app's iframe (`account-search`) is handled identically to a legacy frameset: `Target.frame` names the frame, the locator resolver walks `page.frames` to find it, and the replay executor acts on that child frame. No iframe-specific code beyond the frame name lookup.

---

## Escalation & Handoff

### Detection

The agent loop and replay executor both enter `BLOCKED` on:
- A hard `Failure` (locator exhausted, checkpoint not met)
- An `IRREVERSIBLE` step that requires explicit approval (risk classifier returns `Risk.IRREVERSIBLE`)

### Routing

`escalation/handoff.py::create_intervention()` writes an intervention record to `escalation/pending/<uuid>.json` containing: `run_id`, `step`, `reason`, `screenshot_path`, `cdp_url`, `created_at`, `status=pending`.

### Handoff

The browser is launched with `--remote-debugging-port=9222`. The main automation thread blocks on a `threading.Event` (10-minute timeout). The operator CLI (`python -m escalation.operator`) reads the pending directory, prints the CDP URL, and waits for the operator to press Enter after completing the manual step in their browser (via `chrome://inspect` or any Playwright-based inspector).

### Resume

`wait_for_resume(intervention_id)` sets the threading.Event, unblocking the main loop. The loop re-runs `observe_page()` against the now-changed page and continues from the next step. The manual action is logged as a `MANUAL_ACTION` evidence entry distinct from automated steps.

**Scope note.** This is a minimal but genuine handoff: real pause, real control transfer over the live browser session via CDP, real resume. Full real-time co-browsing (a shared viewport visible in a web UI) is explicitly out of scope per the brief.

### Natural escalation trigger

The overdraft-reversal flow's **approval threshold** is the natural demo: a second reversal on account 88214 in the same session sets `REVERSAL_COUNTS["88214"] = 1`, which makes `classify_action("click", "Reverse Fee", session_state)` return `Risk.IRREVERSIBLE` via the threshold check, triggering the escalation. Run with `python run.py replay --force-second-reversal` and attach via `python -m escalation.operator`.

---

## Safety

### Allowlist (`guardrails/allowlist.yaml`)

Specifies `permitted_domains` (`localhost`, `127.0.0.1`) and `permitted_actions` (`click`, `type`, `wait_for`, `assert_text`). Both the agent loop and the replay executor call `check_action()` from the **same shared module** (`guardrails/enforce.py`) before executing any action. A violation raises `GuardrailViolation`, which is a hard stop — not a warning, not a retry. This is enforced identically in discovery and replay; the check is not implemented twice.

### Risk classification

`classify_action(action, target_label, session_state)` returns `Risk.SAFE` or `Risk.IRREVERSIBLE`. Irreversibility triggers:
- **Verb match**: target label contains `submit`, `confirm`, `delete`, `transfer`, `open account`, `authorize`, `approve`, `reverse`
- **Threshold check**: `session_state["reversal_counts"][account_id] >= 1` and the target label contains `reverse` — the second reversal is always `IRREVERSIBLE` regardless of verb match

`IRREVERSIBLE` steps in **discovery** are auto-approved after logging (so the agent can complete the flow). In **replay**, they pause and call the `approval_callback` (which triggers the escalation handoff).

### Redaction

`guardrails/redactor.py::redact()` masks any 5+-digit numeric sequence (`\b\d{5,}\b`) and any explicitly listed sensitive value (e.g., typed passwords) with `[REDACTED]` before any write to `/evidence/` or logs. `redact_dict()` recurses through nested evidence dicts. Account numbers like `88214` never appear in committed logs.

---

## Cuts

**macOS port conflicts.** On macOS Ventura/Sonoma, the system's **AirPlay receiver occupies port 5000** (`Server: AirTunes`), intercepting all connections before Flask can respond. This causes Playwright/Chromium to receive a `403 Forbidden` and render an empty page. All scripts use port **5001** as a consequence. Additionally, on macOS, `localhost` resolves to IPv6 `::1` but Flask's development server binds to `127.0.0.1` (IPv4) by default. All navigation uses `http://127.0.0.1:5001` explicitly. Both constraints are documented prominently in the README because they are non-obvious environmental requirements for graders on macOS.

**Model selection.** `qwen2.5:14b-instruct` is the primary target. The startup probe (`ollama_client.select_model()`) tests JSON-mode reliability and falls back to `qwen2.5:7b-instruct`, then `gemma:latest` if the preferred model is absent or unreliable. The fallback chain is documented here because the grader's machine may have a different model available.

**One action per LLM call.** The brief warns that local models are weaker than frontier models at structured output. The action schema is deliberately minimal (6 keys, one action per turn) rather than multi-step planning. This trades throughput for reliability — a 14B model consistently produces valid single-action JSON; multi-step plans reliably fail at step 3.

**Recorder heuristic for templates.** The recorder converts typed values to `{{account_id}}` or `{{fee_date}}` by heuristic (≥4 digits → `account_id`, contains `/` and ≤8 chars → `fee_date`). A production version would ask the caller to declare which arguments are parameterized at recording time rather than inferring it post-hoc.

**No real operator UI.** The human handoff uses a bare CLI that reads `/escalation/pending/` and prints a CDP URL. A production version would need a shared browser view — the operator seeing the exact live page state without a URL copy-paste step.

**No multi-tenant implementation.** The design above (fallback locator overrides, per-tenant drift tracking, `{{template}}` reuse) is fully designed but not built. The brief's own Section 3.7 explicitly scopes this to design-only for a single surface.

**No confidence scoring.** The brief's optional stretch goal of gating artifact publication on LLM confidence scores was not chosen. The agent-facing capability API was picked instead as the single stretch goal, as it directly demonstrates the "artifact becomes a callable capability" through-line in the brief.

**Retry/backoff is fixed.** 3 attempts, exponential backoff at 1 s / 2 s / 4 s. A production version would tune these against real traffic distributions.

**Session state is in-process.** `REVERSAL_COUNTS` in `mock_app/data.py` is a module-level dict that resets on server restart. This is intentional — the threshold is designed to be a within-session guardrail, not a durable ledger. A real bank back-office would persist this in a database.
