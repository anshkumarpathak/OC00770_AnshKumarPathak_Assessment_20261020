"""The record editor: an edit is written only if it passes the agent's own schema, and the agent then uses it."""
import json

import httpx

from agents.change import ChangeAgent
from agents.integration import Message
from agents.sla import Contract
from mock_apis import launcher
from support_agent.records import check_record
from support_agent.schemas import Invoice

INV = {"invoice_id": "INV-5102", "client_id": "CL01", "po_id": "PO-4502", "vendor": "Agro Spices Co", "amount": 79200.0,
       "status": "blocked", "block_reason": "price_variance", "posted_at": "2026-10-19", "due_date": "2026-11-18",
       "paid_at": None}


def check(change: dict, original=INV, schema=Invoice, key="invoice_id"):
    return check_record(json.dumps({**original, **change}), original, schema, key)


def test_a_valid_edit_passes():
    record, reasons = check({"amount": 81000.0})
    assert reasons == [] and record["amount"] == 81000.0


def test_bad_edits_are_rejected_with_the_reason():
    assert "Not valid JSON" in check_record('{"invoice_id": "INV-5102",', INV, Invoice, "invoice_id")[1][0]
    assert "identifies the record" in check({"invoice_id": "INV-9999"})[1][0]
    assert "can't be added" in check({"discount": 5})[1][0]
    removed = {k: v for k, v in INV.items() if k != "vendor"}
    assert "can't be removed" in check_record(json.dumps(removed), INV, Invoice, "invoice_id")[1][0]
    assert any("amount" in r for r in check({"amount": "lots"})[1])                       # wrong type
    assert any("block_reason" in r for r in check({"block_reason": None})[1])             # cross-field rule


def test_contract_must_keep_a_rule_for_every_priority():
    contract = httpx.get(f"{launcher.CONTRACTS_URL}/_admin/records/contracts/CL02").json()
    fewer = {**contract, "sla_rules": contract["sla_rules"][:3]}
    _, reasons = check_record(json.dumps(fewer), contract, Contract, "client_id")
    assert any("P1-P4" in r for r in reasons)


def test_message_status_must_be_a_known_value():
    msg = httpx.get(f"{launcher.INTEGRATION_URL}/_admin/records/messages/MSG-5001").json()
    _, reasons = check_record(json.dumps({**msg, "status": "stuck"}), msg, Message, "msg_id")
    assert any("status" in r for r in reasons)


def test_saved_edit_changes_what_the_agent_says_and_reset_restores_it():
    url = f"{launcher.CMDB_URL}/_admin/records/objects/ZSD_PRICING_901"
    obj = httpx.get(url).json()
    assert ChangeAgent().ask("solution.consultant", "Assess the impact of CR-502").answer.count("CAB") >= 1
    httpx.put(url, json={**obj, "criticality": "low", "interfaces": []}).raise_for_status()
    answer = ChangeAgent().ask("solution.consultant", "Assess the impact of CR-502").answer
    assert "business owner sign-off" in answer and "CAB approval" not in answer, answer
    launcher.reset_all()
    assert httpx.get(url).json()["criticality"] == "high"


def test_only_listed_tables_are_editable():
    assert httpx.get(f"{launcher.SAP_URL}/_admin/records/pos/PO-4501").status_code == 404
    assert httpx.put(f"{launcher.SAP_URL}/_admin/records/invoices/INV-0000", json={}).status_code == 404
