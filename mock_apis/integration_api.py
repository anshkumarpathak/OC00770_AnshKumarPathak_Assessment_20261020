"""Mock integration monitor API: interface messages and reprocessing (port 8106).

Stands in for an SAP Integration Suite / PI-PO message monitor.
"""
import copy
import json
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mock_apis import faults

DATA = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Mock Integration Monitor API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["messages"] = {m["msg_id"]: m for m in json.loads((DATA / "integration_messages.json").read_text())}
        _db["reprocess_log"] = []
        _db["idempotency"] = {}


_load()
faults.install(app, injector, _load)


class ReprocessRequest(BaseModel):
    msg_ids: list[str] = Field(min_length=1, max_length=50)
    approved_by: str = Field(min_length=3)


@app.get("/messages")
def list_messages(request: Request, status: str | None = None):
    return [faults.maybe_corrupt(request, copy.deepcopy(m)) for m in _db["messages"].values()
            if status is None or m["status"] == status]


@app.get("/messages/{msg_id}")
def get_message(msg_id: str, request: Request):
    m = _db["messages"].get(msg_id)
    if not m:
        raise HTTPException(404, f"message {msg_id} not found")
    return faults.maybe_corrupt(request, copy.deepcopy(m))


@app.get("/reprocess-log")
def reprocess_log():
    return _db["reprocess_log"]


@app.post("/messages/reprocess")
def reprocess(body: ReprocessRequest, idempotency_key: str | None = Header(default=None)):
    with _lock:
        if idempotency_key and idempotency_key in _db["idempotency"]:
            return _db["idempotency"][idempotency_key]
        missing = [m for m in body.msg_ids if m not in _db["messages"]]
        if missing:
            raise HTTPException(404, f"messages not found: {missing}")
        for m in body.msg_ids:
            _db["messages"][m]["status"] = "reprocessed"
        entry = {"run_id": f"RP-{len(_db['reprocess_log']) + 1:03d}", **body.model_dump(),
                 "run_at": date.today().isoformat()}
        _db["reprocess_log"].append(entry)
        if idempotency_key:
            _db["idempotency"][idempotency_key] = entry
    return entry
