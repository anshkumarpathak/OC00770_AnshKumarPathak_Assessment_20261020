"""Use case 3: SAP change-request impact assessment agent.

Who uses it: an Orane change manager or solution consultant.
Two enterprise APIs: change management (change requests, 8104) and the CMDB
(dependency catalogue, 8105). Runbooks: change assessment rules and the change
and transport policy. It finds affected applications, interfaces, test suites
and the approvals a change needs, with citations. Unknown objects are flagged,
never assumed safe. Submitting an assessment to CAB needs the change manager's
approval; approving changes, importing transports or waiving tests is never possible.
"""
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agents import actions
from agents.base import BaseAgent, BaseExecutor, NoArgs, SearchArgs, cite, dump, src
from support_agent import config
from support_agent.api_client import ApiClient, NotFound
from support_agent.tools import ToolResult, business_days_between


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChangeRequest(_Base):
    cr_id: str = Field(pattern=r"^CR-\d{3}$")
    client: str
    title: str
    objects: list[str] = Field(min_length=1)
    target_date: date
    requested_by: str
    status: Literal["awaiting_assessment", "assessed", "approved", "rejected"]


class CmdbObject(_Base):
    object: str
    application: str
    interfaces: list[str]
    test_suite: str = Field(pattern=r"^TS-[A-Z]{2}-[A-Z]{2,3}-\d{2}$")
    business_owner: str
    criticality: Literal["low", "medium", "high"]


class CrArgs(BaseModel):
    cr_id: str = Field(pattern=r"^CR-\d{3}$")


class ObjectArgs(BaseModel):
    object: str = Field(pattern=r"^Z[A-Z0-9_]{2,40}$")


class StatusArgs(BaseModel):
    status: str | None = None


RULES = {"high": "change_assessment_rules#1", "interfaces": "change_assessment_rules#2",
         "short_notice": "change_assessment_rules#3", "low": "change_assessment_rules#4",
         "unknown": "change_assessment_rules#5", "decision": "change_assessment_rules#6"}
RULE_TEXT = {"high": "CAB approval and a full regression run", "interfaces": "integration test with each connected system",
             "short_notice": "expedited CAB and client IT head sign-off", "low": "business owner sign-off",
             "unknown": "manual dependency review by the solution architect before CAB"}


