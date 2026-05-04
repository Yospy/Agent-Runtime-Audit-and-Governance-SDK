"""Integrity hashing and canonicalization utilities (Setorra).

We compute a SHA-256 hash over a canonicalized JSON representation of an event,
excluding the event's own `integrity.event_hash` when present.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


def canonical_json(obj: Any) -> str:
    """Return a canonical JSON string: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_event_hash(event: Dict[str, Any]) -> str:
    """Compute SHA-256 hex digest for an event, ignoring its own event_hash if present."""
    event_copy = _strip_event_hash(event)
    payload = canonical_json(event_copy)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_event_hash(event: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return event
    copy = dict(event)
    integ = copy.get("integrity")
    if isinstance(integ, dict) and "event_hash" in integ:
        integ = dict(integ)
        integ.pop("event_hash", None)
        copy["integrity"] = integ
    return copy
