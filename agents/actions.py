"""Human approval queue for the actions the new agents can only *propose*:
sending an SLA escalation, submitting a change impact assessment to CAB,
and reprocessing failed integration messages.

The agent creates a pending action; a person approves it on the dashboard;
only then does the registered handler call the mock API. The action id is the
Idempotency-Key, so approving twice cannot do the work twice.
"""
import json
import threading
from datetime import datetime, timezone
from typing import Callable

from support_agent import approvals, config

_lock = threading.Lock()
HANDLERS: dict[str, Callable[[dict, str, str], dict]] = {}
APPROVER = {"send_escalation": "AMS delivery manager", "submit_cab_assessment": "Change manager",
            "reprocess_messages": "Integration lead"}


def handler(action: str):
    def register(fn):
        HANDLERS[action] = fn
        return fn
    return register


def _path():
    return config.RUNTIME_DIR / "actions.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict]:
    return json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else []


def _save(items: list[dict]) -> None:
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(items, indent=2), encoding="utf-8")


def create(agent: str, action: str, summary: str, payload: dict, actor: str, trace_id: str | None) -> dict:
    with _lock:
        items = _load()
        item = {"action_id": f"ACT-{len(items) + 1:04d}", "agent": agent, "action": action, "summary": summary,
                "payload": payload, "approver_role": APPROVER.get(action, "Approver"), "requested_by": actor,
                "status": "pending", "requested_at": _now(), "trace_id": trace_id}
        items.append(item)
        _save(items)
    approvals.audit("action_requested", action_id=item["action_id"], agent=agent, tool=action, user_id=actor)
    return item


def list_all(status: str | None = None, agent: str | None = None) -> list[dict]:
    return [i for i in _load() if (status is None or i["status"] == status) and (agent is None or i["agent"] == agent)]


def pending_for(agent: str, key: str, value: str) -> list[dict]:
    """Pending actions whose payload lists `value` under `key` (to avoid duplicates)."""
    return [i for i in list_all("pending", agent) if value in i["payload"].get(key, [])]


def decide(action_id: str, approver: str, approve: bool) -> dict:
    with _lock:
        items = _load()
        item = next(i for i in items if i["action_id"] == action_id)
        if item["status"] != "pending":
            return item
        if approve:
            item["result"] = HANDLERS[item["action"]](item["payload"], action_id, approver)
            item["status"] = "approved"
        else:
            item["status"] = "rejected"
        item.update(decided_by=approver, decided_at=_now())
        _save(items)
    approvals.audit("action_decided", action_id=action_id, agent=item["agent"], tool=item["action"],
                    status=item["status"], user_id=approver)
    return item
