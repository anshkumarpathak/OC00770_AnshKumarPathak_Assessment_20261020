"""Human approval queue for the actions the new agents can only *propose*:
sending an SLA escalation, submitting a change impact assessment to CAB,
and reprocessing failed integration messages.

The agent creates a pending action; a person approves it on the dashboard;
only then does the registered handler call the mock API. The action id is the
Idempotency-Key, so approving twice cannot do the work twice.

Review before saving (support_agent/review.py): each action carries a draft text
that the approver can revise, approve exactly, then apply. The applied text goes
to the mock system with the action, so what lands is what was approved.
"""
import json
import threading
from datetime import datetime, timezone
from typing import Callable

from support_agent import approvals, config, review
from support_agent.review import ReviewError  # noqa: F401  (re-exported for callers)

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


def draft_text(summary: str, payload: dict) -> str:
    """The first draft the approver reviews: what will be sent, and why it was asked for."""
    reason = payload.get("reason")
    return summary + (f"\nReason given: {reason}" if reason else "")


def create(agent: str, action: str, summary: str, payload: dict, actor: str, trace_id: str | None) -> dict:
    with _lock:
        items = _load()
        item = {"action_id": f"ACT-{len(items) + 1:04d}", "agent": agent, "action": action, "summary": summary,
                "payload": payload, "approver_role": APPROVER.get(action, "Approver"), "requested_by": actor,
                "status": "pending", "requested_at": _now(), "trace_id": trace_id,
                "text": draft_text(summary, payload)}
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
            item["result"] = HANDLERS[item["action"]](_with_note(item), action_id, approver)
            item["status"] = "approved"
        else:
            item["status"] = "rejected"
        item.update(decided_by=approver, decided_at=_now())
        _save(items)
    approvals.audit("action_decided", action_id=action_id, agent=item["agent"], tool=item["action"],
                    status=item["status"], user_id=approver)
    return item


def _with_note(item: dict) -> dict:
    return {**item["payload"], "note": item.get("text", item["summary"])}


def _update(action_id: str, change) -> dict:
    """Load, change one item, save, even when the change raises (an approval can lapse on the way)."""
    with _lock:
        items = _load()
        item = next(i for i in items if i["action_id"] == action_id)
        item.setdefault("text", draft_text(item["summary"], item["payload"]))   # queued before drafts existed
        try:
            return change(item)
        finally:
            _save(items)


def revise(action_id: str, text: str, by: str) -> dict:
    def change(item):
        before = item["text"]
        if review.revise(item, text, by):
            approvals.audit("draft_revised", action_id=action_id, agent=item["agent"], tool=item["action"],
                            user_id=by, before=before, after=item["text"])
        return item
    return _update(action_id, change)


def approve_text(action_id: str, text: str, approver: str) -> dict:
    def change(item):
        a = review.approve_text(item, action_id, text, approver)
        approvals.audit("text_approved", action_id=action_id, agent=item["agent"], tool=item["action"],
                        user_id=approver, text_hash=a["hash"][:12], expires_at=a["expires_at"])
        return item
    return _update(action_id, change)


def apply(action_id: str, approver: str) -> dict:
    """Carry out an approved action: only its approved, unexpired text, and only once."""
    def change(item):
        review.check_applicable(item, action_id)
        item["result"] = HANDLERS[item["action"]](_with_note(item), action_id, approver)
        item.update(status="approved", decided_by=approver, decided_at=_now())
        approvals.audit("action_decided", action_id=action_id, agent=item["agent"], tool=item["action"],
                        status="approved", user_id=approver, text_hash=item["approval"]["hash"][:12])
        return item
    return _update(action_id, change)


def withdraw(action_id: str, by: str) -> dict:
    """Take back an approval that hasn't been applied, so the text can be edited again."""
    def change(item):
        if item.pop("approval", None):
            approvals.audit("approval_withdrawn", action_id=action_id, agent=item["agent"], user_id=by)
        return item
    return _update(action_id, change)
