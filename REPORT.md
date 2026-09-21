# Bank Agent — Technical Report

> **Discovery Goal**: Log in as `agent / bankpass`, navigate to Account Search inside the iframe, look up account `88214`, view account detail, find the $35 overdraft fee dated 3/12, click Reverse Fee, and confirm reaching the reversal confirmation screen.

---

## Architecture

I structured the system as a single synchronous Python process with eight distinct components:

```
mock_app/       Flask bank portal — nested tables, dynamic form names, iframe, threshold hold
agent/          Discovery loop: observe -> LLM decide -> act -> record -> memory injection
artifacts/      Pydantic v2 schema and JSON serialization for capability artifacts
guardrails/     Shared domain allowlist, action risk classifier, and regex redactor
replay/         Deterministic executor, 4-tier fallback locator, outcome classifier
escalation/     threading.Event pause, CDP port 9222 exposure, operator CLI, and memory store
capability_api/ FastAPI service exposing artifact invocation via REST
tests/          pytest suite covering all failure modes, classifiers, and storage round-trips
```

### Discovery Flow
I chose accessibility-tree targeting over raw vision or coordinate clicking. Raw coordinates break across display resolutions, and feeding whole screenshots to local 14B models on unified memory creates prohibitive latency (~15–30s per inference) while frequently misidentifying pixel boundaries on low-contrast legacy UIs. Instead, `agent/observer.py` walks Playwright's accessibility snapshot across both the main document and the nested `<iframe name="account-search">`, producing a flattened list of interactive elements (`role`, `name`, `frame`).

I initially attempted to have the LLM output multi-step plans to minimize turn latency, but `qwen2.5:14b-instruct` consistently hallucinated or misordered steps after step 2. I dropped multi-step planning in favor of an atomic, single-action turn loop (`AgentAction`: action, target description, value, frame, reasoning). Each step validates against Pydantic. If parsing fails, the error message appends to the conversation history and retries up to twice.

Each chosen action passes through `guardrails/enforce.py::check_action()`. Actions classified as `Risk.IRREVERSIBLE` (such as clicking "Reverse Fee") trigger an intervention log and snapshot, which auto-approves in discovery so the agent can discover the complete path. When the agent observes the confirmation screen and returns `action: "done"`, `agent/recorder.py` writes the recorded sequence to `artifacts/saved/overdraft_fee_reversal_v1.json`.

### Replay Flow
`replay/executor.py::replay()` reads the saved artifact and executes it deterministically without calling Ollama. It substitutes caller-supplied runtime inputs (`account_id: "88214"`, `fee_date: "3/12"`) into templated step values, resolves targets through a multi-tier fallback locator, and evaluates declared outcome patterns after each action.

---

## Artifact Schema

I modeled the capability artifact in Pydantic v2 (`artifacts/schema.py`) to serve as an immutable execution contract between discovery and replay:

```
Capability
  name            str — identifier (e.g. "overdraft_fee_reversal")
  version         int — monotonically increasing version number
  description     str — natural-language goal
  input_schema    dict — JSON Schema for runtime parameters
  output_schema   dict — JSON Schema for confirmed outputs
  steps           list[Step]
    action          click | type | wait_for | assert_text
    target          Target
      frame           str | null — iframe identifier ("account-search")
      primary         LocatorStrategy (strategy, value, role, accessible_name)
      fallbacks       list[LocatorStrategy] — tried in priority order
    value_template  str | null — parameterized input string e.g. "{{account_id}}"
  checkpoint      Target — confirmation checkpoint verifying final page state
  outcomes        list[Outcome]
    label           str — "success" | "already_reversed" | "threshold_hold" | "not_found"
    match           str — page text substring matching this outcome
    is_success      bool — whether this outcome satisfies business success
  metadata        dict — model, discovery run ID, target URL, timestamp
```

### Locator Strategy Design
I did not want to rely on single CSS selectors because the mock application deliberately generates random input IDs and names on every render (e.g. `name="user_8192"`). I defined four locator tiers inside `Target`:
1. `role`: Playwright's `get_by_role()` matching semantic role and accessible name.
2. `slug`: Input attribute matching stripping non-alphanumerics (`input[name*='username']`).
3. `text_near`: Text matching and `aria-label` attribute substring searches.
4. `css`: Structural selector used only as a last resort.

During replay, the engine records which locator tier successfully resolved each element. This provides the telemetry needed to detect UI drift before a locator breaks entirely.

---

## Determinism & Error Handling

