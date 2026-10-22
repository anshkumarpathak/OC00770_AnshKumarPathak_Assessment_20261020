"""Mock IT service desk (ITSM) API: client key users and support tickets (port 8101).

Stands in for the ticketing tool an SAP support team works from.
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

app = FastAPI(title="Mock Service Desk API")
injector = faults.FaultInjector()
_lock = threading.Lock()
_db: dict = {}


def _load() -> None:
    with _lock:
        _db["users"] = {u["user_id"]: u for u in json.loads((DATA / "users.json").read_text())}
        _db["tickets"] = copy.deepcopy(json.loads((DATA / "tickets.json").read_text()))
        _db["queue"] = {t["ticket_id"]: t for t in json.loads((DATA / "ams_queue.json").read_text())}
        _db["escalations"] = []
        _db["idempotency"] = {}


_load()
faults.install(app, injector, _load, db=_db, tables=("queue",))


class NewTicket(BaseModel):
    user_id: str
    subject: str = Field(min_length=3, max_length=200)
    description: str = ""
    priority: str = Field(default="P3", pattern="^P[1-4]$")


@app.get("/users/{user_id}")
def get_user(user_id: str, request: Request):
    user = _db["users"].get(user_id)
    if not user:
        raise HTTPException(404, f"user {user_id} not found")
    return faults.maybe_corrupt(request, user)


@app.get("/users/{user_id}/tickets")
def list_tickets(user_id: str, request: Request):
    if user_id not in _db["users"]:
        raise HTTPException(404, f"user {user_id} not found")
    return [faults.maybe_corrupt(request, t) for t in _db["tickets"] if t["user_id"] == user_id]


@app.post("/tickets", status_code=201)
def create_ticket(body: NewTicket, request: Request):
    if body.user_id not in _db["users"]:
        raise HTTPException(404, f"user {body.user_id} not found")
    with _lock:
        ticket = {
            "ticket_id": f"INC-{1001 + len(_db['tickets'])}",
            "user_id": body.user_id,
            "subject": body.subject,
            "status": "open",
            "priority": body.priority,
            "created_at": date.today().isoformat(),
        }
        _db["tickets"].append(ticket)
    return faults.maybe_corrupt(request, ticket)


# ---- AMS ticket queue, used by the SLA escalation agent ------------------------------

class NewEscalation(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=3)
    approved_by: str = Field(min_length=3)
    note: str = ""          # the exact text the approver approved


@app.get("/queue/tickets")
def queue(request: Request, status: str | None = None):
    items = [t for t in _db["queue"].values() if status is None or t["status"] == status]
    return [faults.maybe_corrupt(request, t) for t in items]


@app.get("/queue/tickets/{ticket_id}")
def queue_ticket(ticket_id: str, request: Request):
    t = _db["queue"].get(ticket_id)
    if not t:
        raise HTTPException(404, f"ticket {ticket_id} not found")
    return faults.maybe_corrupt(request, t)


@app.get("/queue/escalations")
def list_escalations():
    return _db["escalations"]


@app.post("/queue/escalations", status_code=201)
def create_escalation(body: NewEscalation, idempotency_key: str | None = Header(default=None)):
    with _lock:
        if idempotency_key and idempotency_key in _db["idempotency"]:
            return _db["idempotency"][idempotency_key]
        missing = [t for t in body.ticket_ids if t not in _db["queue"]]
        if missing:
            raise HTTPException(404, f"tickets not found: {missing}")
        esc = {"escalation_id": f"ESC-{len(_db['escalations']) + 1:03d}", **body.model_dump(),
               "created_at": date.today().isoformat()}
        _db["escalations"].append(esc)
        if idempotency_key:
            _db["idempotency"][idempotency_key] = esc
    return esc
