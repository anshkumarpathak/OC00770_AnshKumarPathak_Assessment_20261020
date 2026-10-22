"""Shared building blocks for the SLA, change-impact and integration agents.

They follow the same design as the support desk assistant (support_agent/):
  - BaseExecutor: every tool call goes through one guard layer (allowlist,
    denied tools, argument validation, API error mapping, tracing).
  - BaseAgent: planner (rules or Claude) -> tools -> grounding check -> trace.
Each agent only declares its tools, denied actions, planner and fact patterns.
"""
import json
import re
import time
from typing import Callable

from pydantic import BaseModel, ValidationError

from support_agent import approvals, config, grounding, planner_llm, retrieval
from support_agent.agent import SAFE_FALLBACK, AgentResponse, _status_from_tools, cited_evidence
from support_agent.api_client import ApiClient, ApiError, NotFound
from support_agent.tools import ToolResult
from support_agent.tracing import ToolCallRecord, Trace

DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?\b")
DURATION_RE = re.compile(r"\b(\d+(?:\.\d+)?)(?:-\d+)?\s+(?:business\s+)?(?:days?|hours?|h|minutes?)\b")


class NoArgs(BaseModel):
    pass


class SearchArgs(BaseModel):
    query: str


class BaseExecutor:
    TOOLS: dict[str, tuple[type[BaseModel], Callable, str]] = {}
    DENIED: dict[str, str] = {}
    KB_DOCS: tuple[str, ...] = ()

    def __init__(self, actor: str, trace: Trace, clients: dict[str, ApiClient]):
        self.actor = actor
        self.user_id = actor
        self.trace = trace
        self.clients = clients
        self.evidence: dict[str, str] = {}
        self.approval_id: str | None = None

    def _calls(self):
        return [c for cl in self.clients.values() for c in cl.calls]

    def execute(self, name: str, args: dict | None = None) -> ToolResult:
        args = args or {}
        t0, before = time.perf_counter(), {k: len(c.calls) for k, c in self.clients.items()}
        if why := self.trace.over_budget():
            result = ToolResult("error", f"budget_exceeded: {why}")
        elif name in self.DENIED:
            result = ToolResult("blocked", f"Action '{name}' is not permitted. {self.DENIED[name]}")
        elif name not in self.TOOLS:
            result = ToolResult("blocked", f"Unknown tool '{name}'.")
        else:
            model, func, _ = self.TOOLS[name]
            try:
                result = func(self, model.model_validate(args))
            except ValidationError as exc:
                result = ToolResult("error", f"Invalid arguments for {name}: {exc.errors()[0]['msg']}")
            except NotFound as exc:
                result = ToolResult("denied", f"Not found: {exc}")
            except ApiError as exc:
                result = ToolResult("error", f"{exc.kind}: {exc}")
        self.evidence.update(result.sources)
        calls = [c for k, cl in self.clients.items() for c in cl.calls[before[k]:]]
        self.trace.tool_calls.append(ToolCallRecord(
            tool=name, args=args, status=result.status, message=result.message,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            api_attempts=sum(c.attempts for c in calls), api_retries=sum(c.retries for c in calls),
            api_errors=[e for c in calls for e in c.errors]))
        if result.status == "blocked":
            approvals.audit("tool_blocked", agent=self.trace.agent, tool=name, args=args, user_id=self.actor,
                            trace_id=self.trace.trace_id, reason=result.message)
        return result

    # ---- shared helpers ------------------------------------------------------------
    def kb(self, *chunk_ids: str) -> list[str]:
        """Add fixed runbook sections to the evidence and return their citation ids."""
        out = []
        for cid in chunk_ids:
            chunk = retrieval.get_chunk(cid)
            if chunk:
                self.evidence[chunk.source_id] = f"{chunk.doc_title} - {chunk.heading}: {chunk.text}"
                out.append(chunk.source_id)
        return out

    def search_runbook(self, args: SearchArgs) -> ToolResult:
        hits = retrieval.index_for(self.KB_DOCS).search(args.query)
        if not hits:
            return ToolResult("ok", "no matching runbook section", [])
        data = [{"cite_as": c.source_id, "document": c.doc_title, "section": c.heading, "text": c.text}
                for _, c in hits]
        return ToolResult("ok", f"{len(hits)} sections", data,
                          {c.source_id: f"{c.doc_title} - {c.heading}: {c.text}" for _, c in hits})

    @classmethod
    def tool_specs(cls) -> list[dict]:
        specs = []
        for name, (model, _, desc) in cls.TOOLS.items():
            schema = model.model_json_schema()
            specs.append({"name": name, "description": desc, "input_schema": {
                "type": "object", "properties": schema.get("properties", {}), "required": schema.get("required", [])}})
        return specs


def dump(model) -> dict:
    return model.model_dump(mode="json")


def src(prefix: str, key: str, data) -> dict[str, str]:
    return {f"{prefix}:{key}": json.dumps(data, default=str)}


def cite(*ids: str) -> str:
    return " ".join(f"[{i}]" for i in dict.fromkeys(ids))


class BaseAgent:
    key = ""
    executor_cls: type[BaseExecutor] = BaseExecutor
    rules_planner: Callable = None
    system_prompt = ""
    fact_patterns: dict[str, re.Pattern] = {}

    def __init__(self, mode: str | None = None):
        self.mode = mode or config.agent_mode()

    def clients(self) -> dict[str, ApiClient]:
        raise NotImplementedError

    def ask(self, actor: str, message: str, history: list[dict] | None = None) -> AgentResponse:
        trace = Trace(user_id=actor, question=message, mode=self.mode, agent=self.key)
        ex = self.executor_cls(actor, trace, self.clients())
        if self.mode == "llm":
            system = self.system_prompt.format(actor=actor, today=config.TODAY.isoformat())
            intent, answer, status = planner_llm.run(ex, message, history, system, self.executor_cls.tool_specs())
            status = status or _status_from_tools(trace, answer, ex.approval_id)
        else:
            intent, answer, status = type(self).rules_planner(ex, message)

        patterns = {**self.fact_patterns, "date": DATE_RE, "duration": DURATION_RE}
        report = grounding.check(answer, ex.evidence, user_text=message, patterns=patterns,
                                 citation_re=grounding.ANY_CITATION_RE)
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
            trace_id=trace.trace_id, latency_ms=trace.latency_ms, tool_calls=[vars(c) for c in trace.tool_calls],
            approval_id=ex.approval_id, grounded=True, raw_grounded=raw_grounded,
            ungrounded_facts=trace.ungrounded_facts, evidence=cited_evidence(trace.citations, ex.evidence))
