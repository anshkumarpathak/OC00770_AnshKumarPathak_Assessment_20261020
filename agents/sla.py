"""Use case 1: AMS SLA monitoring and escalation agent.

Who uses it: an Orane AMS duty lead or delivery manager.
Two enterprise APIs: the service desk ticket queue (8101) and the client
contracts API with each client's SLA rules (8103). Runbooks: SLA policy and
escalation procedure. It finds breached and at-risk tickets, explains the risk
with citations and drafts escalations, which the delivery manager approves.
It can never close tickets, lower their priority or contact the client.
"""
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents import actions
from agents.base import BaseAgent, BaseExecutor, NoArgs, SearchArgs, cite, dump, src
from support_agent import config
from support_agent.api_client import ApiClient
from support_agent.tools import ToolResult


# ---- data contracts -----------------------------------------------------------------
class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AmsTicket(_Base):
    ticket_id: str = Field(pattern=r"^INC-\d{4}$")
    client_id: str = Field(pattern=r"^CL\d{2}$")
    priority: Literal["P1", "P2", "P3", "P4"]
    opened_at: datetime
    status: Literal["open", "in_progress", "resolved", "closed"]
    assignee: str | None = None
    summary: str


class SlaRule(_Base):
    rule_id: str
    priority: Literal["P1", "P2", "P3", "P4"]
    resolve_hours: float = Field(gt=0)


class Contract(_Base):
    client_id: str = Field(pattern=r"^CL\d{2}$")
    client_name: str
    sla_plan: Literal["standard", "premium"]
    service_manager: str
    sla_rules: list[SlaRule]

    @model_validator(mode="after")
    def _all_priorities(self):
        if sorted(r.priority for r in self.sla_rules) != ["P1", "P2", "P3", "P4"]:
            raise ValueError("contract must define exactly one rule for each of P1-P4")
        return self


class TicketArgs(BaseModel):
    ticket_id: str = Field(pattern=r"^INC-\d{4}$")


class ClientArgs(BaseModel):
    client_id: str = Field(pattern=r"^CL\d{2}$")


class EscalationArgs(BaseModel):
    ticket_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=3, max_length=500)


def assess(t: AmsTicket, c: Contract) -> dict:
    rule = next(r for r in c.sla_rules if r.priority == t.priority)
    deadline = t.opened_at + timedelta(hours=rule.resolve_hours)
    left = round((deadline - config.DEMO_NOW).total_seconds() / 3600, 1)
    share = left / rule.resolve_hours
    state = ("breached" if left < 0 else
             "at risk" if share <= config.SLA_AT_RISK_SHARE or left < config.SLA_AT_RISK_HOURS else "on track")
    return {"ticket_id": t.ticket_id, "client": c.client_name, "client_id": c.client_id, "plan": c.sla_plan,
            "priority": t.priority, "status": t.status, "assignee": t.assignee or "unassigned",
            "summary": t.summary, "rule_id": rule.rule_id, "resolve_hours": rule.resolve_hours,
            "deadline": deadline.strftime("%Y-%m-%d %H:%M"), "hours_left": left,
            "share_left_pct": round(max(share, 0) * 100), "state": state,
            "as_of": config.DEMO_NOW.strftime("%Y-%m-%d %H:%M")}


