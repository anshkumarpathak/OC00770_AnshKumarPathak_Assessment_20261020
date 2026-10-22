"""Use case 5: SAP integration failure investigation agent.

Who uses it: an Orane integration support consultant.
Two enterprise APIs: the integration message monitor (8106) and the client's
SAP system (8102: vendor master and system health). Runbook: integration
recovery runbook. It groups failed messages by error, explains the known cause
with citations, checks preconditions in SAP (does the vendor exist? is the
system healthy?) and proposes reprocessing, which the integration lead
approves. Payment and bank-statement files are never resent.
"""
import re
from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agents import actions
from agents.base import BaseAgent, BaseExecutor, NoArgs, SearchArgs, cite, dump, src
from support_agent import config
from support_agent.api_client import ApiClient, NotFound
from support_agent.tools import ToolResult


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Message(_Base):
    msg_id: str = Field(pattern=r"^MSG-\d{4}$")
    interface: str
    timestamp: datetime
    status: Literal["failed", "processed", "reprocessed"]
    error_code: str | None = None
    partner_ref: str
    detail: str


class Vendor(_Base):
    vendor_no: str = Field(pattern=r"^\d{6}$")
    name: str
    client_id: str


class Health(_Base):
    status: Literal["green", "amber", "red"]
    dialog_wp_free: int = Field(ge=0)
    background_wp_free: int = Field(ge=0)
    checked_at: datetime


class MsgArgs(BaseModel):
    msg_id: str = Field(pattern=r"^MSG-\d{4}$")


class VendorArgs(BaseModel):
    vendor_no: str = Field(pattern=r"^\d{6}$")


class ErrorArgs(BaseModel):
    error_code: str = Field(pattern=r"^[A-Z0-9-]{3,30}$")


class ReprocessArgs(BaseModel):
    msg_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)


RUNBOOK = {"IDOC51-VENDOR": "integration_runbook#1", "TIMEOUT-POS": "integration_runbook#2",
           "MT940-DUP": "integration_runbook#3", "PAY-AUTH": "integration_runbook#4"}
APPROVAL_RULE = "integration_runbook#5"


