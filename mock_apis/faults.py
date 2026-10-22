"""Fault injection shared by both mock APIs.

Real enterprise systems fail: they return 5xx errors, time out, or send data
that does not match the agreed contract. Each mock API owns a FaultInjector so
tests and the demo UI can switch these failures on and watch the client cope.

Faults can be scripted (a queue consumed one request at a time, used by tests
and the demo's "API condition" picker) or random (a probability per request,
used for the "chaos" slider in the demo).
"""
import asyncio
import random
import threading
from collections import deque

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

FAULT_TYPES = ("500", "503", "slow", "malformed")          # what random chaos picks from
SCRIPTABLE = FAULT_TYPES + ("429", "404")                  # also allowed in a script
STATUS_FAULTS = ("500", "503", "429", "404")
SLOW_SECONDS = 2.5  # longer than the client's timeout, so "slow" looks like a timeout

# Named conditions for one request, as scripts for each API the agent calls.
# Three faults in a row outlast the client's three attempts; one is recovered by a retry.
CONDITIONS = {
    "normal": [],
    "transient": ["503"],
    "unavailable": ["503", "503", "503"],
    "timeout": ["slow", "slow", "slow"],
    "malformed": ["malformed"],
    "rate_limited": ["429", "429", "429"],
    "not_found": ["404"],
}


class FaultConfig(BaseModel):
    script: list[str] = []
    random_rate: float = 0.0


class FaultInjector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._script: deque[str] = deque()
        self.random_rate = 0.0

    def configure(self, cfg: FaultConfig) -> None:
        # "none" in a script lets a request through untouched, so a test can
        # target e.g. the third request of a multi-call workflow.
        with self._lock:
            self._script = deque(f for f in cfg.script if f in SCRIPTABLE or f == "none")
            self.random_rate = max(0.0, min(cfg.random_rate, 1.0))

    def next_fault(self) -> str | None:
        with self._lock:
            if self._script:
                fault = self._script.popleft()
                return None if fault == "none" else fault
        if self.random_rate and random.random() < self.random_rate:
            return random.choice(FAULT_TYPES)
        return None


def install(app: FastAPI, injector: FaultInjector, reset_fn, db: dict | None = None,
            tables: tuple[str, ...] = ()) -> None:
    """Add the fault middleware and the /_admin endpoints to an app.

    `tables` names the collections in `db` that the portal's record editor may read
    and replace one record at a time (the portal checks the new record against the
    agent's schema first; Reset restores the original data).
    """

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        # The dashboard's "live view" panel reads state with this header so the chaos
        # slider only affects the agents, not the presenter's view of the systems.
        if request.url.path.startswith(("/_admin", "/health", "/docs", "/openapi")) or \
                request.headers.get("x-demo-view") == "1":
            return await call_next(request)
        fault = injector.next_fault()
        request.state.fault = fault
        if fault in STATUS_FAULTS:
            detail = "record not found (injected)" if fault == "404" else f"injected {fault} error"
            return JSONResponse({"detail": detail}, status_code=int(fault))
        response = await call_next(request)
        if fault == "slow":
            # The work is done (a POST is committed) but the reply arrives late.
            # This is exactly the case where a naive retry would pay out twice.
            await asyncio.sleep(SLOW_SECONDS)
        return response

    @app.post("/_admin/faults")
    def set_faults(cfg: FaultConfig):
        injector.configure(cfg)
        return {"ok": True, "random_rate": injector.random_rate}

    @app.post("/_admin/reset")
    def reset():
        injector.configure(FaultConfig())
        reset_fn()
        return {"ok": True}

    def _record_table(table: str, key: str) -> dict:
        if table not in tables or key not in db[table]:
            raise HTTPException(404, f"no editable record {table}/{key}")
        return db[table]

    @app.get("/_admin/records/{table}/{key}")
    def read_record(table: str, key: str):
        return _record_table(table, key)[key]

    @app.put("/_admin/records/{table}/{key}")
    def replace_record(table: str, key: str, record: dict = Body(...)):
        _record_table(table, key)[key] = record
        return record

    @app.get("/health")
    def health():
        return {"status": "ok"}


def maybe_corrupt(request: Request, payload):
    """For the 'malformed' fault: break the contract the client relies on."""
    if getattr(request.state, "fault", None) != "malformed":
        return payload
    broken = dict(payload)
    for key in list(broken)[:2]:
        broken.pop(key)          # drop required fields
    broken["total"] = broken["amount"] = "N/A"   # wrong type
    return broken