class ChangeExecutor(BaseExecutor):
    KB_DOCS = ("change_assessment_rules", "change_policy")
    DENIED = {
        "approve_change": "Only the Change Advisory Board approves changes.",
        "import_transport_to_production": "Production imports happen only after CAB approval, in the weekly window.",
        "waive_testing": "Required tests and CAB steps are never skipped.",
        "mark_tests_passed": "Test results are recorded by testers, not by the assistant.",
    }

    def list_changes(self, args: StatusArgs) -> ToolResult:
        crs = self.clients["changes"].request("GET", "/changes", ChangeRequest, many=True,
                                              params={"status": args.status} if args.status else None)
        data = [dump(c) for c in crs]
        return ToolResult("ok", f"{len(data)} changes", data,
                          {k: v for c in data for k, v in src("cr", c["cr_id"], c).items()})

    def get_change(self, args: CrArgs) -> ToolResult:
        c = self.clients["changes"].request("GET", f"/changes/{args.cr_id}", ChangeRequest)
        return ToolResult("ok", "change loaded", dump(c), src("cr", c.cr_id, dump(c)))

    def lookup_object(self, args: ObjectArgs) -> ToolResult:
        try:
            o = self.clients["cmdb"].request("GET", f"/objects/{args.object}", CmdbObject)
        except NotFound:
            return ToolResult("ok", f"{args.object} is not in the CMDB", {"object": args.object, "in_cmdb": False},
                              src("cmdb", args.object, {"object": args.object, "in_cmdb": False}))
        return ToolResult("ok", "object loaded", {**dump(o), "in_cmdb": True}, src("cmdb", o.object, dump(o)))

    def assess_impact(self, args: CrArgs) -> ToolResult:
        cr_r = self.get_change(args)
        cr = ChangeRequest.model_validate(cr_r.data)
        self.evidence.update(cr_r.sources)
        if cr.status != "awaiting_assessment":
            kb = self.kb(RULES["decision"])
            return ToolResult("denied", f"{cr.cr_id} is already {cr.status.replace('_', ' ')}; it is not reassessed.",
                              dump(cr), {**cr_r.sources, **{k: self.evidence[k] for k in kb}})
        found, unknown = [], []
        for name in cr.objects:
            r = self.lookup_object(ObjectArgs(object=name))
            self.evidence.update(r.sources)
            (found if r.data["in_cmdb"] else unknown).append(r.data)
        interfaces = sorted({i for o in found for i in o["interfaces"]})
        high = any(o["criticality"] == "high" for o in found)
        days = business_days_between(config.TODAY, cr.target_date)
        needed = (["high"] if high else []) + (["interfaces"] if interfaces else []) + \
                 (["short_notice"] if high and days <= config.CHANGE_SHORT_NOTICE_BUSINESS_DAYS else []) + \
                 (["unknown"] if unknown else [])
        if not needed:
            needed = ["low"]
        kb = self.kb(*[RULES[n] for n in needed])
        data = {"cr_id": cr.cr_id, "title": cr.title, "client": cr.client, "target_date": str(cr.target_date),
                "business_days_to_target": days, "affected": found, "unknown_objects": [u["object"] for u in unknown],
                "interfaces": interfaces, "test_suites": [o["test_suite"] for o in found],
                "approvals": [RULE_TEXT[n] for n in needed], "approval_rules": [RULES[n] for n in needed],
                "sign_off": sorted({o["business_owner"] for o in found})}
        sources = {**src("cr", cr.cr_id, {**dump(cr), "business_days_to_target": days}),
                   **{f"cmdb:{o['object']}": self.evidence[f"cmdb:{o['object']}"] for o in found + unknown},
                   **{k: self.evidence[k] for k in kb}}
        return ToolResult("ok", f"{len(needed)} approval rules apply", data, sources)

    def submit_assessment(self, args: CrArgs) -> ToolResult:
        r = self.assess_impact(args)
        if r.status != "ok":
            return r
        self.evidence.update(r.sources)
        if actions.pending_for("change", "cr_ids", args.cr_id):
            return ToolResult("denied", f"An assessment for {args.cr_id} is already waiting for the change manager.",
                              r.data, r.sources)
        d = r.data
        summary = (f"{d['cr_id']} {d['title']}: affects {', '.join(o['application'] for o in d['affected'])}"
                   + (f"; unknown objects {', '.join(d['unknown_objects'])}" if d["unknown_objects"] else "")
                   + f"; needs {', '.join(d['approvals'])}")
        item = actions.create("change", "submit_cab_assessment", summary,
                              {"cr_ids": [d["cr_id"]], "summary": summary, "approvals_needed": d["approvals"]},
                              self.actor, self.trace.trace_id)
        self.approval_id = item["action_id"]
        kb = self.kb(RULES["decision"])
        return ToolResult("pending_approval", f"Assessment {item['action_id']} waits for the change manager.",
                          {**d, "action_id": item["action_id"]},
                          {**r.sources, **src("approval", item["action_id"], item), **{k: self.evidence[k] for k in kb}})


ChangeExecutor.TOOLS = {
    "list_changes": (StatusArgs, ChangeExecutor.list_changes, "List change requests, optionally by status."),
    "get_change": (CrArgs, ChangeExecutor.get_change, "Get one change request."),
    "lookup_object": (ObjectArgs, ChangeExecutor.lookup_object, "Look up an SAP object in the CMDB."),
    "assess_impact": (CrArgs, ChangeExecutor.assess_impact,
                      "Assess a change: affected applications, interfaces, tests and required approvals."),
    "search_runbook": (SearchArgs, ChangeExecutor.search_runbook, "Search the change assessment rules and policy."),
    "submit_assessment": (CrArgs, ChangeExecutor.submit_assessment,
                          "Prepare the assessment for CAB. It is submitted only after the change manager approves."),
}