class IntegrationExecutor(BaseExecutor):
    KB_DOCS = ("integration_runbook",)
    DENIED = {
        "resend_payment_file": "Payment files are never resent by the support team; treasury handles them.",
        "import_bank_statement": "Bank statements are never re-imported; treasury confirms duplicates.",
        "delete_message": "Failed messages are evidence for the audit trail and are never deleted.",
        "change_partner_profile": "Partner profiles change only through a change request.",
    }

    @property
    def mon(self) -> ApiClient:
        return self.clients["integration"]

    def _failed(self) -> list[Message]:
        return self.mon.request("GET", "/messages", Message, many=True, params={"status": "failed"})

    def list_failed_messages(self, _: NoArgs) -> ToolResult:
        data = [dump(m) for m in self._failed()]
        return ToolResult("ok", f"{len(data)} failed messages", data,
                          {k: v for m in data for k, v in src("msg", m["msg_id"], m).items()})

    def get_message(self, args: MsgArgs) -> ToolResult:
        m = self.mon.request("GET", f"/messages/{args.msg_id}", Message)
        return ToolResult("ok", "message loaded", dump(m), src("msg", m.msg_id, dump(m)))

    def check_vendor(self, args: VendorArgs) -> ToolResult:
        try:
            v = self.clients["sap"].request("GET", f"/vendors/{args.vendor_no}", Vendor)
            data = {**dump(v), "exists": True}
        except NotFound:
            data = {"vendor_no": args.vendor_no, "exists": False}
        return ToolResult("ok", "vendor exists" if data["exists"] else "vendor missing", data,
                          src("vendor", args.vendor_no, data))

    def check_system_health(self, _: NoArgs) -> ToolResult:
        h = self.clients["sap"].request("GET", "/system/health", Health)
        return ToolResult("ok", f"system {h.status}", dump(h), src("sap_health", "latest", dump(h)))

    def _preconditions(self, code: str, msgs: list[dict]) -> tuple[list[str], list[str]]:
        """Returns (ready msg ids, blocker sentences) after checking SAP."""
        if code == "IDOC51-VENDOR":
            ready, blockers = [], []
            for m in msgs:
                v = self.check_vendor(VendorArgs(vendor_no=m["partner_ref"]))
                self.evidence.update(v.sources)
                if v.data["exists"]:
                    ready.append(m["msg_id"])
                else:
                    blockers.append(f"vendor {m['partner_ref']} is not in SAP yet {cite('vendor:' + m['partner_ref'])}")
            return ready, list(dict.fromkeys(blockers))
        if code == "TIMEOUT-POS":
            h = self.check_system_health(NoArgs())
            self.evidence.update(h.sources)
            if h.data["status"] == "green":
                return [m["msg_id"] for m in msgs], []
            return [], [f"SAP system health is {h.data['status']} {cite('sap_health:latest')}"]
        return [], []

    def investigate(self, args: ErrorArgs) -> ToolResult:
        msgs = [dump(m) for m in self._failed() if m.error_code == args.error_code]
        if not msgs:
            return ToolResult("denied", f"No failed messages with error {args.error_code}.", [])
        for m in msgs:
            self.evidence.update(src("msg", m["msg_id"], m))
        kb = self.kb(RUNBOOK[args.error_code]) if args.error_code in RUNBOOK else []
        safe = args.error_code in config.REPROCESS_SAFE_ERRORS
        ready, blockers = self._preconditions(args.error_code, msgs) if safe else ([], [])
        data = {"error_code": args.error_code, "interface": msgs[0]["interface"], "msg_ids": [m["msg_id"] for m in msgs],
                "known_cause": self.evidence[kb[0]].split(": ", 1)[1] if kb else "unknown error: needs analysis",
                "reprocess_safe": safe, "ready_msg_ids": ready, "blockers": blockers}
        sources = {**{f"msg:{i}": self.evidence[f"msg:{i}"] for i in data["msg_ids"]}, **{k: self.evidence[k] for k in kb},
                   **{k: v for k, v in self.evidence.items() if k.startswith(("vendor:", "sap_health:"))}}
        return ToolResult("ok", f"{len(msgs)} messages", data, sources)

    def request_reprocess(self, args: ReprocessArgs) -> ToolResult:
        ids = list(dict.fromkeys(args.msg_ids))
        if len(ids) > config.MAX_REPROCESS_BATCH:
            return ToolResult("denied", f"At most {config.MAX_REPROCESS_BATCH} messages per request.", None,
                              {k: self.evidence[k] for k in self.kb(APPROVAL_RULE)})
        msgs = []
        for i in ids:
            r = self.get_message(MsgArgs(msg_id=i))
            self.evidence.update(r.sources)
            msgs.append(r.data)
        not_failed = [m["msg_id"] for m in msgs if m["status"] != "failed"]
        if not_failed:
            return ToolResult("denied", f"{not_failed[0]} is not in a failed state.", msgs)
        codes = {m["error_code"] for m in msgs}
        if codes & config.NEVER_REPROCESS_ERRORS:
            code = sorted(codes & config.NEVER_REPROCESS_ERRORS)[0]
            kb = self.kb(RUNBOOK[code])
            return ToolResult("blocked", f"Messages with {code} are never reprocessed by support.", msgs,
                              {f"msg:{m['msg_id']}": self.evidence[f"msg:{m['msg_id']}"] for m in msgs} |
                              {k: self.evidence[k] for k in kb})
        if len(codes) > 1:
            return ToolResult("denied", "One request can only cover one error type.", msgs,
                              {k: self.evidence[k] for k in self.kb(APPROVAL_RULE)})
        code = codes.pop()
        ready, blockers = self._preconditions(code, msgs)
        if blockers:
            kb = self.kb(RUNBOOK[code])
            return ToolResult("denied", "Preconditions are not met: " + "; ".join(blockers) + ".", msgs,
                              {k: v for k, v in self.evidence.items() if k.startswith(("vendor:", "sap_health:", "msg:"))}
                              | {k: self.evidence[k] for k in kb})
        dupes = [i for i in ids if actions.pending_for("integration", "msg_ids", i)]
        if dupes:
            return ToolResult("denied", f"Reprocessing for {dupes[0]} is already waiting for approval.", msgs)
        item = actions.create("integration", "reprocess_messages",
                              f"Reprocess {len(ids)} x {code} on {msgs[0]['interface']}: {', '.join(ids)}",
                              {"msg_ids": ids, "error_code": code, "reason": args.reason}, self.actor,
                              self.trace.trace_id)
        self.approval_id = item["action_id"]
        kb = self.kb(APPROVAL_RULE)
        return ToolResult("pending_approval", f"Reprocessing {item['action_id']} waits for the integration lead.",
                          {"action_id": item["action_id"], "msg_ids": ids, "error_code": code},
                          {**src("approval", item["action_id"], item), **{k: self.evidence[k] for k in kb},
                           **{f"msg:{i}": self.evidence[f"msg:{i}"] for i in ids}})


