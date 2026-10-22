"""Use case 3, change impact agent: Task 2 (APIs) and Task 3 (citations, approval, limits)."""
import httpx
import pytest

from agents import actions
from agents.change import ChangeAgent, ChangeRequest, CmdbObject
from mock_apis import launcher
from support_agent.api_client import ApiClient, SchemaError

agent = ChangeAgent(mode="rules")


def assessments(cr):
    return httpx.get(f"{launcher.CHANGES_URL}/changes/{cr}/assessments").json()


# ---- Task 2 ----------------------------------------------------------------------
def test_change_and_cmdb_schemas_with_retry():
    launcher.set_faults(launcher.CMDB_URL, ["500", "503"])
    cmdb = ApiClient(launcher.CMDB_URL, "cmdb")
    assert cmdb.request("GET", "/objects/ZSD_PRICING_901", CmdbObject).criticality == "high"
    assert cmdb.calls[-1].attempts == 3
    cr = ApiClient(launcher.CHANGES_URL, "changes").request("GET", "/changes/CR-502", ChangeRequest)
    assert cr.status == "awaiting_assessment"


def test_malformed_change_is_rejected():
    launcher.set_faults(launcher.CHANGES_URL, ["malformed"])
    with pytest.raises(SchemaError):
        ApiClient(launcher.CHANGES_URL, "changes").request("GET", "/changes/CR-501", ChangeRequest)


def test_cmdb_outage_gives_honest_error():
    launcher.set_faults(launcher.CMDB_URL, ["503", "503", "503"])
    r = agent.ask("change.manager", "Assess the impact of CR-502")
    assert r.status == "error" and "CAB" not in r.answer


def test_approved_submission_happens_once():
    r = agent.ask("change.manager", "Submit CR-502 to CAB")
    launcher.set_faults(launcher.CHANGES_URL, ["slow"])
    actions.decide(r.approval_id, "change.manager", approve=True)
    actions.decide(r.approval_id, "change.manager", approve=True)
    assert len(assessments("CR-502")) == 1


# ---- Task 3 ----------------------------------------------------------------------
def test_assessment_cites_cmdb_and_rules():
    r = agent.ask("change.manager", "Assess the impact of CR-502")
    assert r.status == "answered" and r.raw_grounded
    assert {"cr:CR-502", "cmdb:ZSD_PRICING_901", "kb:change_assessment_rules#1",
            "kb:change_assessment_rules#3"} <= set(r.citations)          # high criticality + short notice


def test_unknown_object_is_flagged_not_assumed_safe():
    r = agent.ask("change.manager", "Assess CR-504")
    assert "ZFI_BANK_EXIT is not in the CMDB" in r.answer
    assert "kb:change_assessment_rules#5" in r.citations


def test_submission_waits_for_change_manager():
    r = agent.ask("change.manager", "Submit CR-501 to CAB")
    assert r.status == "pending_approval" and assessments("CR-501") == []


def test_approved_change_is_not_reassessed():
    assert agent.ask("change.manager", "Assess CR-505").status == "denied"


def test_restricted_actions_blocked():
    for q in ("Approve CR-502", "Import the CR-501 transport to production", "Skip the regression tests for CR-503"):
        assert agent.ask("change.manager", q).status == "blocked"
    assert actions.list_all() == []


def test_off_topic_question_about_a_change_abstains():
    assert agent.ask("change.manager", "What is the budget for CR-501?").status == "abstained"
