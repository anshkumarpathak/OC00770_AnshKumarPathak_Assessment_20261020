"""Mock change management API: change requests and their impact assessments (port 8104)."""
import copy
import json
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mock_apis import faults

DATA = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Mock Change Management API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["changes"] = {c["cr_id"]: c for c in json.loads((DATA / "changes.json").read_text())}
        _db["assessments"] = []
        _db["idempotency"] = {}


_load()
faults.install(app, injector, _load, db=_db, tables=("changes",))


class NewAssessment(BaseModel):
    summary: str = Field(min_length=10)
    approvals_needed: list[str]
    approved_by: str = Field(min_length=3)
    note: str = ""          # the exact text the approver approved


@app.get("/changes")
def list_changes(request: Request, status: str | None = None):
    return [faults.maybe_corrupt(request, copy.deepcopy(c)) for c in _db["changes"].values()
            if status is None or c["status"] == status]


@app.get("/changes/{cr_id}")
def get_change(cr_id: str, request: Request):
    c = _db["changes"].get(cr_id)
    if not c:
        raise HTTPException(404, f"change {cr_id} not found")
    return faults.maybe_corrupt(request, copy.deepcopy(c))


@app.get("/changes/{cr_id}/assessments")
def list_assessments(cr_id: str):
    return [a for a in _db["assessments"] if a["cr_id"] == cr_id]


@app.post("/changes/{cr_id}/assessments", status_code=201)
def submit_assessment(cr_id: str, body: NewAssessment, idempotency_key: str | None = Header(default=None)):
    with _lock:
        if idempotency_key and idempotency_key in _db["idempotency"]:
            return _db["idempotency"][idempotency_key]
        c = _db["changes"].get(cr_id)
        if not c:
            raise HTTPException(404, f"change {cr_id} not found")
        if c["status"] != "awaiting_assessment":
            raise HTTPException(409, f"change {cr_id} is {c['status']}")
        a = {"assessment_id": f"IA-{len(_db['assessments']) + 1:03d}", "cr_id": cr_id, **body.model_dump(),
             "submitted_to": "CAB", "submitted_at": date.today().isoformat()}
        _db["assessments"].append(a)
        c["status"] = "assessed"
        if idempotency_key:
            _db["idempotency"][idempotency_key] = a
    return a
