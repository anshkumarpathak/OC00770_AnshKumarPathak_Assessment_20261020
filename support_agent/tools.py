"""The agent's tools and the guard layer every tool call goes through.

Safety lives here, in code, not in the prompt. Whatever the planner asks for
(rule-based or Claude), the ToolExecutor enforces:
  - Allowlist: only tools in TOOLS can run; DENIED_TOOLS are refused outright.
  - Input validation: arguments are checked against a pydantic model.
  - Data scoping: a key user only sees their own company's SAP documents and
    their own tickets, and can only unlock their own SAP user.
  - Access rules: standard roles need the client authorisation owner's
    approval; sensitive roles (SAP_ALL etc.) are never requested.
  - Tracing: every call, its latency and its API retries are recorded.
"""
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from support_agent import approvals, config, retrieval
from support_agent.api_client import ApiClient, ApiError, NotFound, sap_client, service_desk_client
from support_agent.schemas import Invoice, KeyUser, PurchaseOrder, SapUser, Ticket
from support_agent.tracing import ToolCallRecord, Trace

# Actions Orane's support desk never performs, with the right route instead.
DENIED_TOOLS = {
    "delete_sap_user": "SAP users are locked and end-dated through the client HR leaver process, never deleted.",
    "reset_sap_password": "Passwords are reset by the user in the self-service password portal.",
    "release_payment_block": "Payment blocks are released only by the client accounts payable lead.",
    "change_vendor_bank_details": "Vendor bank changes need dual control by the client finance team.",
    "import_transport_to_production": "Production imports happen only after Change Advisory Board approval.",
    "export_payroll_data": "Payroll and personal data requests go to the client HR data owner.",
}


@dataclass
class ToolResult:
    status: str                       # ok | denied | blocked | pending_approval | error
    message: str
    data: Any = None
    sources: dict[str, str] = field(default_factory=dict)   # source_id -> evidence text

    def for_model(self) -> str:
        """What the LLM planner sees as the tool_result content."""
        return json.dumps({"status": self.status, "message": self.message, "data": self.data,
                           "cite_as": list(self.sources)}, default=str)


# ---- tool input models (also used to build the JSON schema shown to Claude) ----

class NoArgs(BaseModel):
    pass


class POArgs(BaseModel):
    po_id: str = Field(pattern=r"^PO-\d{4}$", description="Purchase order number, e.g. PO-4501")


class InvoiceArgs(BaseModel):
    invoice_id: str = Field(pattern=r"^INV-\d{4}$", description="Invoice number, e.g. INV-5102")


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, description="What to look up in the SAP support runbooks and policies")


class TicketArgs(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=2000)
    priority: str = Field(default="P3", pattern="^P[1-4]$", description="P1 production down ... P4 question")


class UnlockArgs(BaseModel):
    sap_user: str | None = Field(default=None, description="Omit to unlock the key user's own SAP user")


class RoleArgs(BaseModel):
    role: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{2,30}$", description="SAP role name, e.g. Z_MM_DISPLAY")
    reason: str = Field(min_length=3, max_length=300)


def _dump(model) -> dict:
    return model.model_dump(mode="json")


def business_days_between(start, end) -> int:
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


