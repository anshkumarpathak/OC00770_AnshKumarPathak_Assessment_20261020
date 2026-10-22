"""Business rules and tuning knobs in one place, so they are easy to review."""
import os
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "kb"
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", ROOT / "runtime"))

# Fixed "today" so the demo and the evaluation give the same answers every run.
TODAY = date.fromisoformat(os.environ.get("AGENT_TODAY", "2026-10-20"))

# Access rules (mirror kb/access_policy.md)
STANDARD_ROLES = {"Z_FI_DISPLAY", "Z_FI_AP_CLERK", "Z_FI_CONTROLLER", "Z_MM_DISPLAY", "Z_MM_BUYER",
                  "Z_SD_STORE", "Z_WM_OPERATOR", "Z_REPORTING"}
SENSITIVE_ROLES = {"SAP_ALL", "SAP_NEW", "Z_BASIS_ADMIN", "Z_FI_PAYMENT_RUN", "Z_DEBUG_CHANGE"}

# Procurement rule (mirrors kb/procurement_runbook.md)
PO_APPROVAL_ESCALATE_BUSINESS_DAYS = 3

# Mock API client behaviour
SERVICE_DESK_URL = os.environ.get("SERVICE_DESK_URL", "http://127.0.0.1:8101")
SAP_URL = os.environ.get("SAP_URL", "http://127.0.0.1:8102")
CONTRACTS_URL = os.environ.get("CONTRACTS_URL", "http://127.0.0.1:8103")
CHANGES_URL = os.environ.get("CHANGES_URL", "http://127.0.0.1:8104")
CMDB_URL = os.environ.get("CMDB_URL", "http://127.0.0.1:8105")
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://127.0.0.1:8106")

# SLA agent: fixed demo clock and at-risk thresholds (mirror kb/escalation_procedure.md)
DEMO_NOW = datetime.fromisoformat(os.environ.get("AGENT_NOW", "2026-10-20T10:00:00"))
SLA_AT_RISK_SHARE = 0.25
SLA_AT_RISK_HOURS = 2.0

# Change agent: short-notice rule (mirrors kb/change_assessment_rules.md)
CHANGE_SHORT_NOTICE_BUSINESS_DAYS = 5

# Integration agent (mirrors kb/integration_runbook.md)
REPROCESS_SAFE_ERRORS = {"IDOC51-VENDOR", "TIMEOUT-POS"}
NEVER_REPROCESS_ERRORS = {"MT940-DUP", "PAY-AUTH"}
MAX_REPROCESS_BATCH = 50

API_TIMEOUT_S = 1.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.2

# Budget per request: at most this many tool calls, within this much time.
# A confused planner stops instead of looping; the portal shows both limits.
MAX_TOOL_STEPS = 8
TURN_BUDGET_S = 10.0

# Retrieval: below these we treat the knowledge base as having no answer
# (calibrated on the questions in evals/cases.jsonl)
RETRIEVAL_MIN_SCORE = 2.0
RETRIEVAL_MIN_COVERAGE = 0.5

# LLM mode (only used when an Anthropic API key is available)
LLM_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
LLM_MAX_TURNS = 6


def agent_mode() -> str:
    """'llm' uses Claude to plan tool calls; 'rules' is the deterministic offline planner."""
    mode = os.environ.get("AGENT_MODE", "auto")
    if mode == "auto":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return "llm" if has_key else "rules"
    return mode
