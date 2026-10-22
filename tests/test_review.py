"""Review before saving: revise the draft, approve exactly that text, apply it once, before it lapses."""
import json

import httpx
import pytest

from agents import actions
from agents.sla import SlaAgent
from mock_apis import launcher
from support_agent import approvals, config
from support_agent.agent import SupportAgent
from support_agent.review import ReviewError


def _escalation() -> str:
    r = SlaAgent().ask("duty.lead", "Escalate all breached tickets")
    assert r.status == "pending_approval"
    return r.approval_id


def _audit(event: str) -> list[dict]:
    lines = (config.RUNTIME_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == event]


def test_revised_text_is_what_lands_in_the_system():
    aid = _escalation()
    draft = actions.list_all("pending")[0]["text"]
    assert "INC-2008" in draft and "Reason given" in draft
    revised = draft + "\nCall the Meridian service manager before noon."
    actions.revise(aid, revised, "delivery.manager")
    actions.approve_text(aid, revised, "delivery.manager")
    actions.apply(aid, "delivery.manager")
    sent = httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/escalations").json()
    assert [e["note"] for e in sent] == [revised]
    assert _audit("draft_revised")[0]["before"] == draft
    assert _audit("text_approved") and _audit("action_decided")[-1]["status"] == "approved"
    with pytest.raises(ReviewError):                      # applying twice can't send it twice
        actions.apply(aid, "delivery.manager")
    assert len(httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/escalations").json()) == 1


def test_only_the_saved_text_can_be_approved():
    aid = _escalation()
    with pytest.raises(ReviewError, match="Save the revision first"):
        actions.approve_text(aid, "something else entirely", "delivery.manager")
    with pytest.raises(ReviewError, match="Approve the exact text first"):
        actions.apply(aid, "delivery.manager")


def test_editing_after_approval_needs_a_new_approval():
    aid = _escalation()
    text = actions.list_all("pending")[0]["text"]
    actions.approve_text(aid, text, "delivery.manager")
    actions.revise(aid, text + " (edited)", "delivery.manager")
    with pytest.raises(ReviewError, match="Approve the exact text first"):
        actions.apply(aid, "delivery.manager")
    assert httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/escalations").json() == []


def test_approval_lapses_if_not_applied_in_time(monkeypatch):
    aid = _escalation()
    monkeypatch.setattr(config, "APPROVAL_TTL_S", 0)
    actions.approve_text(aid, actions.list_all("pending")[0]["text"], "delivery.manager")
    with pytest.raises(ReviewError, match="lapsed"):
        actions.apply(aid, "delivery.manager")
    item = actions.list_all("pending")[0]
    assert "approval" not in item and httpx.get(f"{launcher.SERVICE_DESK_URL}/queue/escalations").json() == []


def test_withdraw_reopens_the_draft():
    aid = _escalation()
    actions.approve_text(aid, actions.list_all("pending")[0]["text"], "delivery.manager")
    actions.withdraw(aid, "delivery.manager")
    assert "approval" not in actions.list_all("pending")[0]
    assert _audit("approval_withdrawn")


def test_role_request_carries_the_approved_justification():
    r = SupportAgent().ask("U001", "Please give me Z_MM_DISPLAY so I can see purchase orders")
    item = approvals.list_all("pending")[0]
    assert item["approval_id"] == r.approval_id and "Z_MM_DISPLAY" in item["text"]
    text = item["text"] + "\nApproved for the October stock count only."
    approvals.revise(r.approval_id, text, "kestrel.auth.owner")
    approvals.approve_text(r.approval_id, text, "kestrel.auth.owner")
    approvals.apply(r.approval_id, "kestrel.auth.owner")
    docs = httpx.get(f"{launcher.SAP_URL}/sap-users/ASHAV/changes").json()
    assert docs[-1]["note"] == text and "Z_MM_DISPLAY" in docs[-1]["change"]
    assert approvals.list_all()[0]["status"] == "approved"
