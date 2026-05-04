"""Minimal multi-agent helpers for the Python SDK.

This module adds deterministic ID helpers and a tiny MessageBus wrapper that
pairs with the collector's multi-agent APIs. It is intentionally minimal and
keeps enforcement and privacy unchanged.

Feature gate: SETORRA_MULTIAGENT_CAPTURE ∈ {1,true,yes,on}
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_message_id(
    *,
    conversation_id: str,
    from_agent: str,
    seq: int,
    content_hash: Optional[str] = None,
) -> str:
    """Return a deterministic message_id for a logical message.

    Inputs combine the conversation, sender, a local sequence number, and an
    optional content hash for stability across retries.
    """
    seed = f"{conversation_id}|{from_agent}|{seq}|{content_hash or ''}"
    return _sha256_hex(seed)


def derive_correlation_id(
    *,
    conversation_id: str,
    message_id: str,
    from_agent: str,
    to_agent: str,
) -> str:
    """Return a deterministic correlation_id for a single hop of a message."""
    seed = f"{conversation_id}|{message_id}|{from_agent}|{to_agent}"
    return _sha256_hex(seed)


class MessageBus:
    """Light wrapper to record multi-agent sends/receives using a collector.

    Usage:
        bus = MessageBus(collector)
        msg = bus.send(["calendar_agent"], {"summary": "Intro"})
        # ... later in the receiving agent process
        bus.record_inbound(from_agent="scheduler", content={...}, message_id=msg["message_id"]) 
    """

    def __init__(self, collector: Any) -> None:
        self.collector = collector

    def send(
        self,
        to_agents: Iterable[str],
        content: Any,
        *,
        channel: str = "delegate",
        metadata: Optional[Dict[str, Any]] = None,
        from_agent: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.collector.send_message(
            to=list(to_agents),
            content=content,
            channel=channel,
            metadata=metadata,
            from_agent=from_agent,
            reply_to=reply_to,
        )

    def record_inbound(
        self,
        *,
        from_agent: str,
        content: Any,
        message_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self.collector.record_inbound_message(
            from_agent=from_agent,
            content=content,
            message_id=message_id,
            reply_to=reply_to,
            metadata=metadata,
        )

