"""Local approval records for action-firewall flows."""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


class ApprovalRuntimeError(RuntimeError):
    """Raised when an approval transition is invalid."""


@dataclass
class ApprovalRecord:
    action_id: str
    intent_hash: str
    payload_hash: str
    context_hash: str
    status: str
    expires_at: Optional[str] = None
    approver_id: Optional[str] = None
    approver_email: Optional[str] = None
    resolved_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._records: Dict[str, ApprovalRecord] = {}

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        self._records[_key(record.action_id, record.intent_hash)] = record
        return record

    def get(self, *, action_id: str, intent_hash: str) -> Optional[ApprovalRecord]:
        return self._records.get(_key(action_id, intent_hash))

    def resolve(
        self,
        *,
        action_id: str,
        intent_hash: str,
        status: str,
        approver_id: Optional[str] = None,
        approver_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRecord:
        record = self.get(action_id=action_id, intent_hash=intent_hash)
        if record is None:
            raise ApprovalRuntimeError("No pending approval record for action")
        if record.status not in {"pending"}:
            raise ApprovalRuntimeError(f"Approval is already {record.status}")
        record.status = status
        record.approver_id = approver_id
        record.approver_email = approver_email
        record.resolved_at = _iso_now()
        record.metadata = dict(metadata or {})
        return record

    def assert_approved(
        self,
        *,
        action_id: str,
        intent_hash: str,
        payload_hash: str,
        context_hash: str,
    ) -> ApprovalRecord:
        record = self.get(action_id=action_id, intent_hash=intent_hash)
        if record is None:
            raise ApprovalRuntimeError("No approval record for action")
        if record.status != "approved":
            raise ApprovalRuntimeError(f"Action approval is {record.status}, not approved")
        if record.payload_hash != payload_hash or record.context_hash != context_hash:
            raise ApprovalRuntimeError("Approved action hash does not match current request")
        return record


class FileApprovalStore(InMemoryApprovalStore):
    """Tiny JSON-file approval store for local MVP tests and demos."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        super().__init__()
        self._load()

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        result = super().create(record)
        self._persist()
        return result

    def resolve(self, **kwargs: Any) -> ApprovalRecord:
        record = super().resolve(**kwargs)
        self._persist()
        return record

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        for item in data:
            if isinstance(item, dict):
                record = ApprovalRecord(**item)
                self._records[_key(record.action_id, record.intent_hash)] = record

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(record) for record in self._records.values()], indent=2),
            encoding="utf-8",
        )


def _key(action_id: str, intent_hash: str) -> str:
    return f"{action_id}:{intent_hash}"


def _iso_now() -> str:
    return _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
