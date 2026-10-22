"""Mock client contracts API: each AMS client's plan and SLA rules (port 8103)."""
import copy
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from mock_apis import faults

DATA = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Mock Contracts API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["contracts"] = {c["client_id"]: c for c in json.loads((DATA / "contracts.json").read_text())}


_load()
faults.install(app, injector, _load)


@app.get("/contracts")
def list_contracts(request: Request):
    return [faults.maybe_corrupt(request, copy.deepcopy(c)) for c in _db["contracts"].values()]


@app.get("/contracts/{client_id}")
def get_contract(client_id: str, request: Request):
    c = _db["contracts"].get(client_id)
    if not c:
        raise HTTPException(404, f"contract for {client_id} not found")
    return faults.maybe_corrupt(request, copy.deepcopy(c))
