# Bank Agent

Computer-use automation system for legacy banking web UIs. Discovers tasks using an accessibility-tree LLM agent, records them as typed capability artifacts, and replays them deterministically with zero LLM calls, safety guardrails, and human escalation.

---

## Why Local LLMs (Ollama)?

This system relies exclusively on local, on-premise LLMs (via Ollama) rather than external APIs like OpenAI or Anthropic. For banking and credit union environments, **data security and privacy** are paramount. Running the model entirely on-premise ensures that sensitive financial data, customer PII, and internal UI structures never leave the institution's secure network. This completely eliminates the compliance, regulatory, and data-leakage risks associated with sending bank data to external third-party API providers.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Ollama | Recent release (`ollama serve` running on `localhost:11434`) |
| Chromium | Installed via Playwright |

> [!IMPORTANT]
> **macOS Port 5000 Conflict**: macOS AirPlay receiver listens on port 5000 by default and returns `403 Forbidden` to Playwright. The mock app and CLI scripts bind to port **5001**.
>
> **macOS IPv6 Note**: macOS resolves `localhost` to `::1`, but Flask binds to `127.0.0.1`. All scripts use `http://127.0.0.1:5001` explicitly. Do not use `localhost`.

---

## Setup

```bash
git clone https://github.com/atharvamj/bank-agent.git
cd bank-agent

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies and browser binary
pip install -r requirements.txt
playwright install chromium

# Start Ollama service (keep this running in a separate terminal)
ollama serve

# In your main terminal, pull the primary 14B model (8.9 GB)
ollama pull qwen2.5:14b-instruct
# Fallback if RAM < 16 GB: ollama pull qwen2.5:7b-instruct
```

---

## Running the System

### 1. Start the Mock Banking App

In a dedicated terminal:
```bash
python -m flask --app mock_app.app run --host=127.0.0.1 --port=5001
```
Verify:
```bash
curl http://127.0.0.1:5001/health   # -> {"status":"ok"}
```

### 2. Autonomous Discovery Run

Drives the hostile UI, overcomes dynamic form fields and iframes, and compiles the flow into `artifacts/saved/overdraft_fee_reversal_v1.json` (also auto-copied to `evidence/`):
```bash
python run.py discover
```
Add `--headed` to watch the browser in real time.

### 3. Deterministic Replay Run

Executes the recorded artifact step-by-step with **zero LLM calls**:
```bash
python run.py replay
```

### 4. Human Escalation & Memory Demo

Demonstrates the second-reversal threshold hold, human takeover via CDP, operator guidance recording, and PII-scrubbed memory persistence:
```bash
# Terminal 2: Run replay with forced second reversal
python run.py replay --force-second-reversal

# Terminal 3: Inspect and approve as operator
python -m escalation.operator
```

### 5. Run Test Suite

```bash
pytest tests/ -v
```

### 6. Capability API (Stretch Goal)

```bash
# Terminal 2: Start API service
uvicorn capability_api.main:app --port 8001

# Terminal 3: Invoke reversal via REST
# Note for Windows users: If using PowerShell, use `curl.exe` instead of `curl`
curl -X POST http://127.0.0.1:8001/capabilities/overdraft_fee_reversal/invoke \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"account_id": "88214", "fee_date": "3/12"}, "headless": true}'
```

---

## Credentials Reference (Mock App)

| Role | Username | Password | Notes |
|---|---|---|---|
| Staff Portal | `agent` | `bankpass` | Entered by discovery agent and replay engine |
| Supervisor Override | — | `sup3rvisor` | Used for manual threshold override over CDP |