@actions.handler("submit_cab_assessment")
def _submit(payload: dict, action_id: str, approver: str) -> dict:
    class Assessment(_Base):
        assessment_id: str
        cr_id: str

    a = ApiClient(config.CHANGES_URL, "changes").request(
        "POST", f"/changes/{payload['cr_ids'][0]}/assessments", Assessment, idempotency_key=action_id,
        json={"summary": payload["summary"], "approvals_needed": payload["approvals_needed"], "approved_by": approver})
    return {"assessment_id": a.assessment_id}


# ---- rule-based planner --------------------------------------------------------------
CR_RE = re.compile(r"\bCR-\d{3}\b", re.I)
DENIED_PATTERNS = [
    (r"\bapprove\b.*\b(cr-\d{3}|change)\b|\bmark\b.*\bapproved\b", "approve_change", "approve changes",
     "change_assessment_rules#6"),
    (r"\b(import|move|push|transport)\b.*\b(production|prod|prd)\b", "import_transport_to_production",
     "import transports to production", "change_policy#2"),
    (r"\b(skip|waive|bypass|ignore)\b.*\b(test|tests|testing|regression|cab)\b", "waive_testing",
     "skip tests or CAB steps", "change_assessment_rules#1"),
    (r"\bmark\b.*\b(tests?|regression)\b.*\b(passed|done|complete)", "mark_tests_passed",
     "mark tests as passed", "change_assessment_rules#1"),
]
QUESTION_RE = re.compile(r"(how|what|when|why|which|who|is|are|do|does|can i|should)\b")


