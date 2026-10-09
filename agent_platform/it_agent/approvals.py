"""
approvals.py - a tiny approval queue stored in var/approvals.json.

Item lifecycle:

    pending --approve--> approved --run ok--> executed
       |                      \\--run fails--> failed
       \\--reject--> rejected

Governance rules enforced here (not in the UI, not in the prompt):
  - you cannot approve or reject your own request (separation of duties)
  - only "pending" items can be approved or rejected
  - rejecting needs a reason (auditors always ask "why?")

NOTE: writes go to a temp file then os.replace(), so a crash mid-write never
leaves a half-written JSON file behind.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..errors import NotFoundError, PolicyError, StorageError, ValidationError

PENDING, APPROVED, REJECTED, EXECUTED, FAILED = "pending", "approved", "rejected", "executed", "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storage
    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "items": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"{self.path} is corrupted ({exc}). Fix it by hand or run: "
                f"python -m agent_platform reset --yes"
            )
        if "items" not in data or "next_id" not in data:
            raise StorageError(f"{self.path} has an unexpected shape")
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise StorageError(f"could not save approvals to {self.path}: {exc}")

    # ------------------------------------------------------------ actions
    def add(self, ticket_id: str, intent: str, requester: str, summary: str) -> dict:
        with self._lock:
            data = self._load()
            approval_id = f"APR-{data['next_id']:04d}"
            item = {
                "id": approval_id,
                "ticket_id": ticket_id,
                "intent": intent,
                "requester": requester,
                "summary": summary,
                "status": PENDING,
                "created_at": _now(),
                "decided_by": None,
                "decided_at": None,
                "note": None,
            }
            data["items"][approval_id] = item
            data["next_id"] += 1
            self._save(data)
            return item

    def get(self, approval_id: str) -> dict:
        item = self._load()["items"].get(approval_id)
        if item is None:
            raise NotFoundError(f"approval {approval_id!r} not found")
        return item

    def list(self, status: str = None) -> list:
        items = list(self._load()["items"].values())
        if status:
            items = [i for i in items if i["status"] == status]
        return sorted(items, key=lambda i: i["id"])

    def _decide(self, approval_id: str, approver: str, new_status: str, note: str = None) -> dict:
        if not approver or not approver.strip():
            raise ValidationError("approver name is required")
        with self._lock:
            data = self._load()
            item = data["items"].get(approval_id)
            if item is None:
                raise NotFoundError(f"approval {approval_id!r} not found")
            if item["status"] != PENDING:
                raise PolicyError(f"{approval_id} is already {item['status']}; only pending items can change")
            if approver.strip().lower() == item["requester"].strip().lower():
                raise PolicyError("separation of duties: you cannot decide on your own request")
            item.update(status=new_status, decided_by=approver.strip(), decided_at=_now(), note=note)
            self._save(data)
            return item

    def approve(self, approval_id: str, approver: str) -> dict:
        return self._decide(approval_id, approver, APPROVED)

    def reject(self, approval_id: str, approver: str, reason: str) -> dict:
        if not reason or not reason.strip():
            raise ValidationError("a rejection reason is required")
        return self._decide(approval_id, approver, REJECTED, reason.strip())

    def mark_result(self, approval_id: str, success: bool, note: str) -> dict:
        """Called by the agent after it tries to run an approved action."""
        with self._lock:
            data = self._load()
            item = data["items"].get(approval_id)
            if item is None:
                raise NotFoundError(f"approval {approval_id!r} not found")
            item["status"] = EXECUTED if success else FAILED
            item["note"] = note
            self._save(data)
            return item
