"""Human-in-the-loop approval queue for SAP role requests.

The agent can only *request* a role. The client's authorisation owner approves
or rejects it in the dashboard, and only then is the SAP API called. The
approval id doubles as the Idempotency-Key, so approving twice cannot write the
change twice. Every decision is appended to runtime/audit.jsonl.
"""
import json
import threading
from datetime import datetime, timezone

from support_agent import api_client, config
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
            api_client.sap_client().request(
                "POST", f"/sap-users/{item['sap_user']}/roles", SapUser, idempotency_key=approval_id,
                json={"role": item["role"], "approved_by": approver})
            item["status"] = "approved"
        else:
            item["status"] = "rejected"
        item.update(decided_by=approver, decided_at=_now(), note=note)
        _save(items)
    audit("approval_decided", **item)
    return item
