"""Use case 1, SLA escalation agent: Task 2 (APIs) and Task 3 (citations, approval, limits)."""
import httpx
import pytest

from agents import actions
from agents.sla import Contract, SlaAgent, SlaExecutor
from mock_apis import launcher
from support_agent.api_client import ApiClient, SchemaError, UpstreamUnavailable
from support_agent.tracing import Trace

agent = SlaAgent(mode="rules")


def escalations():
    return httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/escalations").json()


# ---- Task 2 ----------------------------------------------------------------------
def test_contract_schema_and_retry():
    launcher.set_faults(launcher.CONTRACTS_URL, ["503"])
    c = ApiClient(launcher.CONTRACTS_URL, "contracts")
    assert c.request("GET", "/contracts/CL02", Contract).sla_plan == "premium"
    assert c.calls[-1].attempts == 2


def test_malformed_contract_is_rejected():
    launcher.set_faults(launcher.CONTRACTS_URL, ["malformed"])
    with pytest.raises(SchemaError):
        ApiClient(launcher.CONTRACTS_URL, "contracts").request("GET", "/contracts/CL01", Contract)


def test_contracts_down_means_honest_error_not_a_guess():
    launcher.set_faults(launcher.CONTRACTS_URL, ["503", "503", "503"])
    r = agent.ask("duty.lead", "Which tickets are at risk of breaching SLA?")
    assert r.status == "error" and "INC-" not in r.answer


def test_approved_escalation_is_sent_once_even_if_retried():
    r = agent.ask("duty.lead", "Escalate INC-2002")
    launcher.set_faults(launcher.SERVICE_DESK_URL, ["slow"])          # reply is late, client retries
    actions.decide(r.approval_id, "delivery.manager", approve=True)
    actions.decide(r.approval_id, "delivery.manager", approve=True)   # double click
    assert len(escalations()) == 1


# ---- Task 3 ----------------------------------------------------------------------
def test_overview_is_cited_and_correct():
    r = agent.ask("duty.lead", "Which tickets are at risk of breaching SLA?")
    assert r.status == "answered" and r.raw_grounded
    assert {"ticket:INC-2002", "contract:CL01", "ticket:INC-2001"} <= set(r.citations)
    assert "INC-2004" not in r.answer                                  # on track, not listed


def test_escalation_waits_for_the_delivery_manager():
    r = agent.ask("duty.lead", "Escalate INC-2001 and INC-2003")
    assert r.status == "pending_approval" and escalations() == []
    assert "kb:escalation_procedure#3" in r.citations


def test_on_track_ticket_is_not_escalated():
    r = agent.ask("duty.lead", "Escalate INC-2004")
    assert r.status == "denied" and actions.list_all() == []


def test_sla_manipulation_is_blocked():
    for q in ("Lower the priority of INC-2007 to P4", "Close INC-2002", "Email the client about INC-2001"):
        assert agent.ask("duty.lead", q).status == "blocked"
    t = httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/tickets/INC-2007").json()
    assert t["priority"] == "P2"


def test_denied_tools_refused_even_when_called_directly():
    ex = SlaExecutor("x", Trace("x", "t", "rules", agent="sla"), agent.clients())
    for tool in ("close_ticket", "change_ticket_priority", "notify_client_directly", "send_escalation_now"):
        assert ex.execute(tool, {"ticket_ids": ["INC-2002"]}).status == "blocked"


def test_unknown_question_abstains():
    assert agent.ask("duty.lead", "What is the weather today?").status == "abstained"
