"""Mock CMDB API: the dependency catalogue for each client's SAP objects (port 8105)."""
import copy
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from mock_apis import faults

DATA = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Mock CMDB API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["objects"] = {o["object"]: o for o in json.loads((DATA / "cmdb.json").read_text())}


_load()
faults.install(app, injector, _load)


@app.get("/objects/{name}")
def get_object(name: str, request: Request):
    o = _db["objects"].get(name)
    if not o:
        raise HTTPException(404, f"object {name} is not in the CMDB")
    return faults.maybe_corrupt(request, copy.deepcopy(o))
