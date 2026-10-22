"""Use case 5, integration failure agent: Task 2 (APIs) and Task 3 (citations, approval, limits)."""
import httpx
import pytest

from agents import actions
from agents.integration import Health, IntegrationAgent, Message
from mock_apis import launcher
from support_agent.api_client import ApiClient, SchemaError

agent = IntegrationAgent(mode="rules")


def reprocess_log():
    return httpx.get(f"{launcher.INTEGRATION_URL}/reprocess-log").json()


# ---- Task 2 ----------------------------------------------------------------------
def test_message_and_health_schemas_with_retry():
    launcher.set_faults(launcher.INTEGRATION_URL, ["503"])
    mon = ApiClient(launcher.INTEGRATION_URL, "integration")
    msgs = mon.request("GET", "/messages", Message, many=True, params={"status": "failed"})
    assert len(msgs) == 8 and mon.calls[-1].attempts == 2
    assert ApiClient(launcher.SAP_URL, "sap").request("GET", "/system/health", Health).status == "green"


def test_malformed_message_is_rejected():
    launcher.set_faults(launcher.INTEGRATION_URL, ["malformed"])
    with pytest.raises(SchemaError):
        ApiClient(launcher.INTEGRATION_URL, "integration").request("GET", "/messages/MSG-5004", Message)


def test_sap_outage_blocks_precondition_check_honestly():
    launcher.set_faults(launcher.SAP_URL, ["503", "503", "503"])
    r = agent.ask("integration.consultant", "Why is the POS upload failing?")
    assert r.status == "error" and "Ready to reprocess" not in r.answer


def test_approved_reprocessing_runs_once():
    r = agent.ask("integration.consultant", "Reprocess the POS sales upload")
    launcher.set_faults(launcher.INTEGRATION_URL, ["slow"])
    actions.decide(r.approval_id, "integration.lead", approve=True)
    actions.decide(r.approval_id, "integration.lead", approve=True)
    assert len(reprocess_log()) == 1


# ---- Task 3 ----------------------------------------------------------------------
def test_overview_groups_failures_with_citations():
    r = agent.ask("integration.consultant", "What failed overnight?")
    assert r.status == "answered" and r.raw_grounded
    assert {"kb:integration_runbook#1", "kb:integration_runbook#2", "vendor:300087", "sap_health:latest"} <= set(r.citations)


def test_reprocessing_waits_for_integration_lead():
    r = agent.ask("integration.consultant", "Reprocess the POS sales upload")
    assert r.status == "pending_approval" and reprocess_log() == []


def test_missing_vendor_blocks_reprocessing():
    r = agent.ask("integration.consultant", "Reprocess MSG-5001")
    assert r.status == "denied" and "vendor:300087" in r.citations
    assert actions.list_all() == []


def test_payment_and_bank_files_are_never_reprocessed():
    for q in ("Resend the payment file", "Reprocess MSG-5008", "Import the bank statement again", "Reprocess MSG-5007"):
        assert agent.ask("integration.consultant", q).status == "blocked"
    assert actions.list_all() == [] and reprocess_log() == []


def test_unknown_question_abstains():
    assert agent.ask("integration.consultant", "What is the SAP license cost?").status == "abstained"