class SlaExecutor(BaseExecutor):
    KB_DOCS = ("sla_policy", "escalation_procedure")
    DENIED = {
        "close_ticket": "Tickets are never closed to avoid an SLA breach.",
        "change_ticket_priority": "Ticket priority is never lowered to avoid an SLA breach.",
        "notify_client_directly": "Only the delivery manager informs the client about breaches.",
        "delete_ticket": "Tickets are part of the contractual SLA record and are never deleted.",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._contracts: dict[str, Contract] = {}

    @property
    def desk(self) -> ApiClient:
        return self.clients["service_desk"]

    def contract(self, client_id: str) -> Contract:
        if client_id not in self._contracts:
            self._contracts[client_id] = self.clients["contracts"].request("GET", f"/contracts/{client_id}", Contract)
            self.evidence.update(src("contract", client_id, dump(self._contracts[client_id])))
        return self._contracts[client_id]

    def _open(self) -> list[AmsTicket]:
        return [t for t in self.desk.request("GET", "/queue/tickets", AmsTicket, many=True)
                if t.status in ("open", "in_progress")]

    # ---- tools ---------------------------------------------------------------------
    def list_open_tickets(self, _: NoArgs) -> ToolResult:
        tickets = self._open()
        data = [dump(t) for t in tickets]
        return ToolResult("ok", f"{len(data)} open tickets", data,
                          {k: v for t in data for k, v in src("ticket", t["ticket_id"], t).items()})

    def list_contracts(self, _: NoArgs) -> ToolResult:
        cs = self.clients["contracts"].request("GET", "/contracts", Contract, many=True)
        for c in cs:
            self._contracts[c.client_id] = c
        data = [dump(c) for c in cs]
        return ToolResult("ok", f"{len(data)} contracts", data,
                          {k: v for c in data for k, v in src("contract", c["client_id"], c).items()})

    def get_contract(self, args: ClientArgs) -> ToolResult:
        c = self.contract(args.client_id)
        return ToolResult("ok", "contract loaded", dump(c), src("contract", c.client_id, dump(c)))

    def assess_sla(self, args: TicketArgs) -> ToolResult:
        t = self.desk.request("GET", f"/queue/tickets/{args.ticket_id}", AmsTicket)
        a = assess(t, self.contract(t.client_id))
        return ToolResult("ok", a["state"], a, src("ticket", t.ticket_id, a))

    def sla_overview(self, _: NoArgs) -> ToolResult:
        rows = sorted((assess(t, self.contract(t.client_id)) for t in self._open()), key=lambda a: a["hours_left"])
        return ToolResult("ok", f"{len(rows)} open tickets assessed", rows,
                          {k: v for a in rows for k, v in src("ticket", a["ticket_id"], a).items()})

    def draft_escalation(self, args: EscalationArgs) -> ToolResult:
        rows = []
        for tid in dict.fromkeys(args.ticket_ids):
            r = self.assess_sla(TicketArgs(ticket_id=tid))
            self.evidence.update(r.sources)
            rows.append(r.data)
        closed = [a for a in rows if a["status"] not in ("open", "in_progress")]
        fine = [a for a in rows if a["state"] == "on track"]
        if closed:
            return ToolResult("denied", f"{closed[0]['ticket_id']} is {closed[0]['status']}, so there is nothing to "
                              "escalate.", rows)
        if fine:
            kb = self.kb("escalation_procedure#1")
            return ToolResult("denied", f"{fine[0]['ticket_id']} is on track ({fine[0]['hours_left']} h left); only "
                              "breached or at-risk tickets are escalated.", rows,
                              {f"ticket:{fine[0]['ticket_id']}": self.evidence[f"ticket:{fine[0]['ticket_id']}"],
                               **{k: self.evidence[k] for k in kb}})
        dupes = [a["ticket_id"] for a in rows if actions.pending_for("sla", "ticket_ids", a["ticket_id"])]
        if dupes:
            return ToolResult("denied", f"An escalation for {dupes[0]} is already waiting for approval.", rows)
        summary = "; ".join(f"{a['ticket_id']} {a['client']} {a['priority']} {a['state']}" for a in rows)
        item = actions.create("sla", "send_escalation", summary,
                              {"ticket_ids": [a["ticket_id"] for a in rows], "reason": args.reason}, self.actor,
                              self.trace.trace_id)
        self.approval_id = item["action_id"]
        kb = self.kb("escalation_procedure#3")
        return ToolResult("pending_approval", f"Escalation {item['action_id']} waits for the AMS delivery manager.",
                          {"action_id": item["action_id"], "tickets": rows},
                          {**src("approval", item["action_id"], item), **{k: self.evidence[k] for k in kb}})


SlaExecutor.TOOLS = {
    "list_open_tickets": (NoArgs, SlaExecutor.list_open_tickets, "List open AMS tickets from the service desk."),
    "list_contracts": (NoArgs, SlaExecutor.list_contracts, "List every client's SLA plan and resolution times."),
    "get_contract": (ClientArgs, SlaExecutor.get_contract, "Get a client's SLA plan and resolution times."),
    "assess_sla": (TicketArgs, SlaExecutor.assess_sla,
                   "Compute one ticket's SLA deadline, hours left and state (breached, at risk, on track)."),
    "sla_overview": (NoArgs, SlaExecutor.sla_overview, "Assess every open ticket, most urgent first."),
    "search_runbook": (SearchArgs, SlaExecutor.search_runbook, "Search the SLA policy and escalation procedure."),
    "draft_escalation": (EscalationArgs, SlaExecutor.draft_escalation,
                         "Draft an escalation for breached or at-risk tickets. It is only sent after the AMS "
                         "delivery manager approves it."),
}


@actions.handler("send_escalation")
def _send(payload: dict, action_id: str, approver: str) -> dict:
    from support_agent.api_client import service_desk_client
    from support_agent.schemas import _Base as _B

    class Escalation(_B):
        escalation_id: str
        ticket_ids: list[str]

    esc = service_desk_client().request("POST", "/queue/escalations", Escalation, idempotency_key=action_id,
                                        json={**payload, "approved_by": approver})
    return {"escalation_id": esc.escalation_id}


# ---- rule-based planner --------------------------------------------------------------
TICKET_RE = re.compile(r"\bINC-\d{4}\b", re.I)
DENIED_PATTERNS = [
    (r"\b(close|resolve)\b.*\b(ticket|inc-\d{4})", "close_ticket", "close tickets", "escalation_procedure#4"),
    (r"\b(lower|downgrade|reduce|change|set|move)\b.*\b(priority|p[1-4])\b", "change_ticket_priority",
     "change ticket priorities", "escalation_procedure#4"),
    (r"\b(e-?mail|call|message|notify|contact|tell|write to)\b.*\bclient\b", "notify_client_directly",
     "contact the client directly", "escalation_procedure#3"),
    (r"\bdelete\b.*\b(ticket|inc-\d{4})", "delete_ticket", "delete tickets", "escalation_procedure#4"),
]
QUESTION_RE = re.compile(r"(how|what|when|why|which|who|is|are|do|does|can i|should)\b")


def _line(a: dict) -> str:
    when = (f"breached {abs(a['hours_left'])} h ago (deadline {a['deadline']})" if a["state"] == "breached"
            else f"{a['state']}, deadline {a['deadline']}, {a['hours_left']} h left")
    return (f"{a['ticket_id']} ({a['client']}, {a['priority']}, {a['assignee']}): {when} "
            f"{cite('ticket:' + a['ticket_id'], 'contract:' + a['client_id'])}")


def plan(ex: SlaExecutor, message: str) -> tuple[str, str, str]:
    m = message.lower()
    ids = [t.upper() for t in TICKET_RE.findall(message)]
    try:
        for pattern, tool, action, chunk in DENIED_PATTERNS:
            if re.search(pattern, m):
                ex.execute(tool, {"ticket_ids": ids})
                kb = ex.kb(chunk)
                text = ex.evidence[kb[0]].split(": ", 1)[1]
                return f"denied:{tool}", f"I'm not able to {action}. {text} {cite(*kb)}", "blocked"

        if re.search(r"\bescalat|\bdraft\b", m) and not QUESTION_RE.match(m):
            if not ids:
                ov = _ok(ex.execute("sla_overview"))
                want = ("breached",) if "breach" in m and "risk" not in m else ("breached", "at risk")
                ids = [a["ticket_id"] for a in ov.data if a["state"] in want]
                if not ids:
                    return "escalate", "No open tickets are breached or at risk, so there is nothing to escalate.", "answered"
            r = ex.execute("draft_escalation", {"ticket_ids": ids, "reason": message[:400]})
            if r.status == "error":
                raise _Failed(r)
            if r.status == "pending_approval":
                lines = "\n".join(f"- {_line(a)}" for a in r.data["tickets"])
                return "escalate", (f"I've drafted escalation {r.data['action_id']} for the AMS delivery manager:\n"
                                    f"{lines}\nIt waits for their approval before anything is sent. "
                                    f"{cite('approval:' + r.data['action_id'], *[s for s in r.sources if s.startswith('kb:')])}"), \
                    "pending_approval"
            return "escalate", f"I can't draft that escalation. {r.message} {cite(*r.sources)}".strip(), r.status

        if ids:
            r = ex.execute("assess_sla", {"ticket_id": ids[0]})
            if r.status == "error":
                raise _Failed(r)
            if r.status != "ok":
                return "ticket", f"I couldn't find ticket {ids[0]} in the AMS queue.", r.status
            a = r.data
            if a["status"] not in ("open", "in_progress"):
                return "ticket", f"{a['ticket_id']} is {a['status']}, so its SLA no longer applies. {cite('ticket:' + a['ticket_id'])}", "answered"
            kb = ex.kb("escalation_procedure#1")
            advice = ("I can draft an escalation for the delivery manager." if a["state"] != "on track"
                      else "No escalation is needed yet.")
            return "ticket", (f"{a['ticket_id']} ({a['client']}, {a['priority']}, {a['assignee']}) is {a['state']}: the "
                              f"{a['plan']} plan allows {a['resolve_hours']:g} hours to resolve {cite('contract:' + a['client_id'])}, "
                              f"so the deadline is {a['deadline']} "
                              + (f"and it breached {abs(a['hours_left'])} h ago " if a["state"] == "breached" else
                                 f"and {a['hours_left']} h are left ({a['share_left_pct']}% of the window) ")
                              + f"{cite('ticket:' + a['ticket_id'])}. "
                              f"{advice} {cite(*kb)}"), "answered"

        if re.search(r"\bat risk\b|\bbreach|\bsla\b.*\b(status|today|now|queue)|\bqueue\b|\bwhich tickets\b|"
                     r"\boverview\b|\burgent\b|\bdeadlines?\b", m) and not re.search(r"\b(define|definition|policy)\b", m):
            ov = _ok(ex.execute("sla_overview"))
            risky = [a for a in ov.data if a["state"] != "on track"]
            ok = len(ov.data) - len(risky)
            if not risky:
                return "overview", f"All {ok} open tickets are on track as of {config.DEMO_NOW:%Y-%m-%d %H:%M}.", "answered"
            lines = "\n".join(f"- {_line(a)}" for a in risky)
            b = sum(a["state"] == "breached" for a in risky)
            return "overview", (f"As of {ov.data[0]['as_of']}, {b} tickets have breached and {len(risky) - b} are at "
                                f"risk:\n{lines}\nThe other {ok} open tickets are on track. I can draft an "
                                f"escalation for the delivery manager."), "answered"

        prio = re.search(r"\bp([1-4])\b", m)
        if re.search(r"\bresol", m) and prio:
            cs = _ok(ex.execute("list_contracts")).data
            want = [c for c in cs if c["sla_plan"] in m] or cs
            parts = [f"{c['client_name']} ({c['sla_plan']} plan): P{prio.group(1)} must be resolved within "
                     f"{next(r['resolve_hours'] for r in c['sla_rules'] if r['priority'] == 'P' + prio.group(1)):g} hours "
                     f"{cite('contract:' + c['client_id'])}" for c in want]
            return "question", "; ".join(parts) + ".", "answered"

        r = _ok(ex.execute("search_runbook", {"query": message}))
        if not r.data:
            return "question", ("I don't have information about that in the SLA runbooks, so I won't guess."), "abstained"
        top = r.data[0]
        return "question", f"{top['text']} {cite(top['cite_as'])}", "answered"
    except _Failed as f:
        return "error", (f"I can't reach one of the systems I need ({f.result.message.split(':')[0]}), so I can't "
                         "assess the SLA right now and I won't guess. Please try again shortly."), "error"


class _Failed(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def _ok(r: ToolResult) -> ToolResult:
    # Read-only lookups: anything but "ok" (an outage, or a list endpoint answering 404) stops the turn.
    if r.status != "ok":
        raise _Failed(r)
    return r


SYSTEM_PROMPT = """You are the SLA monitoring assistant for an SAP application management (AMS) support desk.
You help {actor}. All data is fictional sample data. Today's date is {today}; use the tools for the current SLA state.
- Use sla_overview or assess_sla for deadlines; never compute them yourself.
- Cite every fact with the cite_as ids from tool results, e.g. [ticket:INC-2002] [contract:CL01] [kb:escalation_procedure#1].
- Only draft escalations for breached or at-risk tickets; say they wait for the AMS delivery manager's approval.
- You cannot close tickets, change priorities, delete tickets or contact clients. Explain why using the runbook.
- If a tool reports an error, say the system is unavailable and don't guess. Keep answers short."""


class SlaAgent(BaseAgent):
    key = "sla"
    executor_cls = SlaExecutor
    rules_planner = staticmethod(plan)
    system_prompt = SYSTEM_PROMPT
    fact_patterns = {"id": re.compile(r"\b(?:INC-\d{4}|ACT-\d{4}|SLA-[A-Z]{3}-P\d)\b")}

    def clients(self):
        return {"service_desk": ApiClient(config.SERVICE_DESK_URL, "service_desk"),
                "contracts": ApiClient(config.CONTRACTS_URL, "contracts")}