class _Failed(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def _ok(r: ToolResult) -> ToolResult:
    if r.status == "error":
        raise _Failed(r)
    return r


def _describe(d: dict, ex: ChangeExecutor) -> str:
    parts = [f"{d['cr_id']} {d['title']} {cite('cr:' + d['cr_id'])}"]
    for o in d["affected"]:
        parts.append(f"touches {o['object']} ({o['application']}, {o['criticality']} criticality) "
                     f"{cite('cmdb:' + o['object'])}")
    for u in d["unknown_objects"]:
        parts.append(f"{u} is not in the CMDB, so its dependencies are unknown {cite('cmdb:' + u)}")
    text = "; ".join(parts) + ". "
    if d["interfaces"]:
        text += f"Interfaces to test: {', '.join(d['interfaces'])}. "
    if d["test_suites"]:
        text += f"Test suites: {', '.join(d['test_suites'])}. "
    reqs = "; ".join(f"{a} {cite('kb:' + r)}" for a, r in zip(d["approvals"], d["approval_rules"]))
    text += f"Approvals needed: {reqs}"
    if "short_notice" in " ".join(d["approval_rules"]) or any("#3" in r for r in d["approval_rules"]):
        text += f" (the target is {d['business_days_to_target']} business days away)"
    text += "."
    if d["sign_off"]:
        text += f" Business sign-off: {', '.join(d['sign_off'])}."
    return text


def plan(ex: ChangeExecutor, message: str) -> tuple[str, str, str]:
    m = message.lower()
    ids = [c.upper() for c in CR_RE.findall(message)]
    try:
        for pattern, tool, action, chunk in DENIED_PATTERNS:
            if re.search(pattern, m) and not QUESTION_RE.match(m):
                ex.execute(tool, {"cr_ids": ids})
                kb = ex.kb(chunk)
                text = ex.evidence[kb[0]].split(": ", 1)[1]
                return f"denied:{tool}", f"I'm not able to {action}. {text} {cite(*kb)}", "blocked"

        if re.search(r"\b(submit|send|raise|prepare)\b.*\b(cab|assessment)\b", m) and ids:
            r = ex.execute("submit_assessment", {"cr_id": ids[0]})
            if r.status == "error":
                raise _Failed(r)
            if r.status == "pending_approval":
                kb = [s for s in r.sources if s.endswith("#6")]
                return "submit", (f"{_describe(r.data, ex)} I've prepared assessment {r.data['action_id']} for CAB. "
                                  f"The change manager reviews it before it is submitted, and CAB decides. "
                                  f"{cite('approval:' + r.data['action_id'], *kb)}"), "pending_approval"
            return "submit", f"I can't submit that. {r.message} {cite(*r.sources)}", r.status

        if ids and QUESTION_RE.match(m) and not re.search(
                r"impact|approv|test|need|affect|depend|interface|sign|risk|cab|assess|status|object|change", m):
            return "assess", (f"I can only assess impact, dependencies, tests and approvals for {ids[0]}; I don't "
                              "have information about that, so I won't guess."), "abstained"

        if ids:
            r = ex.execute("assess_impact", {"cr_id": ids[0]})
            if r.status == "error":
                raise _Failed(r)
            if r.status == "denied" and "Not found" in r.message:
                return "assess", f"I couldn't find change request {ids[0]}.", "denied"
            if r.status != "ok":
                return "assess", f"{r.message} {cite(*r.sources)}", r.status
            return "assess", _describe(r.data, ex) + " I can prepare this assessment for CAB.", "answered"

        if re.search(r"\b(open|pending|waiting|which|list|all)\b.*\b(changes?|crs?|requests?)\b", m):
            r = _ok(ex.execute("list_changes", {"status": "awaiting_assessment"}))
            lines = "; ".join(f"{c['cr_id']} {c['title']} (due {c['target_date']}) {cite('cr:' + c['cr_id'])}"
                              for c in r.data)
            return "list", f"{len(r.data)} changes are awaiting assessment: {lines}.", "answered"

        r = _ok(ex.execute("search_runbook", {"query": message}))
        if not r.data:
            return "question", "I don't have information about that in the change runbooks, so I won't guess.", "abstained"
        top = r.data[0]
        return "question", f"{top['text']} {cite(top['cite_as'])}", "answered"
    except _Failed as f:
        return "error", (f"I can't reach one of the systems I need ({f.result.message.split(':')[0]}), so I can't "
                         "assess the change right now and I won't guess. Please try again shortly."), "error"


SYSTEM_PROMPT = """You are the change impact assessment assistant for an SAP application management team.
You help {actor}. All data is fictional sample data. Today's date is {today}.
- Use assess_impact for the impact of a change request; never guess dependencies. Objects not in the CMDB are unknown, not low impact.
- Cite every fact with cite_as ids, e.g. [cr:CR-502] [cmdb:ZSD_PRICING_901] [kb:change_assessment_rules#1].
- submit_assessment only prepares the CAB submission; say it waits for the change manager. CAB decides.
- You cannot approve changes, import transports, skip tests or mark tests passed. Explain why using the runbook.
- If a tool reports an error, say the system is unavailable and don't guess. Keep answers short."""


class ChangeAgent(BaseAgent):
    key = "change"
    executor_cls = ChangeExecutor
    rules_planner = staticmethod(plan)
    system_prompt = SYSTEM_PROMPT
    fact_patterns = {"id": re.compile(r"\b(?:CR-\d{3}|ACT-\d{4}|TS-[A-Z]{2}-[A-Z]{2,3}-\d{2})\b"),
                     "object": re.compile(r"\bZ[A-Z]{1,3}_[A-Z0-9_]+\b")}

    def clients(self):
        return {"changes": ApiClient(config.CHANGES_URL, "changes"), "cmdb": ApiClient(config.CMDB_URL, "cmdb")}
