"""The support agent: one call per key user message.

Flow for every message:
  1. planner (rules or Claude) decides which tools to call
  2. every tool call goes through ToolExecutor (allowlist, scoping, limits)
  3. the answer is checked by the grounding guard; ungrounded answers are
     replaced with a safe hand-over message instead of reaching the key user
  4. the whole turn is written to the trace log for the dashboard
"""
import re
from dataclasses import dataclass, field

from support_agent import config, grounding, planner_llm, planner_rules
from support_agent.tools import ToolExecutor
from support_agent.tracing import Trace

SAFE_FALLBACK = ("I'm not confident I can answer that accurately, so I've stopped rather than guess. "
                 "An Orane consultant can help; would you like me to raise a ticket?")
ABSTAIN_RE = re.compile(r"don't have (that |any )?information|not (able|sure)|can't find|no information", re.I)


@dataclass
class AgentResponse:
    answer: str
    status: str
    mode: str
    intent: str | None
    citations: list[str]
    trace_id: str
    latency_ms: float
    tool_calls: list[dict] = field(default_factory=list)
    approval_id: str | None = None
    grounded: bool = True              # final answer (after guard)
    raw_grounded: bool = True          # planner's answer before the guard
    ungrounded_facts: list[str] = field(default_factory=list)


def _status_from_tools(trace: Trace, answer: str, approval_id: str | None) -> str:
    """For the LLM planner the outcome comes from what the tools did, not from the model's wording."""
    statuses = {c.status for c in trace.tool_calls}
    if approval_id:
        return "pending_approval"
    if "error" in statuses:
        return "error"
    if "blocked" in statuses:
        return "blocked"
    if any(c.tool == "create_ticket" and c.status == "ok" for c in trace.tool_calls):
        return "escalated"
    if "denied" in statuses:
        return "denied"
    if not grounding.CITATION_RE.search(answer) and ABSTAIN_RE.search(answer):
        return "abstained"
    return "answered"


class SupportAgent:
    def __init__(self, mode: str | None = None):
        self.mode = mode or config.agent_mode()

    def ask(self, user_id: str, message: str, history: list[dict] | None = None) -> AgentResponse:
        trace = Trace(user_id=user_id, question=message, mode=self.mode)
        ex = ToolExecutor(user_id, trace)

        if self.mode == "llm":
            intent, answer, status = planner_llm.run(ex, message, history)
            status = status or _status_from_tools(trace, answer, ex.approval_id)
        else:
            intent, answer, status = planner_rules.run(ex, message)

        report = grounding.check(answer, ex.evidence, user_text=message)
        raw_grounded = report.grounded
        if not report.grounded:
            trace.guardrail_triggered = True
            answer, status = SAFE_FALLBACK, "guardrail"

        trace.intent, trace.answer, trace.status = intent, answer, status
        trace.citations = report.citations if raw_grounded else []
        trace.grounded, trace.ungrounded_facts = raw_grounded, report.ungrounded_facts + report.unknown_citations
        trace.approval_id = ex.approval_id
        trace.finish()

        return AgentResponse(
            answer=answer, status=status, mode=self.mode, intent=intent, citations=trace.citations,
            trace_id=trace.trace_id, latency_ms=trace.latency_ms,
            tool_calls=[vars(c) for c in trace.tool_calls], approval_id=ex.approval_id,
            grounded=True, raw_grounded=raw_grounded, ungrounded_facts=trace.ungrounded_facts,
        )