Replay is fully deterministic: given the same capability artifact, identical inputs, and an identical server state, the execution path is fixed. There are zero LLM calls, no stochastic branching, and no external dependencies.

### Sealed Result Types
I avoided returning generic booleans or throwing raw Playwright exceptions to callers. The replay engine returns one of three sealed dataclass types:

```python
Success(outputs: dict[str, Any])
BusinessOutcome(label: str, data: dict[str, Any])
Failure(step: int, expected: str, observed: str, evidence_path: str)
```

This lets calling systems (such as the FastAPI endpoint or a workflow orchestrator) branch on type:
- `Success`: Reversal completed, confirmed by "REVERSAL COMPLETE" text.
- `BusinessOutcome`: The UI reached a recognized terminal state that is not a technical failure, such as `threshold_hold` ("Supervisor Approval Required") or `already_reversed` ("Fee already reversed").
- `Failure`: A technical breakdown (locator exhausted, timeout, or missing checkpoint).

### Error Taxonomy & Recovery

| Category | Trigger | Handling Policy |
|---|---|---|
| Business Outcome | Page text matches an `Outcome.match` pattern | Immediate exit with `BusinessOutcome(label)`. Not treated as an error. |
| Transient Network / DOM Lag | Action throws element-not-found or timeout | Bounded retry loop (up to 3 attempts) with backoff (1s, 2s, 4s). |
| Locator Exhaustion | All fallback locators fail on a step | Abort immediately, capture failure screenshot, return `Failure`. |
| Checkpoint Missing | Steps finish but checkpoint text absent | Abort, return `Failure` indicating checkpoint validation failed. |
| Guardrail Breach | Domain or action forbidden by allowlist | Hard stop with `Failure`, log violation to evidence. |

---

## Heterogeneity & Multi-Tenant

I designed the artifact and locator layers to generalize across different vendor UI instances without requiring code changes:

### Surface Abstraction
The schema decouples the goal from the DOM implementation. `Target` specifies semantic intents (`role="button"`, `accessible_name="Reverse Fee"`, `frame="account-search"`) rather than brittle DOM paths. If this system were retargeted at a Windows desktop app via Win32 or UI Automation, only `agent/observer.py` and `replay/locator.py` would need new driver implementations; the schema and replay engine logic would remain identical.

### Parameterization Across Tenants
Discovery records concrete values typed during discovery, but `agent/recorder.py` parameterizes them using `{{account_id}}` and `{{fee_date}}` templates. A capability discovered against Account 88214 on Tenant A executes on Tenant B simply by passing `{"account_id": "77301", "fee_date": "3/10"}` at runtime.

### Drift Tracking
Each replay step records `last_strategy_used` (e.g. `fallback_0` vs `primary`). If Tenant A always resolves on `primary` while Tenant B consistently falls back to `fallback_1`, that delta is tracked in the run telemetry. A tenant whose fallback rate crosses a threshold can be flagged for re-recording before the capability fails completely.

---

## Escalation & Handoff

### Detection & Trigger
Escalation occurs when an operation cannot proceed safely without human authority:
1. Hard locator failure where all recovery tiers are exhausted.
2. An action tagged as `Risk.IRREVERSIBLE` by policy.

The natural demo in this codebase is the **overdraft reversal threshold**:
A single $35 fee reversal on account 88214 is permitted. A second reversal on the same account in the same session increments `session_state["reversal_counts"]["88214"] >= 1`. At step 8, `guardrails/classifier.py` flags clicking "Reverse Fee" as `Risk.IRREVERSIBLE`.

### Live Handoff Protocol
When escalation triggers in replay:
1. The execution thread halts via a `threading.Event`.
2. An evidence screenshot is captured to `evidence/runs/<run_id>/blocked_<step>.png`.
3. An intervention ticket is written to `escalation/pending/<uuid>.json` containing the run ID, step, reason, screenshot path, and the Playwright CDP debugging URL (`http://localhost:9222`).
4. An operator attaches to the live session using `chrome://inspect` or Playwright inspector.
5. The operator runs `python -m escalation.operator`, reviews the pending intervention, optionally inputs resolution guidance, and signals resume.
6. The background thread unblocks and continues execution.