IntegrationExecutor.TOOLS = {
    "list_failed_messages": (NoArgs, IntegrationExecutor.list_failed_messages, "List failed interface messages."),
    "get_message": (MsgArgs, IntegrationExecutor.get_message, "Get one interface message."),
    "check_vendor": (VendorArgs, IntegrationExecutor.check_vendor, "Check whether a vendor exists in SAP."),
    "check_system_health": (NoArgs, IntegrationExecutor.check_system_health, "Get SAP system health."),
    "investigate": (ErrorArgs, IntegrationExecutor.investigate,
                    "Investigate all failed messages with one error code: known cause, preconditions, readiness."),
    "search_runbook": (SearchArgs, IntegrationExecutor.search_runbook, "Search the integration recovery runbook."),
    "request_reprocess": (ReprocessArgs, IntegrationExecutor.request_reprocess,
                          "Request reprocessing of failed messages of one error type. Runs only after the "
                          "integration lead approves; payment and bank files are refused."),
}


@actions.handler("reprocess_messages")
def _reprocess(payload: dict, action_id: str, approver: str) -> dict:
    class Run(_Base):
        run_id: str

    r = ApiClient(config.INTEGRATION_URL, "integration").request(
        "POST", "/messages/reprocess", Run, idempotency_key=action_id,
        json={"msg_ids": payload["msg_ids"], "approved_by": approver, "note": payload.get("note", "")})
    return {"run_id": r.run_id}


# ---- rule-based planner --------------------------------------------------------------
MSG_RE = re.compile(r"\bMSG-\d{4}\b", re.I)
GROUPS = [(r"\bpos\b|sales upload|store", "TIMEOUT-POS"), (r"invoice|edi|idoc|vendor", "IDOC51-VENDOR"),
          (r"payment|payext", "PAY-AUTH"), (r"bank statement|mt940|statement", "MT940-DUP")]
DENIED_PATTERNS = [
    (r"\b(resend|re-send|retransmit|send again)\b.*\bpayment", "resend_payment_file", "resend payment files",
     "integration_runbook#4"),
    (r"\b(re-?import|import|load|post)\b.*\b(statement|mt940)", "import_bank_statement",
     "re-import bank statements", "integration_runbook#3"),
    (r"\bdelete\b.*\b(message|idoc|msg-\d{4})", "delete_message", "delete messages", None),
    (r"\b(change|edit|update)\b.*\bpartner profile", "change_partner_profile", "change partner profiles", None),
]
QUESTION_RE = re.compile(r"(how|what|when|why|which|who|is|are|do|does|can i|should)\b")


