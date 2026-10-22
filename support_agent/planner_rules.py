"""Deterministic planner: works offline, gives repeatable demo and eval runs.

It follows a fixed workflow: classify the request -> call tools through the
ToolExecutor -> compose an answer only from tool results, with citations.
It deliberately maps risky requests to the risky tool name (e.g. "delete SAP
user X" calls delete_sap_user), so the demo shows the *guard layer* stopping
it, the same protection that applies when Claude is the planner.
"""
import re

from support_agent import config
from support_agent.tools import ToolExecutor, ToolResult

PO_RE = re.compile(r"\b(PO-\d{4})\b", re.I)
INV_RE = re.compile(r"\b(INV-\d{4})\b", re.I)
ROLE_RE = re.compile(r"\b(Z_[A-Z0-9_]+|SAP_ALL|SAP_NEW)\b", re.I)
OTHER_USER_RE = re.compile(r"\b(?:user|account)\s+([A-Z][A-Z0-9]{3,11})\b")   # e.g. "unlock user KABIRS"

# Response times by plan and priority (mirror kb/sla_policy.md, which is cited)
RESPONSE = {("premium", "P1"): "15 minutes", ("premium", "P2"): "1 hour", ("premium", "P3"): "4 business hours",
            ("premium", "P4"): "1 business day", ("standard", "P1"): "30 minutes", ("standard", "P2"): "2 hours",
            ("standard", "P3"): "8 business hours", ("standard", "P4"): "2 business days"}

# pattern, denied tool, what the user asked for, the policy section that explains why
DENIED_PATTERNS = [
    (r"\b(delete|remove|wipe|purge)\b.*\b(user|account)\b", "delete_sap_user", "delete SAP users",
     "data_security#2"),
    (r"\b(reset|change)\b.*\bpassword\b|\bnew password\b", "reset_sap_password", "reset passwords",
     "access_policy#1"),
    (r"\b(release|remove|lift|clear)\b.*\bblock\b|\bunblock\b", "release_payment_block",
     "release payment blocks", "finance_runbook#3"),
    (r"\bbank (account|details)\b", "change_vendor_bank_details", "change vendor bank details",
     "finance_runbook#4"),
    (r"\b(import|move|push|release)\b.*\b(to|into|in) (production|prod|prd)\b", "import_transport_to_production",
     "import transports to production", "change_policy#2"),
    (r"\bpayroll\b|\bsalary (data|details)\b|\bemployee (personal )?data\b", "export_payroll_data",
     "export payroll or employee data", "data_security#1"),
]
QUESTION_RE = re.compile(r"(how|what|what's|when|why|which|who|where|is|are|do|does|can i|could i|should i|will)\b")
OUTAGE_RE = re.compile(r"\b(production|system|sap|prd)\b.*\bdown\b|\bnobody can\b|\ball users\b|month-end.*\b(blocked|stopped)\b")


def cite(*source_ids: str) -> str:
    return " ".join(f"[{s}]" for s in dict.fromkeys(source_ids))


def classify(message: str) -> str:
    """Order matters: lookups of one's own data, then specific requests, then general questions."""
    m = message.lower()
    has_doc = bool(PO_RE.search(message) or INV_RE.search(message))
    if re.search(r"\bmy (support )?tickets\b", m):
        return "list_tickets"
    if re.search(r"\bmy (sap )?roles\b|roles do i have|\bmy access\b", m):
        return "my_sap_user"
    if ROLE_RE.search(message):
        return "role_request"
    if not has_doc and QUESTION_RE.match(m):
        return "policy_question"       # a general question, not a request to act
    for pattern, tool, *_ in DENIED_PATTERNS:
        if re.search(pattern, m):
            return f"denied:{tool}"
    if INV_RE.search(message):
        return "invoice"
    if PO_RE.search(message):
        return "purchase_order"
    if re.search(r"\b(new|a|another) role\b|\brole (request|access)\b|\bauthori[sz]ation\b", m):
        return "role_request"
    if re.search(r"\b(un)?lock(ed)?\b|locked out|can'?t log ?in|cannot log ?in", m):
        return "unlock"
    if re.search(r"\b(human|consultant|person|someone|manager|escalate|call me|call back|talk to|speak to)\b", m):
        return "escalate"
    if OUTAGE_RE.search(m) or re.search(r"\b(error|dump|fail\w*|not working|stuck|crash\w*|slow|unable)\b", m):
        return "issue"
    return "policy_question"


