"""Fault injection shared by both mock APIs.

Real enterprise systems fail: they return 5xx errors, time out, or send data
that does not match the agreed contract. Each mock API owns a FaultInjector so
tests and the demo UI can switch these failures on and watch the client cope.

Faults can be scripted (a queue consumed one request at a time, used by tests)
or random (a probability per request, used for the "chaos" slider in the demo).
"""
import asyncio
import random
import threading
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

FAULT_TYPES = ("500", "503", "slow", "malformed")
SLOW_SECONDS = 2.5  # longer than the client's timeout, so "slow" looks like a timeout


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
            self._script = deque(f for f in cfg.script if f in FAULT_TYPES or f == "none")
            self.random_rate = max(0.0, min(cfg.random_rate, 1.0))

    def next_fault(self) -> str | None:
        with self._lock:
            if self._script:
                fault = self._script.popleft()
                return None if fault == "none" else fault
        if self.random_rate and random.random() < self.random_rate:
            return random.choice(FAULT_TYPES)
        return None


def install(app: FastAPI, injector: FaultInjector, reset_fn) -> None:
    """Add the fault middleware and the /_admin endpoints to an app."""

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        # The dashboard's "live view" panel reads state with this header so the chaos
        # slider only affects the agents, not the presenter's view of the systems.
        if request.url.path.startswith(("/_admin", "/health", "/docs", "/openapi")) or \
                request.headers.get("x-demo-view") == "1":
            return await call_next(request)
        fault = injector.next_fault()
        request.state.fault = fault
        if fault in ("500", "503"):
            return JSONResponse({"detail": f"injected {fault} error"}, status_code=int(fault))
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
