"""Shared schema types and helpers for Setorra.

Schema versions are additive-only. Keep field names stable and avoid breaking changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Additive-only schema bump for planning capture events and optional plan
# context on events. Backwards-compatible.
SCHEMA_VERSION = "1.3"


@dataclass
class EvidenceEvent:
    schema_version: str
    session_id: str
    event_index: int
    timestamp: str
    agent: Dict[str, str]
    event_type: str
    parameters_redacted: Any
    privacy_flags: List[str]
    redacted_fields: List[str]
    context: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None
    policy: Optional[Dict[str, Any]] = None
    approval: Optional[Dict[str, Any]] = None
    guardrail: Optional[Dict[str, Any]] = None
    integrity: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OutputSummary:
    schema_version: str
    session_id: str
    timestamp: str
    content: str
    prompts: Optional[Dict[str, Any]] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    integrity: Dict[str, Any] = field(default_factory=dict)