def run(ex: ToolExecutor, message: str) -> tuple[str, str, str]:
    """Returns (intent, answer, status)."""
    intent = classify(message)
    handler = HANDLERS[intent.split(":")[0]]
    try:
        answer, status = handler(ex, message, intent)
    except _ToolFailed as failed:
        answer, status = _system_problem(ex, message, failed.result)
    return intent, answer, status


class _ToolFailed(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def _call(ex: ToolExecutor, name: str, args: dict | None = None) -> ToolResult:
    result = ex.execute(name, args)
    if result.status == "error":
        raise _ToolFailed(result)
    return result


def _kb(ex: ToolExecutor, *chunk_ids: str) -> list[str]:
    """Add fixed policy sections to the evidence and return their citation ids."""
    found = ex._kb(*chunk_ids)
    ex.evidence.update(found)
    return list(found)


def _system_problem(ex: ToolExecutor, message: str, failed: ToolResult) -> tuple[str, str]:
    """A system we depend on is down or sent bad data: don't guess, hand over."""
    answer = ("Sorry, I can't reach one of the systems I need right now, so I can't confirm those details "
              "and I won't guess.")
    ticket = ex.execute("create_ticket", {"subject": "Follow-up needed: assistant could not reach a system",
                                          "description": message[:1500], "priority": "P3"})
    if ticket.status == "ok":
        tid = ticket.data["ticket_id"]
        answer += f" I've raised ticket {tid} so an Orane consultant can follow up. {cite(f'ticket:{tid}')}"
    else:
        answer += " Please try again in a few minutes."
    return answer, "error"


def _not_found(r: ToolResult, label: str) -> tuple[str, str]:
    return f"I couldn't find {label} for your company.", r.status


# ---- intent handlers ---------------------------------------------------------------

def handle_denied(ex, message, intent):
    tool = intent.split(":", 1)[1]
    _, _, action, chunk_id = next(p for p in DENIED_PATTERNS if p[1] == tool)
    ex.execute(tool, {})                                   # the guard refuses this
    kb = _kb(ex, chunk_id)
    text = ex.evidence[kb[0]].split(": ", 1)[1] if kb else ""
    return f"I'm not able to {action}. {text} {cite(*kb)}", "blocked"


def handle_escalate(ex, message, intent):
    profile = _call(ex, "get_my_profile")
    plan = profile.data["sla_plan"]
    ticket = _call(ex, "create_ticket", {"subject": "Key user asked for a consultant",
                                         "description": message[:1500], "priority": "P3"})
    kb = _kb(ex, "sla_policy#4")
    tid = ticket.data["ticket_id"]
    callback = "1 business hour" if plan == "premium" else "4 business hours"
    return (f"I've passed this to an Orane consultant: ticket {tid} {cite('ticket:' + tid)}. On your {plan} plan, "
            f"a consultant calls back within {callback}. {cite('user:' + ex.user_id, *kb)}"), "escalated"


def handle_issue(ex, message, intent):
    if OUTAGE_RE.search(message.lower()):
        return _raise_ticket(ex, message, "P1", "Possible production outage reported by key user")
    r = _call(ex, "search_knowledge", {"query": message})
    if r.data:
        top = r.data[0]
        return (f"{top['text']} {cite(top['cite_as'])} If that doesn't solve it, I can raise a ticket for an "
                f"Orane consultant."), "answered"
    return _raise_ticket(ex, message, "P3", message[:120])


def _raise_ticket(ex, message, priority, subject):
    profile = _call(ex, "get_my_profile")
    plan = profile.data["sla_plan"]
    ticket = _call(ex, "create_ticket", {"subject": subject, "description": message[:1500], "priority": priority})
    tid = ticket.data["ticket_id"]
    kb = _kb(ex, "sla_policy#2") + (_kb(ex, "sla_policy#3") if priority == "P1" else [])
    answer = (f"I've raised {priority} ticket {tid} {cite('ticket:' + tid)}. On your {plan} plan, an Orane "
              f"consultant responds to {priority} within {RESPONSE[(plan, priority)]}.")
    if priority == "P1":
        answer += " P1 incidents are handled 24x7."
    return f"{answer} {cite('user:' + ex.user_id, *kb)}", "escalated"


def handle_unlock(ex, message, intent):
    other = OTHER_USER_RE.search(message)
    args = {"sap_user": other.group(1)} if other else {}
    r = ex.execute("unlock_sap_user", args)
    if r.status == "error":
        raise _ToolFailed(r)
    srcs = list(r.sources)
    if r.status == "ok":
        kb = _kb(ex, "access_policy#1")
        user = r.data["sap_user"]
        return (f"Done: your SAP user {user} is unlocked and you can log in again. If you've forgotten your "
                f"password, reset it in the self-service password portal. {cite(*srcs, *kb)}"), "answered"
    if r.status == "denied" and "administrator" in r.message:
        ticket = _call(ex, "create_ticket", {"subject": "SAP user locked by administrator",
                                             "description": message[:1500], "priority": "P3"})
        tid = ticket.data["ticket_id"]
        return (f"{r.message} I've raised ticket {tid} so it reaches them. {cite(*srcs, 'ticket:' + tid)}"), "denied"
    if r.status == "denied" and "not locked" in r.message:
        kb = _kb(ex, "access_policy#1")
        return (f"{r.message} If you still can't log in, your password may have expired: reset it in the "
                f"self-service password portal. {cite(*srcs, *kb)}"), "denied"
    return f"I can't do that. {r.message} {cite(*srcs)}", r.status


def handle_role_request(ex, message, intent):
    m = ROLE_RE.search(message)
    if not m:
        kb = _kb(ex, "access_policy#5")
        roles = ", ".join(sorted(config.STANDARD_ROLES))
        return (f"Which role do you need? The standard roles you can request are {roles}. {cite(*kb)}"), "needs_info"
    r = ex.execute("request_role", {"role": m.group(1).upper(), "reason": message[:300]})
    if r.status == "error":
        raise _ToolFailed(r)
    srcs = list(r.sources)
    if r.status == "pending_approval":
        d = r.data
        return (f"I've requested {d['role']} for your SAP user. Every role must first be approved by your "
                f"company's authorisation owner: request {d['approval_id']} is waiting for them, and approved roles "
                f"are applied within 1 business day. {cite(*srcs)}"), "pending_approval"
    return f"I can't request that. {r.message} {cite(*srcs)}", r.status


def handle_purchase_order(ex, message, intent):
    po_id = PO_RE.search(message).group(1).upper()
    r = ex.execute("get_purchase_order", {"po_id": po_id})
    if r.status == "error":
        raise _ToolFailed(r)
    if r.status != "ok":
        return _not_found(r, f"purchase order {po_id}")
    p, src = r.data, f"po:{po_id}"
    head = f"{po_id} ({p['vendor']}, Rs {p['total']:,.0f})"
    if p["status"] == "awaiting_approval":
        answer = (f"{head} is awaiting approval by {p['approver']}. It was created on {p['created_at']}. "
                  f"{cite(src)}")
        kb = _kb(ex, "procurement_runbook#1")
        if p["approval_overdue"]:
            ticket = _call(ex, "create_ticket", {"subject": f"PO approval overdue for {po_id}",
                                                 "description": message[:1500], "priority": "P3"})
            tid = ticket.data["ticket_id"]
            answer += (f" It has been waiting {p['business_days_awaiting_approval']} business days, more than the "
                       f"3 business days allowed, so I've raised ticket {tid} to your procurement lead. "
                       f"{cite(*kb, 'ticket:' + tid)}")
        else:
            answer += (f" Purchase orders waiting more than 3 business days are escalated to your procurement "
                       f"lead. {cite(*kb)}")
        return answer, "answered"
    kb = _kb(ex, "procurement_runbook#2")
    if p["status"] == "approved":
        return (f"{head} was approved on {p['approved_at']} and can be sent to the vendor. {cite(src, *kb)}"), "answered"
    if p["status"] == "goods_received":
        return (f"{head}: the goods receipt has been posted, so its status is goods received. "
                f"{cite(src, *kb)}"), "answered"
    return f"{head} is {p['status'].replace('_', ' ')}. {cite(src, *kb)}", "answered"


def handle_invoice(ex, message, intent):
    inv_id = INV_RE.search(message).group(1).upper()
    r = ex.execute("get_invoice", {"invoice_id": inv_id})
    if r.status == "error":
        raise _ToolFailed(r)
    if r.status != "ok":
        return _not_found(r, f"invoice {inv_id}")
    i, src = r.data, f"invoice:{inv_id}"
    head = f"{inv_id} ({i['vendor']}, Rs {i['amount']:,.0f})"
    if i["status"] == "paid":
        return f"{head} was paid on {i['paid_at']}. {cite(src)}", "answered"
    if i["status"] == "posted":
        return f"{head} is posted and not blocked. It is due on {i['due_date']}. {cite(src)}", "answered"
    kb = _kb(ex, "finance_runbook#2", "finance_runbook#3")
    if i["block_reason"] == "price_variance":
        why = ("because of a price variance: the invoice price is higher than the purchase order price beyond "
               "the tolerance. Your procurement team reviews price variance blocks within 2 business days")
    elif i["block_reason"] == "missing_goods_receipt":
        why = (f"because no goods receipt has been posted against {i['po_id']}. The block clears automatically "
               f"once the goods receipt is posted in transaction MIGO")
    else:
        why = f"because of a {i['block_reason'].replace('_', ' ')}"
    return (f"{head} is blocked for payment {why}. {cite(src, kb[0])} Only your accounts payable lead can "
            f"release a payment block. {cite(*kb[1:])}"), "answered"


def handle_my_sap_user(ex, message, intent):
    r = _call(ex, "get_my_sap_user")
    u = r.data
    state = {"unlocked": "It is unlocked.", "locked_failed_logins": "It is locked after too many failed logins.",
             "locked_by_admin": "It is locked by an administrator."}[u["lock_status"]]
    return (f"Your SAP user {u['sap_user']} has these roles: {', '.join(u['roles'])}. {state} "
            f"{cite('sap_user:' + u['sap_user'])}"), "answered"


def handle_list_tickets(ex, message, intent):
    r = _call(ex, "list_my_tickets")
    if not r.data:
        return "You don't have any support tickets.", "answered"
    lines = "; ".join(f"{t['ticket_id']} \"{t['subject']}\" ({t['priority']}, {t['status']}) "
                      f"{cite('ticket:' + t['ticket_id'])}" for t in r.data)
    return f"Your tickets: {lines}", "answered"


def handle_policy_question(ex, message, intent):
    r = _call(ex, "search_knowledge", {"query": message})
    if not r.data:
        return ("I don't have information about that in our support runbooks, so I won't guess. "
                "I can raise a ticket for an Orane consultant if you'd like."), "abstained"
    top = r.data[0]
    return f"{top['text']} {cite(top['cite_as'])} (From the {top['document']}, \"{top['section']}\".)", "answered"


HANDLERS = {
    "denied": handle_denied,
    "escalate": handle_escalate,
    "issue": handle_issue,
    "unlock": handle_unlock,
    "role_request": handle_role_request,
    "purchase_order": handle_purchase_order,
    "invoice": handle_invoice,
    "my_sap_user": handle_my_sap_user,
    "list_tickets": handle_list_tickets,
    "policy_question": handle_policy_question,
}
