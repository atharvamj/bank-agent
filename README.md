# Bank Agent — Computer-Use Automation System

An accessibility-tree-driven computer-use agent that discovers back-office UI tasks, records them as typed replayable **capability artifacts**, and replays them deterministically with guardrails and human escalation.

Built for interface.ai's take-home brief.

---

## Architecture Overview

```
mock_app/     Flask hostile-DOM bank app (target UI)
agent/        Observe → LLM decide → Act loop + Ollama client + recorder
artifacts/    Pydantic schema + JSON persistence for capability artifacts
replay/       Deterministic executor, locator resolver, outcome classifier
guardrails/   Shared allowlist YAML, risk classifier, redactor
escalation/   Intervention record, CDP handoff, operator CLI
capability_api/ FastAPI endpoint listing/invoking capabilities (stretch goal)
evidence/     JSONL logs + screenshots from real runs (committed)
tests/        pytest suite (29 tests)
```

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Ollama | Any recent version |
| macOS / Linux | (Playwright Chromium) |

> [!IMPORTANT]
> **macOS port 5000 conflict**: macOS Ventura/Sonoma runs **AirPlay receiver on port 5000** by default.
> Flask must use port **5001** on macOS (the mock app and all scripts already use 5001).
> On Linux, port 5000 is fine.
>
> **macOS IPv6 note**: on macOS, `localhost` resolves to IPv6 `::1` but Flask
> binds to `127.0.0.1` (IPv4) by default. All scripts in this repo use
> `http://127.0.0.1:5001` explicitly. Do **not** substitute `localhost`.

---

## Setup

### 1. Clone and install Python deps

```bash
git clone <your-repo-url>
cd bank-agent
pip install -r requirements.txt
playwright install chromium
```

### 2. Install and start Ollama

```bash
# Install from https://ollama.com if not already installed
ollama serve   # ensure it's running on localhost:11434

# Pull the target model (8.9 GB — Ollama resumes on network drops)
ollama pull qwen2.5:14b-instruct

# Fallback if RAM is constrained or network fails:
ollama pull qwen2.5:7b-instruct
# or use the pre-installed:
# ollama pull gemma:latest   (already present on the dev machine)
```

The agent auto-probes available models at startup and selects the best one found.

### 3. Run the mock bank app

```bash
# In a dedicated terminal — keep it running throughout
python -m flask --app mock_app.app run --host=127.0.0.1 --port=5001
```

Verify it works:
```bash
curl http://127.0.0.1:5001/health   # → {"status":"ok"}
```

---

## Demo Commands

### Discovery run (LLM drives the UI, records a capability)

```bash
python run.py discover
```

The agent will:
1. Probe the local Ollama model for JSON-mode reliability
2. Open a headless Chromium browser against `http://localhost:5000`
3. Log in, navigate to account search, look up **account 88214**, find the 3/12 overdraft fee, click Reverse Fee, and confirm the reversal
4. Save the capability artifact to `artifacts/saved/overdraft_fee_reversal_v1.json`
5. Write a JSONL evidence log to `evidence/runs/<run_id>/discovery.jsonl`

Add `--headed` to watch the browser in real time.

### Replay run (zero LLM calls)

```bash
python run.py replay
```

Reads the saved artifact, executes it step-by-step with no Ollama calls, and returns one of three result types: `Success`, `BusinessOutcome`, or `Failure`.

### Escalation / threshold-hold demo

```bash
# Terminal 1 — keep the mock app running
python -m flask --app mock_app.app run --host=127.0.0.1 --port=5001

# Terminal 2 — run replay in escalation demo mode
python run.py replay --force-second-reversal

# Terminal 3 — attach as the human operator when prompted
python -m escalation.operator
```

When the second reversal triggers the `threshold_hold`, the automation pauses, writes an intervention record, and prints a CDP URL. The operator CLI lists the intervention and signals resume.

### Print the accessibility tree

```bash
python run.py observer
```

### Run the test suite

```bash
pytest tests/ -v
```

### Start the capability API (stretch goal)

```bash
# In a separate terminal, with mock app already running:
uvicorn capability_api.main:app --port 8001
```

Then:
```bash
# List capabilities
curl http://localhost:8001/capabilities

# Invoke the overdraft reversal
curl -X POST http://localhost:8001/capabilities/overdraft_fee_reversal/invoke \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"account_id": "88214", "fee_date": "3/12"}, "headless": true}'
```

---

## Evidence

Pre-committed evidence from real runs lives in `/evidence/`:

| File | Contents |
|---|---|
| `evidence/runs/<disc_id>/discovery.jsonl` | Every step of a real discovery run |
| `evidence/runs/<rply_id>/replay.jsonl` | Every step of a real replay run |
| `artifacts/saved/overdraft_fee_reversal_v1.json` | The saved capability artifact |

---

## Credential Reference (mock app only)

| Role | Username | Password |
|---|---|---|
| Staff login | `agent` | `bankpass` |
| Supervisor approval | — | `sup3rvisor` |

---

## Project Decisions & Assumptions

See `REPORT.md` — specifically the **Cuts** section — for all documented assumptions and trade-offs.