class ToolExecutor:
    def __init__(self, user_id: str, trace: Trace,
                 desk: ApiClient | None = None, sap: ApiClient | None = None):
        self.user_id = user_id
        self.trace = trace
        self.desk = desk or service_desk_client()
        self.sap = sap or sap_client()
        self.evidence: dict[str, str] = {}
        self.approval_id: str | None = None
        self._profile: KeyUser | None = None

    # ---- the single entry point -------------------------------------------------
    def execute(self, name: str, args: dict | None = None) -> ToolResult:
        args = args or {}
        t0 = time.perf_counter()
        n_calls_before = len(self.desk.calls) + len(self.sap.calls)

        if why := self.trace.over_budget():
            result = ToolResult("error", f"budget_exceeded: {why}")
        elif name in DENIED_TOOLS:
            result = ToolResult("blocked", f"Action '{name}' is not permitted. {DENIED_TOOLS[name]}")
        elif name not in TOOLS:
            result = ToolResult("blocked", f"Unknown tool '{name}'.")
        else:
            arg_model, func, _ = TOOLS[name]
            try:
                parsed = arg_model.model_validate(args)
                result = func(self, parsed)
            except ValidationError as exc:
                result = ToolResult("error", f"Invalid arguments for {name}: {exc.errors()[0]['msg']}")
            except NotFound as exc:
                result = ToolResult("denied", f"Not found: {exc}")
            except ApiError as exc:
                result = ToolResult("error", f"{exc.kind}: {exc}")

        self.evidence.update(result.sources)
        api_calls = (self.desk.calls + self.sap.calls)[n_calls_before:]
        self.trace.tool_calls.append(ToolCallRecord(
            tool=name, args=args, status=result.status, message=result.message,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            api_attempts=sum(c.attempts for c in api_calls),
            api_retries=sum(c.retries for c in api_calls),
            api_errors=[e for c in api_calls for e in c.errors],
        ))
        if result.status == "blocked":
            approvals.audit("tool_blocked", tool=name, args=args, user_id=self.user_id,
                            trace_id=self.trace.trace_id, reason=result.message)
        return result

    # ---- helpers -----------------------------------------------------------------
    def profile(self) -> KeyUser:
        """Who is asking, from the service desk. Cached for the turn."""
        if self._profile is None:
            self._profile = self.desk.request("GET", f"/users/{self.user_id}", KeyUser)
        return self._profile

    def _own(self, doc, label: str) -> ToolResult | None:
        if doc.client_id != self.profile().client_id:
            # Logged as blocked, but worded like "not found" so we don't confirm it exists.
            return ToolResult("blocked", f"{label} was not found for your company.")
        return None

    @staticmethod
    def _kb(*chunk_ids: str) -> dict[str, str]:
        out = {}
        for cid in chunk_ids:
            chunk = retrieval.get_chunk(cid)
            if chunk:
                out[chunk.source_id] = f"{chunk.doc_title} - {chunk.heading}: {chunk.text}"
        return out

    # ---- read tools --------------------------------------------------------------
    def get_my_profile(self, _: NoArgs) -> ToolResult:
        p = self.profile()
        data = {"user_id": p.user_id, "name": p.name, "client_name": p.client_name, "sla_plan": p.sla_plan,
                "job_role": p.job_role, "sap_user": p.sap_user}              # email not exposed
        return ToolResult("ok", "profile loaded", data, {f"user:{p.user_id}": json.dumps(data)})

    def list_my_tickets(self, _: NoArgs) -> ToolResult:
        tickets = self.desk.request("GET", f"/users/{self.user_id}/tickets", Ticket, many=True)
        data = [_dump(t) for t in tickets]
        return ToolResult("ok", f"{len(data)} tickets", data,
                          {f"ticket:{t['ticket_id']}": json.dumps(t) for t in data})

    def get_purchase_order(self, args: POArgs) -> ToolResult:
        po = self.sap.request("GET", f"/purchase-orders/{args.po_id}", PurchaseOrder)
        if (blocked := self._own(po, f"Purchase order {args.po_id}")):
            return blocked
        data = _dump(po)
        if po.status == "awaiting_approval":
            waiting = business_days_between(po.created_at, config.TODAY)
            data["business_days_awaiting_approval"] = waiting
            data["approval_overdue"] = waiting > config.PO_APPROVAL_ESCALATE_BUSINESS_DAYS
        return ToolResult("ok", "purchase order loaded", data, {f"po:{po.po_id}": json.dumps(data)})

    def get_invoice(self, args: InvoiceArgs) -> ToolResult:
        inv = self.sap.request("GET", f"/invoices/{args.invoice_id}", Invoice)
        if (blocked := self._own(inv, f"Invoice {args.invoice_id}")):
            return blocked
        data = _dump(inv)
        return ToolResult("ok", "invoice loaded", data, {f"invoice:{inv.invoice_id}": json.dumps(data)})

    def get_my_sap_user(self, _: NoArgs) -> ToolResult:
        u = self.sap.request("GET", f"/sap-users/{self.profile().sap_user}", SapUser)
        data = _dump(u)
        return ToolResult("ok", "SAP user loaded", data, {f"sap_user:{u.sap_user}": json.dumps(data)})

    def search_knowledge(self, args: SearchArgs) -> ToolResult:
        hits = retrieval.index().search(args.query)
        if not hits:
            return ToolResult("ok", "no matching runbook or policy found", [])
        data = [{"cite_as": c.source_id, "document": c.doc_title, "section": c.heading,
                 "text": c.text, "score": s} for s, c in hits]
        return ToolResult("ok", f"{len(hits)} sections", data,
                          {c.source_id: f"{c.doc_title} - {c.heading}: {c.text}" for _, c in hits})

    # ---- action tools ------------------------------------------------------------
    def create_ticket(self, args: TicketArgs) -> ToolResult:
        t = self.desk.request("POST", "/tickets", Ticket, json={"user_id": self.user_id, **args.model_dump()})
        data = _dump(t)
        return ToolResult("ok", f"ticket {t.ticket_id} created", data, {f"ticket:{t.ticket_id}": json.dumps(data)})

    def unlock_sap_user(self, args: UnlockArgs) -> ToolResult:
        own = self.profile().sap_user
        if args.sap_user and args.sap_user.upper() != own:
            return ToolResult("blocked", "You can only unlock your own SAP user.", None, self._kb("access_policy#2"))
        u = self.sap.request("GET", f"/sap-users/{own}", SapUser)
        src = {f"sap_user:{own}": json.dumps(_dump(u))}
        if u.lock_status == "unlocked":
            return ToolResult("denied", f"SAP user {own} is not locked.", _dump(u), src)
        if u.lock_status == "locked_by_admin":
            return ToolResult("denied", f"SAP user {own} was locked by an administrator, so only the client "
                              "security administrator can unlock it.", _dump(u), {**src, **self._kb("access_policy#2")})
        done = self.sap.request("POST", f"/sap-users/{own}/unlock", SapUser,
                                idempotency_key=f"{self.trace.trace_id}-unlock")
        approvals.audit("sap_user_unlocked", sap_user=own, user_id=self.user_id, trace_id=self.trace.trace_id)
        return ToolResult("ok", f"SAP user {own} unlocked.", _dump(done),
                          {f"sap_user:{own}": json.dumps(_dump(done)), **self._kb("access_policy#2")})

    def request_role(self, args: RoleArgs) -> ToolResult:
        role = args.role.upper()
        if role in config.SENSITIVE_ROLES:
            return ToolResult("blocked", f"{role} is a sensitive role and is never assigned through the support "
                              "desk. It needs a change request with a segregation of duties review.",
                              {"role": role}, self._kb("access_policy#4"))
        if role not in config.STANDARD_ROLES:
            return ToolResult("denied", f"{role} is not in the standard role catalogue.", {"role": role},
                              self._kb("access_policy#5"))
        p = self.profile()
        u = self.sap.request("GET", f"/sap-users/{p.sap_user}", SapUser)
        if role in u.roles:
            return ToolResult("denied", f"SAP user {p.sap_user} already has {role}.", _dump(u),
                              {f"sap_user:{p.sap_user}": json.dumps(_dump(u))})
        item = approvals.create(p.user_id, p.client_id, p.sap_user, role, args.reason, self.trace.trace_id)
        self.approval_id = item["approval_id"]
        return ToolResult("pending_approval", f"Request {item['approval_id']} for {role} is waiting for the client "
                          "authorisation owner.", {"approval_id": item["approval_id"], "role": role},
                          {f"approval:{item['approval_id']}": json.dumps(item), **self._kb("access_policy#3")})


