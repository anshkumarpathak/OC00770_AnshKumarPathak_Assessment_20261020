"""Mock SAP S/4HANA API: purchase orders, invoices and SAP users (port 8102).

A simplified stand-in for the OData services of a client's SAP system.
"""
import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mock_apis import faults

DATA = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Mock SAP S/4HANA API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["pos"] = {p["po_id"]: p for p in json.loads((DATA / "purchase_orders.json").read_text())}
        _db["invoices"] = {i["invoice_id"]: i for i in json.loads((DATA / "invoices.json").read_text())}
        _db["users"] = {u["sap_user"]: u for u in json.loads((DATA / "sap_users.json").read_text())}
        _db["vendors"] = {v["vendor_no"]: v for v in json.loads((DATA / "sap_vendors.json").read_text())}
        _db["health"] = json.loads((DATA / "sap_health.json").read_text())
        _db["change_log"] = []      # like SAP change documents: one entry per change made
        _db["idempotency"] = {}


_load()
faults.install(app, injector, _load)


class RoleAssignment(BaseModel):
    role: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,30}$")
    approved_by: str = Field(min_length=3)


def _get(table: str, key: str, label: str):
    item = _db[table].get(key)
    if not item:
        raise HTTPException(404, f"{label} {key} not found")
    return copy.deepcopy(item)


def _log(sap_user: str, change: str) -> dict:
    entry = {"change_id": f"CD{len(_db['change_log']) + 1:05d}", "sap_user": sap_user, "change": change,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _db["change_log"].append(entry)
    return entry


@app.get("/purchase-orders/{po_id}")
def get_po(po_id: str, request: Request):
    return faults.maybe_corrupt(request, _get("pos", po_id, "purchase order"))


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, request: Request):
    return faults.maybe_corrupt(request, _get("invoices", invoice_id, "invoice"))


@app.get("/sap-users/{sap_user}")
def get_sap_user(sap_user: str, request: Request):
    return faults.maybe_corrupt(request, _get("users", sap_user, "SAP user"))


@app.get("/sap-users/{sap_user}/changes")
def list_changes(sap_user: str):
    return [c for c in _db["change_log"] if c["sap_user"] == sap_user]


@app.post("/sap-users/{sap_user}/unlock")
def unlock(sap_user: str, request: Request, idempotency_key: str | None = Header(default=None)):
    with _lock:
        if idempotency_key and idempotency_key in _db["idempotency"]:
            return _db["idempotency"][idempotency_key]
        user = _db["users"].get(sap_user)
        if not user:
            raise HTTPException(404, f"SAP user {sap_user} not found")
        if user["lock_status"] == "unlocked":
            raise HTTPException(409, "user is not locked")
        user["lock_status"] = "unlocked"
        _log(sap_user, "unlocked")
        result = copy.deepcopy(user)
        if idempotency_key:
            _db["idempotency"][idempotency_key] = result
    return faults.maybe_corrupt(request, result)


@app.post("/sap-users/{sap_user}/roles")
def assign_role(sap_user: str, body: RoleAssignment, request: Request,
                idempotency_key: str | None = Header(default=None)):
    with _lock:
        # Idempotency: a retried call with the same key returns the first result
        # instead of writing a second change document.
        if idempotency_key and idempotency_key in _db["idempotency"]:
            return _db["idempotency"][idempotency_key]
        user = _db["users"].get(sap_user)
        if not user:
            raise HTTPException(404, f"SAP user {sap_user} not found")
        if body.role not in user["roles"]:
            user["roles"].append(body.role)
        _log(sap_user, f"role {body.role} assigned, approved by {body.approved_by}")
        result = copy.deepcopy(user)
        if idempotency_key:
            _db["idempotency"][idempotency_key] = result
    return result


@app.delete("/sap-users/{sap_user}")
def delete_user(sap_user: str):
    # A dangerous endpoint that exists on the real system. The agent never gets
    # a tool for it: the allowlist in support_agent/tools.py is what protects it.
    with _lock:
        if _db["users"].pop(sap_user, None) is None:
            raise HTTPException(404, f"SAP user {sap_user} not found")
    return {"deleted": sap_user}


# ---- used by the integration failure agent -------------------------------------------

@app.get("/vendors/{vendor_no}")
def get_vendor(vendor_no: str, request: Request):
    return faults.maybe_corrupt(request, _get("vendors", vendor_no, "vendor"))


@app.get("/system/health")
def system_health(request: Request):
    return faults.maybe_corrupt(request, copy.deepcopy(_db["health"]))