### Escalation Memory & Continuous Learning
I added `escalation/memory.py::EscalationMemoryStore` to close the feedback loop between human interventions and future automated runs without violating replay determinism:
- **Redacted Persistence**: When an operator resolves an escalation, the ticket, reason, target element, and operator note are written to `escalation/escalation_memory.json`. All sensitive account numbers matching `\b\d{5,}\b` are scrubbed to `[REDACTED]` via `guardrails/redactor.py::redact()`.
- **In-Context Prompt Injection**: During subsequent discovery runs, `agent/loop.py` queries `find_relevant(url, target)`. Matching historical interventions are injected as a `[PREVIOUS ESCALATION LESSONS]` guidance block directly in the LLM's user prompt, providing few-shot context on expected supervisor approvals or obstacle resolutions.
- **Audit Logging**: During replay, matching memories are surfaced in the logs as `REPLAY_MEMORY_HIT` with historical resolution notes, providing an auditable trail of why an approval was previously granted.

---

## Safety

### Allowlist Enforcement
`guardrails/allowlist.yaml` specifies allowed domains (`localhost`, `127.0.0.1`) and permitted actions (`click`, `type`, `wait_for`, `assert_text`). Both discovery and replay call the exact same `guardrails/enforce.py::check_action()` function. If the agent attempts to navigate to an external URL or execute an arbitrary script, the check raises `GuardrailViolation`, which immediately halts the process.

### Risk Classification
`guardrails/classifier.py::classify_action()` implements two safety checks:
1. **Verb matching**: Actions containing `submit`, `confirm`, `delete`, `transfer`, `authorize`, `approve`, or `reverse` are categorized as `Risk.IRREVERSIBLE`. Safe navigation actions like `click "Sign In"` or `click "View"` pass as `Risk.SAFE`.
2. **Stateful Thresholds**: If an account has already undergone a reversal in the current session (`reversal_counts[account_id] >= 1`), any subsequent reversal click is forced to `Risk.IRREVERSIBLE`.

In discovery mode, irreversible actions log an audit snapshot and auto-approve so the agent can discover the end-to-end flow. In replay mode, irreversible actions pause the browser and require operator confirmation via the escalation CLI.

### Data Redaction
`guardrails/redactor.py` masks sensitive financial patterns before any data is written to disk:
- Any 5+-digit numeric sequence (`\b\d{5,}\b`) is replaced with `[REDACTED]`.
- Explicit sensitive strings (e.g. passwords typed during authentication) are scrubbed.
- `redact_dict()` recursively scrubs structured dictionaries before writing JSONL evidence logs. Account `88214` never appears in raw logs or stored escalation memories.

---

## Cuts

I made several explicit scope reductions during implementation:

1. **macOS Port and IPv6 Adjustments**
   On macOS Sonoma, the system AirPlay receiver listens on port 5000 (`Server: AirTunes`), intercepting incoming connections and returning HTTP 403. I moved the mock application to port **5001**. Furthermore, macOS resolves `localhost` to IPv6 `::1` while Flask binds to IPv4 `127.0.0.1`. Chromium threw connection errors until I standardized all URLs to `http://127.0.0.1:5001`.

2. **Model Selection & Sizing**
   I targeted `qwen2.5:14b-instruct` (8.9 GB) because smaller models struggled with structured JSON compliance. On a 16 GB unified memory machine, cold-loading the 14B model takes ~12 seconds. I included an automated startup probe with fallback to `qwen2.5:7b-instruct` and `gemma:latest` in case of RAM constraints.

3. **Single-Action Turns vs. Multi-Step Planning**
   I initially planned to let the LLM generate multi-action sequences (e.g. fill username, fill password, click sign in within one prompt turn). During early testing, local 14B models frequently hallucinated form field names or lost track of iframe boundaries by the second sub-step. I cut multi-step planning and enforced one atomic action per turn.

4. **Regex Parameterization Heuristics**
   `agent/recorder.py` replaces 4+-digit numeric values with `{{account_id}}` and date strings containing `/` with `{{fee_date}}`. This heuristic works for this specific banking flow, but is too brittle for production. In a commercial implementation, the operator should explicitly declare input parameters at recording time.

5. **Bare CLI Instead of Shared Web UI**
   The escalation mechanism uses a terminal CLI reading JSON files in `escalation/pending/` and directing the operator to `chrome://inspect`. A production system would require a real-time web-based co-browsing interface with WebRTC canvas streaming.

6. **Static Retry Limits**
   The retry limit of 3 attempts with exponential backoff (1s, 2s, 4s) was an educated guess, not tuned against real production latency distributions.

7. **In-Memory Session State**
   `mock_app/data.py` stores `REVERSAL_COUNTS` in a module-level dictionary. In a production core banking system, reversal thresholds are tracked in an ACID database across distributed cluster nodes.