# name -> (argument model, function, description shown to the planner)
TOOLS: dict[str, tuple[type[BaseModel], Callable, str]] = {
    "get_my_profile": (NoArgs, ToolExecutor.get_my_profile,
                       "Get the key user's name, company, SLA plan and SAP user ID."),
    "list_my_tickets": (NoArgs, ToolExecutor.list_my_tickets, "List the key user's support tickets."),
    "get_purchase_order": (POArgs, ToolExecutor.get_purchase_order,
                           "Get a purchase order's status, approver, vendor, items and total from SAP."),
    "get_invoice": (InvoiceArgs, ToolExecutor.get_invoice,
                    "Get a vendor invoice's status, amount, due date and payment block reason from SAP."),
    "get_my_sap_user": (NoArgs, ToolExecutor.get_my_sap_user,
                        "Get the key user's SAP user lock status and assigned roles."),
    "search_knowledge": (SearchArgs, ToolExecutor.search_knowledge,
                         "Search the SAP support runbooks: SLA, access, finance, procurement, change, data security."),
    "create_ticket": (TicketArgs, ToolExecutor.create_ticket,
                      "Raise a support ticket so an Orane consultant picks the case up."),
    "unlock_sap_user": (UnlockArgs, ToolExecutor.unlock_sap_user,
                        "Unlock the key user's own SAP user if it was locked by failed logins."),
    "request_role": (RoleArgs, ToolExecutor.request_role,
                     "Request a standard SAP role. It is applied only after the client authorisation owner "
                     "approves; sensitive roles are refused."),
}


def tool_specs() -> list[dict]:
    """Tool definitions for the Claude API. Denied tools are never offered."""
    specs = []
    for name, (model, _, description) in TOOLS.items():
        schema = model.model_json_schema()
        specs.append({"name": name, "description": description, "input_schema": {
            "type": "object", "properties": schema.get("properties", {}), "required": schema.get("required", [])}})
    return specs
