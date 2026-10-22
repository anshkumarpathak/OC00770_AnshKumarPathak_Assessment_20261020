"""Task 2: schema validation, retries and error handling against the live mock APIs."""
import httpx
import pytest

from mock_apis import launcher
from support_agent import api_client
from support_agent.schemas import Invoice, KeyUser, PurchaseOrder, SapUser


def test_valid_responses_parse_into_schemas():
    po = api_client.sap_client().request("GET", "/purchase-orders/PO-4501", PurchaseOrder)
    assert po.total == 184000.0 and po.status == "awaiting_approval"
    inv = api_client.sap_client().request("GET", "/invoices/INV-5102", Invoice)
    assert inv.block_reason == "price_variance"
    user = api_client.service_desk_client().request("GET", "/users/U003", KeyUser)
    assert user.sla_plan == "premium"


def test_transient_errors_are_retried_then_succeed():
    launcher.set_faults(launcher.SAP_URL, ["503", "500"])
    client = api_client.sap_client()
    po = client.request("GET", "/purchase-orders/PO-4502", PurchaseOrder)
    assert po.po_id == "PO-4502"
    assert client.calls[-1].attempts == 3
    assert client.calls[-1].errors == ["HTTP 503", "HTTP 500"]


def test_timeout_is_retried():
    launcher.set_faults(launcher.SERVICE_DESK_URL, ["slow"])
    client = api_client.service_desk_client()
    assert client.request("GET", "/users/U002", KeyUser).user_id == "U002"
    assert client.calls[-1].errors == ["ReadTimeout"]


def test_gives_up_after_max_attempts():
    launcher.set_faults(launcher.SAP_URL, ["503", "503", "503"])
    with pytest.raises(api_client.UpstreamUnavailable):
        api_client.sap_client().request("GET", "/purchase-orders/PO-4501", PurchaseOrder)


def test_rate_limit_is_retried_then_gives_up():
    launcher.set_faults(launcher.SAP_URL, ["429", "429", "429"])
    client = api_client.sap_client()
    with pytest.raises(api_client.UpstreamUnavailable):
        client.request("GET", "/invoices/INV-5101", Invoice)
    assert client.calls[-1].errors == ["HTTP 429"] * 3


def test_injected_not_found_maps_to_not_found():
    launcher.set_faults(launcher.SAP_URL, ["404"])
    with pytest.raises(api_client.NotFound):
        api_client.sap_client().request("GET", "/invoices/INV-5101", Invoice)


def test_malformed_payload_is_rejected_not_retried():
    launcher.set_faults(launcher.SAP_URL, ["malformed"])
    client = api_client.sap_client()
    with pytest.raises(api_client.SchemaError):
        client.request("GET", "/invoices/INV-5101", Invoice)
    assert client.calls[-1].attempts == 1


def test_not_found_is_not_retried():
    client = api_client.sap_client()
    with pytest.raises(api_client.NotFound):
        client.request("GET", "/purchase-orders/PO-9999", PurchaseOrder)
    assert client.calls[-1].attempts == 1


def test_business_rule_rejection_surfaces_as_rejected():
    with pytest.raises(api_client.Rejected):       # ROHANM is not locked
        api_client.sap_client().request("POST", "/sap-users/ROHANM/unlock", SapUser)


def test_retry_after_timeout_does_not_apply_change_twice():
    # The role is assigned but the reply is late; the client times out and
    # retries with the same Idempotency-Key, and SAP must log only one change.
    launcher.set_faults(launcher.SAP_URL, ["slow"])
    client = api_client.sap_client()
    user = client.request("POST", "/sap-users/MEERAI/roles", SapUser, idempotency_key="test-key-1",
                          json={"role": "Z_REPORTING", "approved_by": "test.owner"})
    assert client.calls[-1].attempts == 2 and "Z_REPORTING" in user.roles
    changes = httpx.get(f"{launcher.SAP_URL}/sap-users/MEERAI/changes").json()
    assert len(changes) == 1
