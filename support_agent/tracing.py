"""One JSON line per agent turn in runtime/traces.jsonl, read by the dashboard.

Personal data is masked before anything is written: emails, phone numbers and
anything that looks like a card number.
"""
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from support_agent import config

_MASKS = [
    (re.compile(r"[^@\s]+@[^@\s]+\.[a-z]+"), "<email>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<card>"),
    (re.compile(r"\+?\d[\d -]{8,}\d"), "<phone>"),
]


def redact(text: str) -> str:
    for pattern, mask in _MASKS:
        text = pattern.sub(mask, text)
    return text


@dataclass
class ToolCallRecord:
    tool: str
    args: dict
    status: str          # ok | denied | blocked | pending_approval | error
    message: str
    latency_ms: float
    api_attempts: int = 0
    api_retries: int = 0
    api_errors: list[str] = field(default_factory=list)


@dataclass
class Trace:
    user_id: str
    question: str
    mode: str
    agent: str = "support_desk"
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    intent: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    answer: str = ""
    status: str = "answered"
    citations: list[str] = field(default_factory=list)
    grounded: bool = True
    ungrounded_facts: list[str] = field(default_factory=list)
    guardrail_triggered: bool = False
    approval_id: str | None = None
    latency_ms: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    _t0: float = field(default_factory=time.perf_counter, repr=False)

    def over_budget(self) -> str | None:
        """Why the next tool call must not run, if this turn has used its step or time budget."""
        if len(self.tool_calls) >= config.MAX_TOOL_STEPS:
            return f"step budget used ({config.MAX_TOOL_STEPS} tool calls per request)"
        if time.perf_counter() - self._t0 > config.TURN_BUDGET_S:
            return f"time budget used ({config.TURN_BUDGET_S:.0f} s per request)"
        return None

    def finish(self) -> None:
        self.latency_ms = round((time.perf_counter() - self._t0) * 1000, 1)
        self.question = redact(self.question)
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        record = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        with open(config.RUNTIME_DIR / "traces.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def load_traces() -> list[dict]:
    path = config.RUNTIME_DIR / "traces.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
