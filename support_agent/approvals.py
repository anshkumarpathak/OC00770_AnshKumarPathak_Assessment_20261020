"""Human-in-the-loop approval queue for SAP role requests.

The agent can only *request* a role. The client's authorisation owner approves
or rejects it in the dashboard, and only then is the SAP API called. The
approval id doubles as the Idempotency-Key, so approving twice cannot write the
change twice. Every decision is appended to runtime/audit.jsonl.

Review before saving (support_agent/review.py): the request carries a justification
text the authorisation owner can revise, approve exactly, then apply.
"""
import json
import threading
from datetime import datetime, timezone

from support_agent import api_client, config, review
from support_agent.schemas import SapUser

_lock = threading.Lock()


def _path():
    return config.RUNTIME_DIR / "approvals.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict]:
    return json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else []


def _save(items: list[dict]) -> None:
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(items, indent=2), encoding="utf-8")


def audit(event: str, **details) -> None:
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNTIME_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), "event": event, **details}) + "\n")


def draft_text(role: str, sap_user: str, reason: str) -> str:
    return f"Assign role {role} to SAP user {sap_user}.\nJustification from the key user: {reason}"


def create(user_id: str, client_id: str, sap_user: str, role: str, reason: str, trace_id: str | None) -> dict:
    with _lock:
        items = _load()
        item = {
            "approval_id": f"APR-{len(items) + 1:04d}",
            "user_id": user_id,
            "client_id": client_id,
            "sap_user": sap_user,
            "role": role,
            "reason": reason,
            "text": draft_text(role, sap_user, reason),
            "status": "pending",
            "requested_at": _now(),
            "trace_id": trace_id,
        }
        items.append(item)
        _save(items)
    audit("approval_requested", **item)
    return item


def list_all(status: str | None = None) -> list[dict]:
    return [i for i in _load() if status is None or i["status"] == status]


def decide(approval_id: str, approver: str, approve: bool, note: str = "") -> dict:
    with _lock:
        items = _load()
        item = next((i for i in items if i["approval_id"] == approval_id), None)
        if item is None:
            raise KeyError(approval_id)
        if item["status"] != "pending":
            return item
        if approve:
            _assign(item, approval_id, approver)
            item["status"] = "approved"
        else:
            item["status"] = "rejected"
        item.update(decided_by=approver, decided_at=_now(), note=note)
        _save(items)
    audit("approval_decided", **item)
    return item


def _assign(item: dict, approval_id: str, approver: str) -> None:
    api_client.sap_client().request(
        "POST", f"/sap-users/{item['sap_user']}/roles", SapUser, idempotency_key=approval_id,
        json={"role": item["role"], "approved_by": approver,
              "note": item.get("text") or draft_text(item["role"], item["sap_user"], item["reason"])})


def _update(approval_id: str, change) -> dict:
    """Load, change one request, save, even when the change raises (an approval can lapse on the way)."""
    with _lock:
        items = _load()
        item = next((i for i in items if i["approval_id"] == approval_id), None)
        if item is None:
            raise KeyError(approval_id)
        item.setdefault("text", draft_text(item["role"], item["sap_user"], item["reason"]))
        try:
            return change(item)
        finally:
            _save(items)


def revise(approval_id: str, text: str, by: str) -> dict:
    def change(item):
        before = item["text"]
        if review.revise(item, text, by):
            audit("draft_revised", approval_id=approval_id, user_id=by, role=item["role"], before=before,
                  after=item["text"])
        return item
    return _update(approval_id, change)


def approve_text(approval_id: str, text: str, approver: str) -> dict:
    def change(item):
        a = review.approve_text(item, approval_id, text, approver)
        audit("text_approved", approval_id=approval_id, user_id=approver, role=item["role"],
              text_hash=a["hash"][:12], expires_at=a["expires_at"])
        return item
    return _update(approval_id, change)


def apply(approval_id: str, approver: str) -> dict:
    """Assign the role: only with its approved, unexpired text, and only once."""
    def change(item):
        review.check_applicable(item, approval_id)
        _assign(item, approval_id, approver)
        item.update(status="approved", decided_by=approver, decided_at=_now())
        audit("approval_decided", **{k: v for k, v in item.items() if k not in ("revisions", "approval")},
              text_hash=item["approval"]["hash"][:12])
        return item
    return _update(approval_id, change)


def withdraw(approval_id: str, by: str) -> dict:
    """Take back an approval that hasn't been applied, so the text can be edited again."""
    def change(item):
        if item.pop("approval", None):
            audit("approval_withdrawn", approval_id=approval_id, user_id=by)
        return item
    return _update(approval_id, change)