class _Failed(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def _ok(r: ToolResult) -> ToolResult:
    # Read-only lookups: anything but "ok" (an outage, or a list endpoint answering 404) stops the turn.
    if r.status != "ok":
        raise _Failed(r)
    return r


def _group_text(d: dict) -> str:
    msgs = cite(*[f"msg:{i}" for i in d["msg_ids"]])
    kb = cite("kb:" + RUNBOOK[d["error_code"]]) if d["error_code"] in RUNBOOK else ""
    text = f"{d['interface']}: {len(d['msg_ids'])} x {d['error_code']} {msgs}. Known cause: {d['known_cause']} {kb}"
    if not d["reprocess_safe"]:
        return text + " These are never reprocessed by support."
    if d["blockers"]:
        text += " Blocked until: " + "; ".join(d["blockers"]) + "."
    if d["ready_msg_ids"]:
        text += f" Ready to reprocess after approval: {', '.join(d['ready_msg_ids'])}"
        if "sap_health:latest" in " ".join(d["blockers"]) or d["error_code"] == "TIMEOUT-POS":
            text += f" (system health green {cite('sap_health:latest')})"
        text += "."
    return text


def _code_for(m: str) -> str | None:
    return next((code for pattern, code in GROUPS if re.search(pattern, m)), None)


def plan(ex: IntegrationExecutor, message: str) -> tuple[str, str, str]:
    m = message.lower()
    ids = [i.upper() for i in MSG_RE.findall(message)]
    try:
        for pattern, tool, action, chunk in DENIED_PATTERNS:
            if re.search(pattern, m) and not QUESTION_RE.match(m):
                blocked = ex.execute(tool, {"msg_ids": ids})
                kb = ex.kb(chunk) if chunk else []
                text = ex.evidence[kb[0]].split(": ", 1)[1] if kb else blocked.message.split(". ", 1)[-1]
                return f"denied:{tool}", f"I'm not able to {action}. {text} {cite(*kb)}".strip(), "blocked"

        if re.search(r"\b(reprocess|retry|restart|re-?run|resend|push again)\b", m) and not QUESTION_RE.match(m):
            if not ids:
                code = _code_for(m)
                if not code:
                    return "reprocess", ("Which interface should I reprocess: the POS sales upload, the vendor invoice "
                                         "IDocs, the bank statement or the payment file?"), "needs_info"
                inv = ex.execute("investigate", {"error_code": code})
                if inv.status == "error":
                    raise _Failed(inv)
                if inv.status != "ok":
                    return "reprocess", f"{inv.message}", inv.status
                ids = inv.data["ready_msg_ids"] or inv.data["msg_ids"]
                prefix = (_group_text(inv.data) + " ") if inv.data["blockers"] and inv.data["ready_msg_ids"] else ""
            else:
                prefix = ""
            r = ex.execute("request_reprocess", {"msg_ids": ids, "reason": message[:400]})
            if r.status == "error":
                raise _Failed(r)
            if r.status == "pending_approval":
                d = r.data
                return "reprocess", (f"{prefix}I've requested reprocessing {d['action_id']} for {', '.join(d['msg_ids'])} "
                                     f"({d['error_code']}). The integration lead approves it before anything is "
                                     f"resent. {cite('approval:' + d['action_id'], 'kb:' + APPROVAL_RULE)}"), "pending_approval"
            return "reprocess", f"I can't request that. {r.message} {cite(*r.sources)}", r.status

        if ids:
            r = ex.execute("get_message", {"msg_id": ids[0]})
            if r.status == "error":
                raise _Failed(r)
            if r.status != "ok":
                return "message", f"I couldn't find message {ids[0]}.", r.status
            msg = r.data
            if msg["status"] != "failed":
                return "message", f"{msg['msg_id']} on {msg['interface']} is {msg['status']}. {cite('msg:' + msg['msg_id'])}", "answered"
            inv = _ok(ex.execute("investigate", {"error_code": msg["error_code"]}))
            return "message", (f"{msg['msg_id']} failed with {msg['error_code']}: {msg['detail']} "
                               f"{cite('msg:' + msg['msg_id'])}. {_group_text(inv.data)}"), "answered"

        code = _code_for(m)
        if code and re.search(r"\b(why|investigate|what happened|fail|failing|failed|error|wrong|look into)", m):
            inv = ex.execute("investigate", {"error_code": code})
            if inv.status == "error":
                raise _Failed(inv)
            if inv.status != "ok":
                return "investigate", f"{inv.message}", "answered"
            return "investigate", _group_text(inv.data), "answered"

        if re.search(r"\b(what failed|failures|failed|overnight|last night|summary|status|broken|errors)\b", m) \
                and not re.search(r"\bhow\b", m):
            lst = _ok(ex.execute("list_failed_messages"))
            codes = list(dict.fromkeys(x["error_code"] for x in lst.data))
            parts = [_group_text(_ok(ex.execute("investigate", {"error_code": c})).data) for c in codes]
            return "overview", (f"{len(lst.data)} messages failed, from {len(codes)} causes:\n"
                                + "\n".join(f"- {p}" for p in parts)), "answered"

        r = _ok(ex.execute("search_runbook", {"query": message}))
        if not r.data:
            return "question", "I don't have information about that in the integration runbook, so I won't guess.", "abstained"
        top = r.data[0]
        return "question", f"{top['text']} {cite(top['cite_as'])}", "answered"
    except _Failed as f:
        return "error", (f"I can't reach one of the systems I need ({f.result.message.split(':')[0]}), so I can't "
                         "investigate right now and I won't guess. Please try again shortly."), "error"


SYSTEM_PROMPT = """You are the integration failure investigation assistant for an SAP application management team.
You help {actor}. All data is fictional sample data. Today's date is {today}.
- Use investigate to group failures and check preconditions in SAP (vendor exists, system health). Never guess a cause.
- Cite every fact with cite_as ids, e.g. [msg:MSG-5004] [vendor:300087] [sap_health:latest] [kb:integration_runbook#2].
- request_reprocess only queues a request for the integration lead; say so. Payment files (PAY-AUTH) and bank statements (MT940-DUP) are never reprocessed.
- You cannot resend payment files, re-import statements, delete messages or change partner profiles.
- If a tool reports an error, say the system is unavailable and don't guess. Keep answers short."""


class IntegrationAgent(BaseAgent):
    key = "integration"
    executor_cls = IntegrationExecutor
    rules_planner = staticmethod(plan)
    system_prompt = SYSTEM_PROMPT
    fact_patterns = {"id": re.compile(r"\b(?:MSG-\d{4}|ACT-\d{4})\b"), "vendor": re.compile(r"\b3\d{5}\b")}

    def clients(self):
        return {"integration": ApiClient(config.INTEGRATION_URL, "integration"),
                "sap": ApiClient(config.SAP_URL, "sap")}
