"""Review before saving, shared by both approval queues (agents/actions.py and support_agent/approvals.py).

Every proposed action carries a draft text the approver can revise. An approval
covers one exact text: it stores a hash of the item id and that text, and it
lapses after config.APPROVAL_TTL_S. Applying the action re-checks both, so a
text edited after approval, or an approval left too long, is never acted on.
These functions work on one queue item (a dict) and leave saving to the queue.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from support_agent import config


class ReviewError(Exception):
    """The review step can't go ahead; the message says why, for the approver."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def digest(item_id: str, text: str) -> str:
    return hashlib.sha256(f"{item_id}\n{text}".encode()).hexdigest()


def revise(item: dict, text: str, by: str) -> bool:
    """Replace the draft. Returns False when nothing changed. Any approval of the old text lapses."""
    if item["status"] != "pending":
        raise ReviewError(f"{item.get('action_id') or item.get('approval_id')} is already {item['status']}.")
    text = text.strip()
    if not text:
        raise ReviewError("The text can't be empty.")
    if text == item["text"]:
        return False
    item.setdefault("revisions", []).append({"at": _now().isoformat(timespec="seconds"), "by": by,
                                             "before": item["text"], "after": text})
    item["text"] = text
    item.pop("approval", None)
    return True


def approve_text(item: dict, item_id: str, text: str, approver: str) -> dict:
    """Approve exactly the saved text. Unsaved edits on screen must be saved as a revision first."""
    if item["status"] != "pending":
        raise ReviewError(f"{item_id} is already {item['status']}.")
    if text.strip() != item["text"]:
        raise ReviewError("The text on screen differs from the saved draft. Save the revision first, so the "
                          "approval covers the text you can see.")
    now = _now()
    item["approval"] = {"by": approver, "at": now.isoformat(timespec="seconds"),
                        "expires_at": (now + timedelta(seconds=config.APPROVAL_TTL_S)).isoformat(timespec="seconds"),
                        "hash": digest(item_id, item["text"])}
    return item["approval"]


def seconds_left(item: dict) -> int:
    a = item.get("approval")
    if not a:
        return 0
    return max(0, int((datetime.fromisoformat(a["expires_at"]) - _now()).total_seconds()))


def check_applicable(item: dict, item_id: str) -> None:
    """Raise unless the item holds a live approval of its current text."""
    a = item.get("approval")
    if item["status"] != "pending":
        raise ReviewError(f"{item_id} is already {item['status']}.")
    if not a:
        raise ReviewError("Approve the exact text first.")
    if a["hash"] != digest(item_id, item["text"]):
        item.pop("approval", None)
        raise ReviewError("The text changed after it was approved. Approve the new text.")
    if seconds_left(item) <= 0:
        item.pop("approval", None)
        raise ReviewError(f"The approval lapsed after {config.APPROVAL_TTL_S // 60} minutes without being applied. "
                          "Approve the text again.")
