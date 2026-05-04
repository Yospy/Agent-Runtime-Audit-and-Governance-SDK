"""Setorra Collector: session lifecycle, redaction, integrity, and storage.

Usage (≤10 lines):

    from setorra import setorra_collector, invoke
    collector = setorra_collector(agent_name="my-agent", agent_version="1.0.0")
    result = invoke(collector, agent_or_executor=my_agent, inputs={"q": "..."})

The collector buffers evidence events in memory (up to ~10MB by default) and
flushes three artifacts per run: evidence, output summary, and manifest.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import numbers
import platform
import socket
import sys
import os
import threading
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import re

try:  # Python 3.8+ standard library; fall back gracefully if unavailable.
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover
    import importlib_metadata  # type: ignore

from .context import get_session_id, set_session_id, get_plan_id, set_plan_id
from .id import new_ulid
from .integrity import canonical_json, compute_event_hash
from .guardrails import (
    GuardrailEnforcer,
    GuardrailEvaluation,
    GuardrailLoader,
    GuardrailViolationError,
    GuardrailError,
)
from .policy import (
    ApprovalNotifier,
    PendingApproval,
    PolicyApprovalCancelled,
    PolicyApprovalReceipt,
    PolicyApprovalRequirement,
    PolicyApprovalTimeout,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyDeniedError,
    PolicyError,
    PolicyLoader,
)
from .redact import redact
from .schema import EvidenceEvent, SCHEMA_VERSION
from .storage import (
    DEFAULT_DIR,
    ensure_storage_dir,
)
from .backend_ingest import deliver_artifacts
from .remote import HandshakeClient, HandshakeError, InvalidApiKey, RevokedKey, TransientNetworkError
from .multiagent import derive_message_id, derive_correlation_id
from .validation import validate_payloads


_DEFAULT_MAX_BUFFER_BYTES = 10 * 1024 * 1024  # ~10MB per run


class SetorraCollector:
    """Collector orchestrates a single process's agent runs.

    Create one collector per process or service. Start a session for each run.
    """

    def __init__(
        self,
        agent_name: str,
        agent_version: str,
        storage_dir: Path | str = DEFAULT_DIR,
        max_buffer_bytes: int = _DEFAULT_MAX_BUFFER_BYTES,
        *,
        environment: str = "local",
        hostname: Optional[str] = None,
        sdk_version: Optional[str] = None,
        signing_key: Optional[bytes | str] = None,
        signing_key_id: Optional[str] = None,
        policy_loader: Optional[PolicyLoader] = None,
        policy_decider: Optional[PolicyDecisionPoint] = None,
        auto_wait_for_approval: bool = True,
        approval_notifier: Optional[ApprovalNotifier] = None,
        guardrail_loader: Optional[GuardrailLoader] = None,
        guardrail_enforcer: Optional[GuardrailEnforcer] = None,
        # Planning capture gating (default resolves from env in __init__).
        capture_plans: Optional[bool] = None,
        # Multi-agent capture gating (default resolves from env in __init__).
        capture_multiagent: Optional[bool] = None,
    ) -> None:
        self.agent_name = agent_name
        self.agent_version = agent_version
        self.storage_dir = Path(storage_dir)
        self.max_buffer_bytes = max_buffer_bytes
        self.environment = environment
        self.hostname = hostname or socket.gethostname()
        self.sdk_version = sdk_version or _detect_sdk_version()
        self.runtime = _detect_runtime()

        if isinstance(signing_key, str):
            signing_key_bytes = signing_key.encode("utf-8")
        else:
            signing_key_bytes = signing_key
        self._signing_key = signing_key_bytes
        self._signing_key_id = signing_key_id

        ensure_storage_dir(self.storage_dir)

        # Session-scoped state
        self._session_id: Optional[str] = None
        self._started_at: Optional[_dt.datetime] = None
        self._events: List[Dict[str, Any]] = []
        self._bytes_estimate: int = 0
        self._first_hash: Optional[str] = None
        self._last_hash: Optional[str] = None
        self._environment_info: Dict[str, Any] = {}
        self._policy_info: Dict[str, Any] = {}
        self._policy_loader = policy_loader
        if policy_decider is not None:
            self._policy_decider = policy_decider
        elif policy_loader is not None:
            self._policy_decider = PolicyDecisionPoint(policy_loader)
        else:
            self._policy_decider = None
        self._auto_wait_for_approval = auto_wait_for_approval
        self._approval_notifier = approval_notifier
        self._pending_lock = threading.Lock()
        self._pending_approvals: Dict[str, PendingApproval] = {}
        self._last_policy_decision: Optional[PolicyDecision] = None
        self._prompt_info: Dict[str, Dict[str, Optional[str]]] = {}
        self._prompt_event_parameters: Dict[str, Any] = {}
        self._prompt_privacy_flags: List[str] = []
        self._prompt_redacted_fields: List[str] = []
        self._prompt_hashes: Dict[str, Optional[str]] = {}
        self._tool_stats: Dict[str, Dict[str, Any]] = {}
        self._tool_latest_input: Dict[str, Optional[str]] = {}
        self._reasoning_records: List[Dict[str, Any]] = []
        self._consents: List[Dict[str, Any]] = []
        self._run_result: Dict[str, Any] = {}
        self._outcome_emitted: bool = False
        self._model_id: Optional[str] = None
        self._guardrail_loader = guardrail_loader
        if guardrail_enforcer is not None:
            self._guardrail_enforcer = guardrail_enforcer
        elif guardrail_loader is not None:
            self._guardrail_enforcer = GuardrailEnforcer(guardrail_loader)
        else:
            self._guardrail_enforcer = None
        self._guardrail_info: Dict[str, Any] = {}
        self._guardrail_summary: Dict[str, Any] = {}
        # Per-run error summary buffer for output.json. Capped to bound memory.
        self._errors: List[Dict[str, Any]] = []
        self._errors_truncated: int = 0
        self._errors_cap: int = 100
        self._action_statuses: Dict[str, Dict[str, Any]] = {}
        self._action_total: int = 0
        # Planning capture state
        self._capture_plans: bool = (
            bool(capture_plans)
            if capture_plans is not None
            else (os.getenv("SETORRA_CAPTURE_PLANS", "").strip().lower() in {"1", "true", "yes", "on"})
        )
        self._current_plan_id: Optional[str] = None
        self._current_plan_version: int = 0
        # Remote/handshake state (optional). Populated when SDK links to backend
        # using only the API key. Secrets are kept in-memory only.
        self._organization_info: Dict[str, Any] = {}
        self._remote_session_token: Optional[str] = None
        self._remote_token_expires_at: Optional[_dt.datetime] = None
        # Multi-agent state (feature-gated)
        self._capture_multiagent: bool = (
            bool(capture_multiagent)
            if capture_multiagent is not None
            else (os.getenv("SETORRA_MULTIAGENT_CAPTURE", "").strip().lower() in {"1", "true", "yes", "on"})
        )
        self._conversation_id: Optional[str] = None
        self._participants: Dict[str, Dict[str, Any]] = {}
        self._ma_edges: List[Dict[str, Any]] = []
        self._ma_msg_counter: int = 0
        self._ma_stats: Dict[str, int] = {
            "messages_total": 0,
            "messages_allowed": 0,
            "messages_denied": 0,
            "messages_partial": 0,
            "handoffs_started": 0,
            "handoffs_completed": 0,
            "handoffs_cancelled": 0,
        }

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def start_session(
        self,
        meta: Optional[Dict[str, Any]] = None,
        *,
        policy_version: str = "0",
        capability_scope: Optional[Any] = None,
        environment: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> str:
        if self._session_id is not None:
            raise RuntimeError("A session is already active. Call end_session() first.")
        session_id = new_ulid()
        self._session_id = session_id
        set_session_id(session_id)
        self._started_at = _utcnow()
        self._events = []
        self._bytes_estimate = 0
        self._first_hash = None
        self._last_hash = None
        self._tool_stats = {}
        self._tool_latest_input = {}
        self._reasoning_records = []
        self._consents = []
        self._run_result = {}
        self._outcome_emitted = False
        self._prompt_event_parameters = {}
        self._prompt_privacy_flags = []
        self._prompt_redacted_fields = []
        # Reset per-run errors buffer
        self._errors = []
        self._errors_truncated = 0
        self._action_statuses = {}
        self._action_total = 0

        self._environment_info = environment or {
            "sdk_version": self.sdk_version,
            "environment": self.environment,
            "hostname": self.hostname,
            "runtime": self.runtime,
        }

        bundle = self._get_policy_bundle()
        if bundle is not None:
            self._policy_info = {
                "version": bundle.version,
                "decision": "not_evaluated",
                "bundle_hash": bundle.bundle_hash,
            }
            if bundle.source_path is not None:
                self._policy_info["source"] = str(bundle.source_path)
        else:
            self._policy_info = {
                "version": policy_version,
                "decision": "not_evaluated",
            }
        if capability_scope is not None:
            self._policy_info["capability_scope"] = capability_scope

        self._model_id = model_id

        guard_bundle = self._get_guardrail_bundle()
        if guard_bundle is not None:
            self._guardrail_info = {
                "version": guard_bundle.version,
                "bundle_hash": guard_bundle.bundle_hash,
            }
            if guard_bundle.source_path is not None:
                self._guardrail_info["source"] = str(guard_bundle.source_path)
            if guard_bundle.metadata:
                self._guardrail_info["metadata"] = guard_bundle.metadata
        else:
            self._guardrail_info = {"status": "unconfigured"}
        self._guardrail_summary = {"decision": "not_evaluated", "applied_rules": []}
        # Reset planning for new session
        self._current_plan_id = None
        self._current_plan_version = 0
        set_plan_id(None)

        prompts, prompt_params, prompt_flags, prompt_fields = _prepare_prompts(system_prompt, user_prompt)
        self._prompts = prompts
        self._prompt_event_parameters = prompt_params
        self._prompt_privacy_flags = prompt_flags
        self._prompt_redacted_fields = prompt_fields
        self._prompt_hashes = {
            "system": self._prompts.get("system", {}).get("hash"),
            "user": self._prompts.get("user", {}).get("hash"),
        }

        # Emit agent.started event
        started_context = dict(meta or {})
        started_context.setdefault("environment", self._environment_info)
        self._emit_event(
            event_type="agent.started",
            parameters={},
            context=started_context,
            execution={"status": "started"},
        )

        self._emit_session_manifest()
        self._emit_prompt_capture()
        return session_id

    # ------------------------------------------------------------------
    # Multi-agent: conversation + participants (feature-gated)
    # ------------------------------------------------------------------
    def start_conversation(
        self,
        *,
        conversation_id: Optional[str] = None,
        participants: Optional[List[Dict[str, Any]]] = None,
        edges: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """Start or attach to a multi-agent conversation.

        Emits a `multiagent.topology` event once per conversation.
        Returns the active conversation_id or None when capture is disabled.
        """
        if not self._capture_multiagent:
            return None
        if self._session_id is None:
            return None
        if self._conversation_id is None:
            self._conversation_id = conversation_id or new_ulid()
        self._ma_msg_counter = 0
        self._participants = {}
        for p in (participants or []):
            if not isinstance(p, dict):
                continue
            aid = str(p.get("agent_id") or p.get("name") or "")
            if not aid:
                continue
            self._participants[aid] = {
                "agent_id": aid,
                "version": p.get("version"),
                "role": p.get("role"),
            }
        self._ma_edges = [e for e in (edges or []) if isinstance(e, dict)]
        self._emit_event(
            event_type="multiagent.topology",
            parameters={
                "participants": list(self._participants.values()),
                "edges": self._ma_edges,
            },
            context={"conversation_id": self._conversation_id},
            execution={"status": "ready"},
        )
        return self._conversation_id

    def add_participant(self, *, agent_id: str, version: Optional[str] = None, role: Optional[str] = None) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None:
            return None
        if self._conversation_id is None:
            self.start_conversation()
        self._participants[agent_id] = {"agent_id": agent_id, "version": version, "role": role}
        return self._emit_event(
            event_type="agent.joined",
            parameters={"agent_id": agent_id, "version": version, "role": role},
            context={"conversation_id": self._conversation_id},
            execution={"status": "joined"},
        )

    def remove_participant(self, *, agent_id: str, reason: Optional[str] = None) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None:
            return None
        if self._conversation_id is None:
            return None
        self._participants.pop(agent_id, None)
        return self._emit_event(
            event_type="agent.left",
            parameters={"agent_id": agent_id, "reason": reason},
            context={"conversation_id": self._conversation_id},
            execution={"status": "left"},
        )

    # ------------------------------------------------------------------
    # Multi-agent: messaging and handoffs (feature-gated)
    # ------------------------------------------------------------------
    def send_message(
        self,
        *,
        to: List[str],
        content: Any,
        channel: str = "delegate",
        metadata: Optional[Dict[str, Any]] = None,
        from_agent: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an outbound message attempt (policy-gated) and per-recipient result.

        This method does not perform actual delivery; it only captures evidence
        of the attempted inter-agent communication.
        """
        result: Dict[str, Any] = {"recipients": []}
        if not self._capture_multiagent or self._session_id is None:
            return result
        if self._conversation_id is None:
            self.start_conversation()
        sender = from_agent or self.agent_name
        # Prepare redacted preview and content hash
        redacted_preview, flags, fields = redact(content)
        try:
            content_canon = canonical_json(content)
        except Exception:
            content_canon = str(content)
        content_sha256 = hashlib.sha256(content_canon.encode("utf-8")).hexdigest()
        # Allocate message/correlation IDs
        self._ma_msg_counter += 1
        message_id = derive_message_id(
            conversation_id=self._conversation_id,
            from_agent=sender,
            seq=self._ma_msg_counter,
            content_hash=content_sha256,
        )
        corr_map = {t: derive_correlation_id(
            conversation_id=self._conversation_id,
            message_id=message_id,
            from_agent=sender,
            to_agent=t,
        ) for t in to}

        # Default assumption: all delivered unless a policy denies
        recipient_exec: List[Dict[str, Any]] = []
        decision_status = "delivered"
        try:
            # Evaluate policy for agent.message; do not block if approvals are needed.
            decision = self.enforce_policy(
                "agent.message",
                actor={"type": "agent", "name": sender},
                parameters={
                    "to": to,
                    "channel": channel,
                    "content_preview": redacted_preview,
                    "hashes": {"content_sha256": content_sha256},
                },
                context={"conversation_id": self._conversation_id},
                wait_for_approval=False,
            )
            if getattr(decision, "decision", None) == "require_approval":
                decision_status = "pending_approval"
                recipient_exec = [{"to": t, "status": "pending_approval"} for t in to]
            else:
                self._ma_stats["messages_allowed"] += 1
                recipient_exec = [{"to": t, "status": "delivered"} for t in to]
        except PolicyDeniedError:
            decision_status = "blocked"
            self._ma_stats["messages_denied"] += 1
            recipient_exec = [{"to": t, "status": "blocked"} for t in to]
        except Exception:
            decision_status = "errored"
            recipient_exec = [{"to": t, "status": "errored"} for t in to]

        self._ma_stats["messages_total"] += 1
        if decision_status == "partial":
            self._ma_stats["messages_partial"] += 1

        self._emit_event(
            event_type="agent.message.outbound",
            parameters={
                "direction": "outbound",
                "from_agent": sender,
                "to_agents": to,
                "channel": channel,
                "message_id": message_id,
                "reply_to": reply_to,
                "content_preview": redacted_preview,
                "hashes": {"content_sha256": content_sha256},
                "correlation_ids": corr_map,
                "metadata": metadata or {},
            },
            privacy_flags=list(flags),
            redacted_fields=fields,
            context={"conversation_id": self._conversation_id},
            execution={"result": decision_status, "recipients": recipient_exec},
        )
        result.update({
            "message_id": message_id,
            "result": decision_status,
            "recipients": recipient_exec,
        })
        return result

    def record_inbound_message(
        self,
        *,
        from_agent: str,
        content: Any,
        message_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None:
            return None
        if self._conversation_id is None:
            return None
        redacted_preview, flags, fields = redact(content)
        try:
            content_canon = canonical_json(content)
        except Exception:
            content_canon = str(content)
        content_sha256 = hashlib.sha256(content_canon.encode("utf-8")).hexdigest()
        return self._emit_event(
            event_type="agent.message.inbound",
            parameters={
                "direction": "inbound",
                "from_agent": from_agent,
                "to_agent": self.agent_name,
                "message_id": message_id,
                "reply_to": reply_to,
                "content_preview": redacted_preview,
                "hashes": {"content_sha256": content_sha256},
                "metadata": metadata or {},
            },
            privacy_flags=list(flags),
            redacted_fields=fields,
            context={"conversation_id": self._conversation_id},
            execution={"status": "received"},
        )

    def handoff(self, *, to_agent: str, payload: Optional[Any] = None) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None:
            return None
        if self._conversation_id is None:
            self.start_conversation()
        redacted_preview, flags, fields = redact(payload)
        child_run_id = new_ulid()
        self._ma_stats["handoffs_started"] += 1
        return self._emit_event(
            event_type="agent.handoff.started",
            parameters={
                "from_agent": self.agent_name,
                "to_agent": to_agent,
                "payload_preview": redacted_preview,
            },
            privacy_flags=list(flags),
            redacted_fields=fields,
            context={"conversation_id": self._conversation_id},
            execution={"parent_run_id": self._session_id, "child_run_id": child_run_id, "status": "started"},
        )

    def complete_handoff(self, *, child_run_id: str) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None or self._conversation_id is None:
            return None
        self._ma_stats["handoffs_completed"] += 1
        return self._emit_event(
            event_type="agent.handoff.completed",
            parameters={"child_run_id": child_run_id},
            context={"conversation_id": self._conversation_id},
            execution={"status": "completed"},
        )

    def cancel_handoff(self, *, child_run_id: str, reason: Optional[str] = None) -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None or self._conversation_id is None:
            return None
        self._ma_stats["handoffs_cancelled"] += 1
        return self._emit_event(
            event_type="agent.handoff.cancelled",
            parameters={"child_run_id": child_run_id, "reason": reason},
            context={"conversation_id": self._conversation_id},
            execution={"status": "cancelled"},
        )

    def close_conversation(self, *, final_status: str = "success") -> Optional[str]:
        if not self._capture_multiagent or self._session_id is None or self._conversation_id is None:
            return None
        # Summary first
        self._emit_event(
            event_type="conversation.summary",
            parameters={
                "counts": dict(self._ma_stats),
            },
            context={"conversation_id": self._conversation_id},
            execution={"status": "summarized"},
        )
        # Close marker
        evt_hash = self._emit_event(
            event_type="conversation.closed",
            parameters={},
            context={"conversation_id": self._conversation_id},
            execution={"status": final_status},
        )
        # Reset conversation state
        self._conversation_id = None
        self._participants = {}
        self._ma_edges = []
        self._ma_msg_counter = 0
        self._ma_stats = {
            "messages_total": 0,
            "messages_allowed": 0,
            "messages_denied": 0,
            "messages_partial": 0,
            "handoffs_started": 0,
            "handoffs_completed": 0,
            "handoffs_cancelled": 0,
        }
        return evt_hash

    def _rehash_events(self) -> None:
        """Recompute the integrity chain over the current events list in-place.

        This ensures event_hash values reflect the final enriched events (after any
        guardrail/policy/context modifications) before serialization/transmission.
        """
        if not self._events:
            return
        prev_hash: Optional[str] = None
        first_hash: Optional[str] = None
        for evt in self._events:
            try:
                integ = evt.get("integrity") or {}
                # Ensure signature key exists (even if null) so hashing matches the canonical form.
                if "signature" not in integ:
                    integ["signature"] = None
                integ["prev_hash"] = prev_hash
                evt["integrity"] = integ
                h = compute_event_hash(evt)
                evt["integrity"]["event_hash"] = h
                if first_hash is None:
                    first_hash = h
                prev_hash = h
            except Exception:
                # Best-effort: if hashing fails, leave existing values untouched.
                continue
        if first_hash is not None:
            self._first_hash = first_hash
            self._last_hash = prev_hash

    def _refresh_error_trace_pointers(self) -> None:
        """Update output error summaries after final event rehashing."""
        if not self._errors or not self._events:
            return
        for item in self._errors:
            try:
                event_index = item.get("event_index")
                if not isinstance(event_index, int):
                    continue
                if event_index < 0 or event_index >= len(self._events):
                    continue
                event = self._events[event_index]
                item["event_hash"] = event.get("integrity", {}).get("event_hash")
                item["timestamp"] = event.get("timestamp") or item.get("timestamp")
            except Exception:
                continue

    def end_session(
        self,
        *,
        final_output: Any,
        model_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
        cost_estimate: Optional[float] = None,
    ) -> Dict[str, Any]:
        if self._session_id is None or self._started_at is None:
            raise RuntimeError("No active session. Call start_session() first.")

        if not self._outcome_emitted:
            self.record_outcome(status="success")

        status = self._run_result.get("status", "success")

        self.record_action(
            "agent.finished",
            status=status,
        )

        finished_at = _utcnow()
        duration_ms = int((finished_at - self._started_at).total_seconds() * 1000)

        redacted_output, _, _ = redact(final_output)
        # Conform to output-summary Avro schema: content must be a string.
        if isinstance(redacted_output, str):
            content_str = redacted_output
        elif isinstance(redacted_output, dict):
            content_str = (
                redacted_output.get("output")
                or redacted_output.get("content")
                or json.dumps(redacted_output, ensure_ascii=False)
            )
        else:
            content_str = str(redacted_output) if redacted_output is not None else ""

        model_id_final = model_id or self._model_id

        tools_used = _build_tool_summary(self._tool_stats)

        summary_latency = latency_ms if latency_ms is not None else duration_ms

        # Recompute the integrity chain just before emitting artifacts to ensure hashes
        # match the final event shape that will be serialized/sent.
        self._rehash_events()
        self._refresh_error_trace_pointers()

        out = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "timestamp": _iso(finished_at),
            "content": content_str,
            # Normalize prompts: ensure fields are strings to satisfy strict validators.
            "prompts": (
                {
                    "system": (self._prompt_hashes.get("system") or "") if getattr(self, "_prompt_hashes", None) else "",
                    "user": (self._prompt_hashes.get("user") or "") if getattr(self, "_prompt_hashes", None) else "",
                }
                if getattr(self, "_prompt_hashes", None)
                else None
            ),
            "tools_used": list(self._tool_stats.values()) if getattr(self, "_tool_stats", None) else [],
            "errors": list(self._errors) if getattr(self, "_errors", None) else [],
            "errors_truncated": int(self._errors_truncated) if getattr(self, "_errors_truncated", None) else 0,
            "actions": self._build_action_summary(),
            "integrity": {
                "first_event_hash": self._first_hash,
                "final_event_hash": self._last_hash,
                "chain_length": len(self._events),
            },
        }

        # Build artifacts in-memory before local persistence or backend delivery.
        # evidence.ndjson bytes (one JSON object per line)
        try:
            evidence_lines = []
            for evt in self._events:
                # Final guard: self-check hash right before serialization
                expected = evt.get("integrity", {}).get("event_hash")
                recomputed = compute_event_hash(evt)
                if expected != recomputed:
                    # Update to the recomputed value to avoid stale hashes
                    evt = dict(evt)
                    integ = dict(evt.get("integrity") or {})
                    integ["event_hash"] = recomputed
                    evt["integrity"] = integ
                evidence_lines.append(json.dumps(evt, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            # Fallback: best-effort serialization
            evidence_lines = [json.dumps(evt, default=str) for evt in self._events]
        evidence_bytes = ("\n".join(evidence_lines)).encode("utf-8")

        # output.json bytes
        try:
            output_bytes = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            output_bytes = json.dumps(out, default=str).encode("utf-8")

        # manifest.json bytes assembled from in-memory sizes/hashes
        produced_at = _iso(_utcnow())
        import hashlib as _hashlib
        ev_sha256 = _hashlib.sha256(evidence_bytes).hexdigest()
        out_sha256 = _hashlib.sha256(output_bytes).hexdigest()
        ev_lines = len(self._events)
        objects = [
            {
                "name": "evidence.jsonl",
                "bytes": len(evidence_bytes),
                "sha256": ev_sha256,
                "content_type": "application/x-ndjson",
                "lines": ev_lines,
            },
            {
                "name": "output.json",
                "bytes": len(output_bytes),
                "sha256": out_sha256,
                "content_type": "application/json",
                # Backend schema requires a number; default to 1 if we don't have a precise count.
                "lines": max(1, 1 if not hasattr(self, "_output_line_count") else getattr(self, "_output_line_count") or 1),
            },
        ]

        # Context block
        ctx: Dict[str, Any] = {
            "session_id": self._session_id,
            "env": self.environment,
            "toolchain_version": self.sdk_version,
            "evidence_schema_version": SCHEMA_VERSION,
            "started_at": _iso(self._started_at),
            "finished_at": _iso(finished_at),
        }

        manifest = {
            "schema_version": "manifest-v1",
            "hash_alg": "sha256-v1",
            "produced_at": produced_at,
            "session_id": self._session_id,  # Top-level session_id required by backend validation
            "context": ctx,
            "objects": objects,
            "chain_summary": {
                "first_event_hash": self._first_hash,
                "final_event_hash": self._last_hash,
                "chain_length": len(self._events),
            },
        }

        # Validate payloads against Schema Registry (if configured)
        try:
            validate_payloads(manifest, out, self._events)
        except Exception as e:
            # Never block the run on validation errors
            print(f"[validation] Error running validation: {e}")

        # Deliver artifacts to backend over HTTP. Best-effort; never raises.
        session_id_str = str(self._session_id)
        base_url = os.getenv("SETORRA_BACKEND", "").strip() or os.getenv("SETORRA_CONTROL_URL", "").strip()
        if not base_url and self.environment.strip().lower() == "local":
            base_url = "http://127.0.0.1:5001"
        token = getattr(self, "_remote_session_token", None)
        org_id = None
        if getattr(self, "_organization_info", None):
            org_id = self._organization_info.get("org_id")
        # Serialize manifest
        try:
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            manifest_bytes = json.dumps(manifest, default=str).encode("utf-8")

        # Always drop a local copy for debugging/validation (offline-friendly).
        # Use the configured collector storage directory by default; keep the
        # env override for harnesses and local backend probes that need a
        # dedicated artifact root.
        session_dir = Path(os.getenv("SETORRA_LOCAL_DATA_DIR") or self.storage_dir) / session_id_str
        evidence_path = session_dir / "evidence.jsonl"
        output_path = session_dir / "output.json"
        manifest_path = session_dir / "manifest.json"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            evidence_path.write_bytes(evidence_bytes)
            output_path.write_bytes(output_bytes)
            manifest_path.write_bytes(manifest_bytes)
        except Exception:
            # Never break the run if local persistence fails.
            pass

        metrics = None
        if os.getenv("SETORRA_DEBUG_INGEST"):
            try:
                ingest_prefix = os.getenv("SETORRA_INGEST_PREFIX", "/ingest")
                print(
                    f"[debug][ingest] base_url={base_url or 'offline'} prefix={ingest_prefix} "
                    f"session_id={session_id_str} evidence_bytes={len(evidence_bytes)} "
                    f"output_bytes={len(output_bytes)} manifest_bytes={len(manifest_bytes)}"
                )
                print(
                    f"[debug][ingest.hashes] evidence_sha256={ev_sha256} output_sha256={out_sha256} "
                    f"first_hash={self._first_hash} last_hash={self._last_hash}"
                )
            except Exception:
                pass
        if base_url and token:
            try:
                agent_label = f"{self.agent_name}/{self.agent_version}"
                metrics = deliver_artifacts(
                    base_url=base_url,
                    session_token=token,
                    org_id=org_id,
                    agent_label=agent_label,
                    session_id=session_id_str,
                    evidence_bytes=evidence_bytes,
                    output_bytes=output_bytes,
                    manifest_bytes=manifest_bytes,
                    timeout_s=float(os.getenv("SETORRA_BACKEND_TIMEOUT_MS", "2500")) / 1000.0 if os.getenv("SETORRA_BACKEND_TIMEOUT_MS") else 2.5,
                )
            except Exception:
                metrics = None
        if metrics is not None:
            try:
                e = metrics.get("evidence")
                o = metrics.get("output")
                m = metrics.get("manifest")
                total_bytes = len(evidence_bytes) + len(output_bytes) + len(manifest_bytes)
                ok = (e and 200 <= e.status < 300) and (o and 200 <= o.status < 300) and (m and 200 <= m.status < 300)
                print(
                    f"[backend] deliver session={session_id_str} bytes={total_bytes} "
                    f"status(e/o/m)={(e.status if e else 0)}/{(o.status if o else 0)}/{(m.status if m else 0)} "
                    f"ms(e/o/m)={(e.ms if e else 0):.1f}/{(o.ms if o else 0):.1f}/{(m.ms if m else 0):.1f} ok={str(bool(ok)).lower()}"
                )
            except Exception:
                pass

        # Note: Backend HTTP delivery intentionally omitted here; remain
        # offline-first unless explicitly integrated elsewhere.

        # Clear context
        session_id = self._session_id
        self._session_id = None
        set_session_id(None)
        self._started_at = None
        self._events = []
        self._bytes_estimate = 0
        self._first_hash = None
        self._last_hash = None
        self._environment_info = {}
        self._policy_info = {}
        # Reset multi-agent state (if any)
        self._conversation_id = None
        self._participants = {}
        self._ma_edges = []
        self._ma_msg_counter = 0
        self._ma_stats = {
            "messages_total": 0,
            "messages_allowed": 0,
            "messages_denied": 0,
            "messages_partial": 0,
            "handoffs_started": 0,
            "handoffs_completed": 0,
            "handoffs_cancelled": 0,
        }
        self._guardrail_info = {}
        self._prompt_info = {}
        self._prompt_event_parameters = {}
        self._prompt_privacy_flags = []
        self._prompt_redacted_fields = []
        self._prompt_hashes = {}
        self._tool_stats = {}
        self._tool_latest_input = {}
        self._reasoning_records = []
        self._consents = []
        self._run_result = {}
        self._outcome_emitted = False
        self._model_id = None
        self._last_policy_decision = None
        self._guardrail_summary = {}
        self._action_statuses = {}
        self._action_total = 0
        # Clear planning context
        self._current_plan_id = None
        self._current_plan_version = 0
        set_plan_id(None)
        with self._pending_lock:
            for pending in self._pending_approvals.values():
                pending.cancel()
            self._pending_approvals.clear()

        return {
            "session_id": session_id,
            "evidence_path": str(evidence_path),
            "output_path": str(output_path),
            "manifest_path": str(manifest_path),
            "artifact_dir": str(session_dir),
        }

    # ------------------------------------------------------------------
    # Event recording API
    # ------------------------------------------------------------------
    def apply_guardrails(
        self,
        action: str,
        *,
        actor: Optional[Dict[str, Any]] = None,
        parameters: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> GuardrailEvaluation:
        if self._session_id is None:
            raise RuntimeError("No active session. Call start_session() first.")

        actor_payload = actor or {}
        parameters_payload = {} if parameters is None else parameters
        context_payload = context or {}

        if self._guardrail_enforcer is None:
            try:
                parameters_copy = deepcopy(parameters_payload)
            except Exception:
                parameters_copy = parameters_payload
            try:
                context_copy = deepcopy(context_payload)
            except Exception:
                context_copy = context_payload
            return GuardrailEvaluation(
                action=action,
                status="allow",
                rule_id=None,
                message=None,
                parameters=parameters_copy,
                context=context_copy,
                applied_rules=tuple(),
                modifications=tuple(),
            )

        evaluation = self._guardrail_enforcer.evaluate(
            action,
            actor=actor_payload,
            parameters=parameters_payload,
            context=context_payload,
        )

        self._update_guardrail_summary(evaluation)

        guardrail_payload: Dict[str, Any] = {
            "status": evaluation.status,
            "action": action,
            "applied_rules": list(evaluation.applied_rules),
            "modifications": list(evaluation.modifications),
        }
        if evaluation.rule_id is not None:
            guardrail_payload["rule_id"] = evaluation.rule_id
        if evaluation.message:
            guardrail_payload["message"] = evaluation.message

        event_type = "guardrail.applied"
        status_payload = "allow"
        if evaluation.status == "modified":
            event_type = "guardrail.modified"
            status_payload = "modified"
        elif evaluation.status == "deny":
            event_type = "guardrail.blocked"
            status_payload = "denied"

        self.record_action(
            event_type,
            parameters={
                "action": action,
                "parameters": evaluation.parameters,
                "actor": actor_payload,
            },
            execution={"status": status_payload},
            guardrail=guardrail_payload,
        )

        if evaluation.status == "deny":
            reason = evaluation.message or "Guardrail denied the action"
            raise GuardrailViolationError(reason)

        return evaluation

    def enforce_policy(
        self,
        action: str,
        *,
        actor: Optional[Dict[str, Any]] = None,
        parameters: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
        wait_for_approval: Optional[bool] = None,
    ) -> PolicyDecision:
        if self._session_id is None:
            raise RuntimeError("No active session. Call start_session() first.")

        wait = self._auto_wait_for_approval if wait_for_approval is None else wait_for_approval

        intent_payload = self._build_intent_payload(
            action=action,
            actor=actor or {},
            parameters=parameters if parameters is not None else {},
            context=context or {},
        )

        if self._policy_decider is None:
            decision = self._fallback_allow_decision_from_intent(intent_payload)
            self._last_policy_decision = decision
            self._emit_policy_event(
                event_type="policy.decision",
                action=action,
                decision=decision,
                stage="evaluated",
                status=decision.decision,
            )
            return decision

        try:
            decision = self._policy_decider.evaluate(action, intent_payload)
        except PolicyError as exc:
            decision = self._fallback_allow_decision_from_intent(
                intent_payload,
                rationale=str(exc),
                fallback_mode="fail-open",
                fallback_trigger="policy_error",
            )

        self._last_policy_decision = decision
        self._update_policy_summary(decision)

        approval_payload = None
        if decision.approval_requirement is not None:
            approval_payload = self._approval_payload_from_requirement(decision.approval_requirement, status="pending")

        self._emit_policy_event(
            event_type="policy.decision",
            action=action,
            decision=decision,
            stage="evaluated",
            status=decision.decision,
            approval=approval_payload,
        )

        if decision.decision == "deny":
            raise PolicyDeniedError(f"Action '{action}' denied by policy")

        if decision.decision == "require_approval":
            requirement = decision.approval_requirement
            if requirement is None:
                raise PolicyError("Approval required but no approval policy metadata present")
            pending = self._register_pending_approval(action, decision)
            if self._approval_notifier is not None:
                try:
                    self._approval_notifier(requirement)
                except Exception:
                    pass
            if not wait:
                return decision
            try:
                receipt = self._wait_for_approval(pending)
            except PolicyApprovalTimeout as exc:
                with self._pending_lock:
                    self._pending_approvals.pop(decision.intent_hash, None)
                self._emit_policy_event(
                    event_type="policy.approval",
                    action=action,
                    decision=decision,
                    stage="expired",
                    status="expired",
                    approval=self._approval_payload_from_requirement(requirement, status="expired"),
                )
                self._update_policy_summary(decision)
                raise exc
            except PolicyApprovalCancelled as exc:
                with self._pending_lock:
                    self._pending_approvals.pop(decision.intent_hash, None)
                self._emit_policy_event(
                    event_type="policy.approval",
                    action=action,
                    decision=decision,
                    stage="cancelled",
                    status="cancelled",
                    approval=self._approval_payload_from_requirement(requirement, status="cancelled"),
                )
                self._update_policy_summary(decision)
                raise exc
            else:
                with self._pending_lock:
                    self._pending_approvals.pop(decision.intent_hash, None)
                final_decision = decision.with_receipt(receipt)
                self._last_policy_decision = final_decision
                self._update_policy_summary(final_decision)
                self._emit_policy_event(
                    event_type="policy.approval",
                    action=action,
                    decision=final_decision,
                    stage="approved",
                    status="approved",
                    approval=self._approval_payload_from_receipt(receipt, status="approved"),
                )
                return final_decision

        return decision

    def resume_with_approval(
        self,
        *,
        intent_hash: str,
        token: str,
        approver_id: Optional[str] = None,
        method: Optional[str] = None,
        approved_at: Optional[_dt.datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        with self._pending_lock:
            pending = self._pending_approvals.get(intent_hash)
        if pending is None:
            raise PolicyError(f"No pending approval for intent {intent_hash}")
        requirement = pending.decision.approval_requirement
        if requirement is None:
            raise PolicyError("Pending approval missing requirement metadata")
        now = _utcnow_tz()
        if requirement.expires_at < now:
            pending.cancel()
            with self._pending_lock:
                self._pending_approvals.pop(intent_hash, None)
            raise PolicyApprovalTimeout("Approval token expired before submission")
        receipt = PolicyApprovalReceipt(
            intent_hash=intent_hash,
            token=token,
            approver_id=approver_id,
            approved_at=(approved_at or now).astimezone(_dt.timezone.utc),
            method=method,
            metadata=metadata or {},
        )
        pending.provide(receipt)
        with self._pending_lock:
            self._pending_approvals.pop(intent_hash, None)
        final_decision = pending.decision.with_receipt(receipt)
        self._last_policy_decision = final_decision
        self._update_policy_summary(final_decision)
        self._emit_policy_event(
            event_type="policy.approval",
            action=pending.action,
            decision=final_decision,
            stage="approved",
            status="approved",
            approval=self._approval_payload_from_receipt(receipt, status="approved"),
        )
        return final_decision

    def cancel_pending_approval(
        self,
        *,
        intent_hash: str,
        reason: Optional[str] = None,
        status: str = "denied",
    ) -> None:
        with self._pending_lock:
            pending = self._pending_approvals.pop(intent_hash, None)
        if pending is None:
            return
        pending.cancel()
        original_decision = pending.decision
        requirement = original_decision.approval_requirement
        approval_payload = None
        if requirement is not None:
            payload = self._approval_payload_from_requirement(requirement, status=status)
            if reason:
                payload["reason"] = reason
            approval_payload = payload
        final_decision = PolicyDecision(
            decision="deny",
            policy_version=original_decision.policy_version,
            intent_hash=original_decision.intent_hash,
            bundle_hash=original_decision.bundle_hash,
            rule_id=original_decision.rule_id,
            rationale=reason or "approval_denied",
            fallback_mode=original_decision.fallback_mode,
            fallback_trigger=original_decision.fallback_trigger,
        )
        self._last_policy_decision = final_decision
        self._update_policy_summary(final_decision)
        self._emit_policy_event(
            event_type="policy.approval",
            action=pending.action,
            decision=final_decision,
            stage=status,
            status=status,
            approval=approval_payload,
        )

    # ------------------------------------------------------------------
    # Planning capture (optional, additive)
    # ------------------------------------------------------------------
    def record_plan_create(
        self,
        *,
        summary: str,
        steps: List[Any],
        rationale: Optional[str] = None,
    ) -> Optional[str]:
        """Capture a proposed plan. Returns the plan_id.

        When planning capture is disabled, this still records internal state
        for subsequent adoption but emits no evidence events.
        """
        if self._session_id is None:
            return None
        plan_id = self._current_plan_id or new_ulid()
        canon = self._canonicalize_plan(summary=summary, steps=steps)
        plan_hash = hashlib.sha256(canonical_json(canon).encode("utf-8")).hexdigest()
        self._current_plan_id = plan_id
        self._current_plan_version = 1
        set_plan_id(plan_id)

        rationale_redacted, flags, fields = redact(rationale)
        parameters = {
            "plan_id": plan_id,
            "stage": "proposed",
            "summary": canon["summary"],
            "steps": canon["steps"],
            "plan_hash": plan_hash,
        }
        if rationale_redacted is not None:
            parameters["rationale_preview"] = rationale_redacted

        if self._capture_plans:
            self._emit_event(
                event_type="plan.create",
                parameters=parameters,
                execution={"status": "proposed"},
                privacy_flags=list(flags),
                redacted_fields=fields,
                context={"plan": {"plan_id": plan_id, "version": 1}},
            )
        return plan_id

    def record_plan_refine(
        self,
        *,
        summary: Optional[str] = None,
        steps: Optional[List[Any]] = None,
        rationale: Optional[str] = None,
    ) -> Optional[str]:
        """Capture a refinement to the current plan (increments version)."""
        if self._session_id is None or self._current_plan_id is None:
            return None
        self._current_plan_version += 1
        plan_id = self._current_plan_id
        canon = self._canonicalize_plan(summary=summary, steps=steps)
        plan_hash = hashlib.sha256(canonical_json(canon).encode("utf-8")).hexdigest()
        rationale_redacted, flags, fields = redact(rationale)
        parameters = {
            "plan_id": plan_id,
            "stage": "refined",
            "version": self._current_plan_version,
            "plan_hash": plan_hash,
        }
        if "summary" in canon:
            parameters["summary"] = canon["summary"]
        if "steps" in canon:
            parameters["steps"] = canon["steps"]
        if rationale_redacted is not None:
            parameters["rationale_preview"] = rationale_redacted
        if self._capture_plans:
            self._emit_event(
                event_type="plan.refine",
                parameters=parameters,
                execution={"status": "refined"},
                privacy_flags=list(flags),
                redacted_fields=fields,
                context={"plan": {"plan_id": plan_id, "version": self._current_plan_version}},
            )
        return plan_id

    def record_plan_adopt(self) -> Optional[str]:
        """Capture adoption of the current plan for execution."""
        if self._session_id is None or self._current_plan_id is None:
            return None
        plan_id = self._current_plan_id
        if self._current_plan_version == 0:
            self._current_plan_version = 1
        set_plan_id(plan_id)
        if self._capture_plans:
            self._emit_event(
                event_type="plan.adopt",
                parameters={
                    "plan_id": plan_id,
                    "stage": "adopted",
                    "version": self._current_plan_version,
                },
                execution={"status": "adopted"},
                context={"plan": {"plan_id": plan_id, "version": self._current_plan_version}},
            )
        return plan_id

    def record_plan_abandon(self, *, reason: Optional[str] = None) -> Optional[str]:
        """Capture abandoning of the current plan and clear context."""
        if self._session_id is None or self._current_plan_id is None:
            return None
        plan_id = self._current_plan_id
        reason_redacted, flags, fields = redact(reason)
        if self._capture_plans:
            params = {
                "plan_id": plan_id,
                "stage": "abandoned",
                "version": self._current_plan_version or 1,
            }
            if reason_redacted is not None:
                params["reason_preview"] = reason_redacted
            self._emit_event(
                event_type="plan.abandon",
                parameters=params,
                execution={"status": "abandoned"},
                privacy_flags=list(flags),
                redacted_fields=fields,
                context={"plan": {"plan_id": plan_id, "version": self._current_plan_version or 1}},
            )
        # Clear plan context regardless of capture flag
        self._current_plan_id = None
        self._current_plan_version = 0
        set_plan_id(None)
        return plan_id

    def _canonicalize_plan(
        self,
        *,
        summary: Optional[str] = None,
        steps: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Return a canonical plan payload used for hashing and storage.

        Only includes provided fields; callers can pass partial updates.
        """
        out: Dict[str, Any] = {}
        if summary is not None:
            s = str(summary).strip()
            # Clip to a small preview budget to avoid large payloads
            out["summary"] = s[:256]
        if steps is not None:
            norm_steps: List[str] = []
            for item in steps:
                try:
                    text = str(item)
                except Exception:
                    text = "<unserializable>"
                norm_steps.append(text.strip())
            out["steps"] = norm_steps
        return out

    def pending_approvals(self) -> List[PolicyApprovalRequirement]:
        with self._pending_lock:
            return [
                pending.decision.approval_requirement
                for pending in self._pending_approvals.values()
                if pending.decision.approval_requirement is not None
            ]

    def record_action(
        self,
        event_type: str,
        *,
        parameters: Any = None,
        execution: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        model_id: Optional[str] = None,
        prompt_hashes: Optional[Dict[str, Optional[str]]] = None,
        environment: Optional[Dict[str, Any]] = None,
        output_preview: Any = None,
        latency_ms: Optional[int] = None,
        input_hash: Optional[str] = None,
        output_hash: Optional[str] = None,
        reasoning_hash: Optional[str] = None,
        reasoning_preview: Any = None,
        error_type: Optional[str] = None,
        error_preview: Any = None,
        policy: Optional[Dict[str, Any]] = None,
        tool_name: Optional[str] = None,
        approval: Optional[Dict[str, Any]] = None,
        guardrail: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Record a single event (tool/LLM/reasoning/etc.) to the evidence buffer."""

        if self._session_id is None:
            return None

        parameters_redacted, flags_params, fields_params = redact(parameters)
        output_redacted, flags_output, fields_output = redact(output_preview)
        error_redacted, flags_error, fields_error = redact(error_preview)
        reasoning_redacted, flags_reasoning, fields_reasoning = redact(reasoning_preview)

        privacy_flags = sorted({*flags_params, *flags_output, *flags_error, *flags_reasoning})
        redacted_fields = sorted({*fields_params, *fields_output, *fields_error, *fields_reasoning})

        exec_payload: Dict[str, Any] = dict(execution or {})
        if status is not None:
            exec_payload["status"] = status
        elif "status" not in exec_payload:
            exec_payload["status"] = "success"
        if latency_ms is not None:
            exec_payload["latency_ms"] = latency_ms
        if input_hash is not None:
            exec_payload["input_hash"] = input_hash
        if output_hash is not None:
            exec_payload["output_hash"] = output_hash
        if reasoning_hash is not None:
            exec_payload["reasoning_hash"] = reasoning_hash
        if reasoning_redacted is not None:
            exec_payload["reasoning_preview"] = reasoning_redacted
        if output_redacted is not None:
            exec_payload["output_preview"] = output_redacted
        if error_type is not None:
            exec_payload["error_type"] = error_type
        if error_redacted is not None:
            exec_payload["error_preview"] = error_redacted

        # Enrich execution with tool/model info and redacted params/previews
        if tool_name is not None:
            exec_payload.setdefault("tool", tool_name)
        # If this is an LLM event and no model attached, default to current model_id
        if event_type.startswith("llm") and model_id is None and self._model_id:
            exec_payload.setdefault("model", self._model_id)
        if model_id is not None:
            exec_payload.setdefault("model", model_id)
        if parameters_redacted is not None and parameters_redacted != {}:
            exec_payload.setdefault("parameters_preview", parameters_redacted)
        if output_redacted is not None:
            exec_payload.setdefault("output_preview", output_redacted)
        if reasoning_redacted is not None:
            exec_payload.setdefault("reasoning_preview", reasoning_redacted)

        # Classify tool auth failures and augment execution payload.
        # Only runs for tool.error; evidence remains additive and privacy-safe.
        will_append_error_summary = False
        classified_auth_failed = False
        classified_error_code: Optional[int] = None
        classified_reason: Optional[str] = None
        event_index_pre = len(self._events)
        if event_type == "tool.error":
            will_append_error_summary = True
            classified_auth_failed, classified_error_code, classified_reason = _classify_tool_auth_failure(
                error_type=error_type,
                error_preview=error_redacted,
            )
            if classified_auth_failed:
                exec_payload["auth_failed"] = True
                if classified_error_code is not None:
                    exec_payload["error_code"] = classified_error_code
                if classified_reason is not None:
                    exec_payload["reason_code"] = classified_reason

        context_payload: Dict[str, Any] = {}
        if model_id is not None:
            context_payload["model_id"] = model_id
        if prompt_hashes is not None:
            context_payload["prompts"] = prompt_hashes
        if environment is not None:
            context_payload["environment"] = environment
        # Thread plan context when enabled and present
        if self._capture_plans and self._current_plan_id:
            context_payload["plan"] = {
                "plan_id": self._current_plan_id,
                "version": self._current_plan_version or 1,
            }
        # Attach org and model/prompt defaults if not already present
        if not context_payload.get("model") and self._model_id:
            context_payload["model"] = {"name": self._model_id}
        if not context_payload.get("prompts") and getattr(self, "_prompt_hashes", None):
            context_payload["prompts"] = self._prompt_hashes
        if self._environment_info and "environment" not in context_payload:
            context_payload["environment"] = self._environment_info
        if self._organization_info.get("org_id"):
            org = {"org_id": self._organization_info.get("org_id")}
            key_prefix = self._organization_info.get("key_id_prefix")
            if key_prefix:
                org["key_id_prefix"] = key_prefix
            org_name = self._organization_info.get("org_name")
            if org_name:
                org["org_name"] = org_name
            context_payload.setdefault("organization", {}).update(org)

        event_hash = self._emit_event(
            event_type=event_type,
            parameters=parameters_redacted if parameters_redacted is not None else {},
            execution=exec_payload,
            context=context_payload,
            privacy_flags=privacy_flags,
            redacted_fields=redacted_fields,
            policy=policy,
            approval=approval,
            guardrail=guardrail,
        )
        self._track_action_event(event_type, parameters_redacted, exec_payload)

        if event_type.startswith("tool"):
            name = tool_name
            if name is None:
                if isinstance(parameters, dict):
                    name = str(parameters.get("name") or "<tool>")
                elif isinstance(parameters_redacted, dict):
                    name = str(parameters_redacted.get("name") or "<tool>")
                else:
                    name = "<tool>"
            # Ensure execution carries the resolved tool name
            if name and "tool" not in exec_payload:
                exec_payload["tool"] = name
            if event_type == "tool.start":
                self._tool_latest_input[name] = input_hash
            if event_type == "tool.end":
                stats = self._tool_stats.setdefault(
                    name,
                    {"name": name, "count": 0, "total_duration_ms": 0, "last_input_hash": None, "last_output_hash": None},
                )
                stats["count"] += 1
                if latency_ms is not None:
                    stats["total_duration_ms"] += latency_ms
                stats["last_input_hash"] = input_hash or self._tool_latest_input.get(name)
                stats["last_output_hash"] = output_hash
            if event_type == "tool.error" and will_append_error_summary:
                # Append a summary item for output.json errors[] with trace pointers.
                try:
                    # Best-effort: use the last event to fetch timestamp and confirm hash.
                    ts = None
                    if self._events and self._events[-1]["integrity"]["event_hash"] == event_hash:
                        ts = self._events[-1].get("timestamp")
                    summary_item: Dict[str, Any] = {
                        "event_hash": event_hash,
                        "event_index": event_index_pre,
                        "timestamp": ts or _iso(_utcnow()),
                        "tool": name,
                        "error_type": error_type,
                        "message": "" if error_redacted is None else str(error_redacted),
                        "message_preview": error_redacted,
                        "auth_failed": bool(classified_auth_failed),
                    }
                    if classified_error_code is not None:
                        summary_item["error_code"] = classified_error_code
                    if classified_reason is not None:
                        summary_item["reason_code"] = classified_reason
                    if len(self._errors) < self._errors_cap:
                        self._errors.append(summary_item)
                    else:
                        self._errors_truncated += 1
                except Exception:
                    # Never let summary bookkeeping break evidence emission
                    pass

        if event_type == "reasoning" and reasoning_hash is not None:
            self._reasoning_records.append({"hash": reasoning_hash, "preview": reasoning_redacted})

        return event_hash

    def record_user_consent(
        self,
        *,
        scope: str,
        question: str,
        response: str,
        decision: str,
        method: str = "CLI",
        approver_id: Optional[str] = None,
    ) -> Optional[str]:
        """Capture a user's consent decision as a dedicated evidence event."""

        if self._session_id is None:
            return None

        decided_at = _iso(_utcnow())

        question_redacted, flags_q, fields_q = redact(question)
        response_redacted, flags_r, fields_r = redact(response)

        def _map_fields(label: str, fields: List[str]) -> List[str]:
            mapped: List[str] = []
            for field in fields:
                if not field or field == "$":
                    mapped.append(label)
                else:
                    mapped.append(f"{label}.{field.lstrip('.')}")
            return mapped

        privacy_flags = sorted(set(flags_q) | set(flags_r))
        redacted_fields = sorted(
            set(_map_fields("question", fields_q) + _map_fields("response", fields_r))
        )

        payload_for_hash = {
            "scope": scope,
            "question": question_redacted,
            "response": response_redacted,
            "decision": decision,
            "method": method,
            "approver_id": approver_id,
            "approved_at": decided_at,
        }
        consent_hash = hashlib.sha256(canonical_json(payload_for_hash).encode("utf-8")).hexdigest()

        approval_block = {
            "approver_id": approver_id,
            "approved_at": decided_at,
            "method": method,
            "signature": None,
        }

        self._emit_event(
            event_type="user.consent",
            parameters={
                "scope": scope,
                "question": question_redacted,
                "response": response_redacted,
                "decision": decision,
                "method": method,
                "consent_hash": consent_hash,
            },
            execution={"status": decision},
            privacy_flags=privacy_flags,
            redacted_fields=redacted_fields,
            approval=approval_block,
        )

        self._consents.append(
            {
                "scope": scope,
                "decision": decision,
                "approved_at": decided_at,
                "method": method,
                "consent_hash": consent_hash,
            }
        )

        return consent_hash

    def prompt_hashes(self) -> Dict[str, Optional[str]]:
        """Expose prompt hashes for instrumentation callbacks."""

        return dict(self._prompt_hashes)

    def _track_action_event(self, event_type: str, parameters: Any, execution: Dict[str, Any]) -> None:
        if not event_type.startswith("action."):
            return
        params = parameters if isinstance(parameters, dict) else {}
        action_id = params.get("action_id") or execution.get("action_id")
        if not action_id:
            return
        action_id = str(action_id)
        if event_type == "action.requested":
            self._action_total += 1
        status = str(execution.get("status") or params.get("decision") or event_type.split(".", 1)[1])
        record = self._action_statuses.setdefault(
            action_id,
            {
                "action_id": action_id,
                "action": params.get("action"),
                "system": params.get("system"),
                "idempotency_key": params.get("idempotency_key"),
                "payload_hash": execution.get("payload_hash") or params.get("payload_hash"),
                "context_hash": execution.get("context_hash") or params.get("context_hash"),
            },
        )
        if params.get("action") is not None:
            record["action"] = params.get("action")
        if params.get("system") is not None:
            record["system"] = params.get("system")
        if params.get("idempotency_key") is not None:
            record["idempotency_key"] = params.get("idempotency_key")
        if params.get("payload_hash") is not None:
            record["payload_hash"] = params.get("payload_hash")
        if params.get("context_hash") is not None:
            record["context_hash"] = params.get("context_hash")
        if params.get("result_hash") is not None:
            record["result_hash"] = params.get("result_hash")
        record["status"] = status
        record["event_type"] = event_type

    def _build_action_summary(self) -> Dict[str, Any]:
        final_statuses = list(self._action_statuses.values())
        by_status: Dict[str, int] = {}
        for item in final_statuses:
            status = str(item.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": int(self._action_total),
            "by_status": by_status,
            "final_statuses": final_statuses,
        }

    def _fallback_allow_decision_from_intent(
        self,
        intent: Dict[str, Any],
        *,
        rationale: str = "no_policy",
        fallback_mode: Optional[str] = None,
        fallback_trigger: Optional[str] = None,
    ) -> PolicyDecision:
        intent_hash = self._hash_intent(intent)
        decision = PolicyDecision(
            decision="allow",
            policy_version=str(self._policy_info.get("version", "unconfigured")),
            intent_hash=intent_hash,
            bundle_hash=self._policy_info.get("bundle_hash"),
            rule_id=None,
            rationale=rationale,
            fallback_mode=fallback_mode,
            fallback_trigger=fallback_trigger,
        )
        self._update_policy_summary(decision)
        return decision

    def _hash_intent(self, intent: Dict[str, Any]) -> str:
        payload = canonical_json(intent)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _build_intent_payload(
        self,
        *,
        action: str,
        actor: Dict[str, Any],
        parameters: Any,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "session_id": self._session_id,
            "agent": {"name": self.agent_name, "version": self.agent_version},
            "action": action,
            "actor": self._sanitize_for_intent(actor),
            "parameters": self._sanitize_for_intent(parameters),
            "context": self._sanitize_for_intent(context),
        }

    def _sanitize_for_intent(self, value: Any, *, _depth: int = 0, _max_depth: int = 5) -> Any:
        if _depth > _max_depth:
            return "<depth-limit>"
        if value is None or isinstance(value, (int, float, str, bool)):
            return value
        if isinstance(value, numbers.Number):
            return float(value)
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}B>"
        if isinstance(value, (list, tuple)):
            return [self._sanitize_for_intent(item, _depth=_depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            sanitized_items = [self._sanitize_for_intent(item, _depth=_depth + 1) for item in value]
            return [item for _, item in sorted((repr(item), item) for item in sanitized_items)]
        if isinstance(value, dict):
            sanitized: Dict[str, Any] = {}
            for key, item in value.items():
                try:
                    sanitized[str(key)] = self._sanitize_for_intent(item, _depth=_depth + 1)
                except Exception:
                    sanitized[str(key)] = "<unserializable>"
            return sanitized
        if hasattr(value, "__name__"):
            return f"<callable:{value.__name__}>"
        if hasattr(value, "__class__"):
            return f"<{value.__class__.__name__}>"
        try:
            return str(value)
        except Exception:
            return "<unserializable>"

    def _register_pending_approval(self, action: str, decision: PolicyDecision) -> PendingApproval:
        pending = PendingApproval(
            action=action,
            decision=decision,
            created_at=_utcnow_tz(),
            condition=threading.Condition(),
        )
        with self._pending_lock:
            self._pending_approvals[decision.intent_hash] = pending
        return pending

    def _wait_for_approval(self, pending: PendingApproval) -> PolicyApprovalReceipt:
        requirement = pending.decision.approval_requirement
        if requirement is None:
            raise PolicyError("Pending approval missing requirement metadata")
        now = _utcnow_tz()
        timeout = (requirement.expires_at - now).total_seconds()
        if timeout <= 0:
            pending.cancel()
            with self._pending_lock:
                self._pending_approvals.pop(pending.decision.intent_hash, None)
            raise PolicyApprovalTimeout("Approval window expired")
        receipt = pending.wait(timeout=timeout)
        return receipt

    def _approval_payload_from_requirement(
        self,
        requirement: PolicyApprovalRequirement,
        *,
        status: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": status,
            "intent_hash": requirement.intent_hash,
            "expires_at": _iso_utc(requirement.expires_at),
        }
        if requirement.approver_roles:
            payload["approver_roles"] = list(requirement.approver_roles)
        if requirement.metadata:
            payload["metadata"] = dict(requirement.metadata)
        return payload

    def _approval_payload_from_receipt(
        self,
        receipt: PolicyApprovalReceipt,
        *,
        status: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": status,
            "intent_hash": receipt.intent_hash,
            "token": receipt.token,
            "approved_at": _iso_utc(receipt.approved_at),
        }
        if receipt.approver_id:
            payload["approver_id"] = receipt.approver_id
        if receipt.method:
            payload["method"] = receipt.method
        if receipt.metadata:
            payload["metadata"] = dict(receipt.metadata)
        return payload

    def _emit_policy_event(
        self,
        *,
        event_type: str,
        action: str,
        decision: PolicyDecision,
        stage: str,
        status: str,
        approval: Optional[Dict[str, Any]] = None,
    ) -> str:
        policy_block: Dict[str, Any] = {
            "version": decision.policy_version,
            "decision": decision.decision,
            "intent_hash": decision.intent_hash,
        }
        if decision.bundle_hash:
            policy_block["bundle_hash"] = decision.bundle_hash
        if decision.rule_id:
            policy_block["rule"] = decision.rule_id
        if decision.fallback_mode:
            policy_block["fallback_mode"] = decision.fallback_mode
        if decision.fallback_trigger:
            policy_block["fallback_trigger"] = decision.fallback_trigger

        parameters = {
            "action": action,
            "stage": stage,
            "status": status,
        }
        if decision.rationale:
            parameters["rationale"] = decision.rationale

        return self._emit_event(
            event_type=event_type,
            parameters=parameters,
            execution={"status": status},
            policy=policy_block,
            approval=approval,
        )

    def _update_policy_summary(self, decision: PolicyDecision) -> None:
        self._policy_info["version"] = decision.policy_version
        self._policy_info["decision"] = decision.decision
        self._policy_info["intent_hash"] = decision.intent_hash
        if decision.rule_id is not None:
            self._policy_info["rule_id"] = decision.rule_id
        else:
            self._policy_info.pop("rule_id", None)
        if decision.bundle_hash is not None:
            self._policy_info["bundle_hash"] = decision.bundle_hash
        if decision.fallback_mode is not None:
            self._policy_info["fallback_mode"] = decision.fallback_mode
        else:
            self._policy_info.pop("fallback_mode", None)
        if decision.fallback_trigger is not None:
            self._policy_info["fallback_trigger"] = decision.fallback_trigger
        else:
            self._policy_info.pop("fallback_trigger", None)
        if decision.approval_requirement is not None:
            self._policy_info["approval_expires_at"] = _iso_utc(decision.approval_requirement.expires_at)
        else:
            self._policy_info.pop("approval_expires_at", None)
        if decision.approval_receipt is not None:
            self._policy_info["approval_token"] = decision.approval_receipt.token
            if decision.approval_receipt.approver_id:
                self._policy_info["approver_id"] = decision.approval_receipt.approver_id
        else:
            self._policy_info.pop("approval_token", None)
            self._policy_info.pop("approver_id", None)

    def _update_guardrail_summary(self, evaluation: GuardrailEvaluation) -> None:
        summary = self._guardrail_summary or {"decision": "not_evaluated", "applied_rules": []}
        if "bundle_version" not in summary and "version" in self._guardrail_info:
            summary["bundle_version"] = self._guardrail_info.get("version")
        applied = summary.setdefault("applied_rules", [])
        for rule_id in evaluation.applied_rules:
            if rule_id not in applied:
                applied.append(rule_id)
        summary["decision"] = evaluation.status
        summary["last_status"] = evaluation.status
        if evaluation.rule_id is not None:
            summary["last_rule_id"] = evaluation.rule_id
        else:
            summary.pop("last_rule_id", None)
        if evaluation.message:
            messages = summary.setdefault("messages", [])
            messages.append(evaluation.message)
        if evaluation.modifications:
            summary["modification_count"] = summary.get("modification_count", 0) + len(evaluation.modifications)
        summary["last_updated_at"] = _iso(_utcnow())
        self._guardrail_summary = summary

    def _get_policy_bundle(self) -> Optional[Any]:
        if self._policy_decider is None:
            return None
        try:
            return self._policy_decider.current_bundle()
        except PolicyError:
            return None

    def _get_guardrail_bundle(self) -> Optional[Any]:
        if self._guardrail_enforcer is None:
            return None
        try:
            return self._guardrail_enforcer.current_bundle()
        except GuardrailError:
            return None

    def _emit_session_manifest(self) -> None:
        manifest_parameters = {
            "environment": self._environment_info,
            "policy": self._policy_info,
            "guardrail": self._guardrail_info,
        }
        if self._signing_key_id:
            manifest_parameters["signing"] = {
                "key_id": self._signing_key_id,
                "algorithm": "HMAC-SHA256",
            }

        self._emit_event(
            event_type="session.manifest",
            parameters=manifest_parameters,
            execution={"status": "ready"},
        )

    def _emit_prompt_capture(self) -> None:
        self._emit_event(
            event_type="prompt.capture",
            parameters=self._prompt_event_parameters,
            execution={"status": "captured"},
            context={"prompts": self._prompt_hashes},
            privacy_flags=self._prompt_privacy_flags,
            redacted_fields=self._prompt_redacted_fields,
        )

    def record_outcome(
        self,
        status: str,
        *,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        linked_event_hash: Optional[str] = None,
    ) -> Optional[str]:
        """Emit the agent.outcome event exactly once and cache the summary."""

        if self._session_id is None:
            return None
        if self._outcome_emitted:
            return self._run_result.get("linked_event_hash")

        error_preview_redacted = None
        if error_message is not None:
            error_preview_redacted, _, _ = redact(error_message)

        outcome_hash = self.record_action(
            "agent.outcome",
            status=status,
            error_type=error_type,
            error_preview=error_preview_redacted,
            execution={"linked_event_hash": linked_event_hash},
        )

        if linked_event_hash is None:
            linked_event_hash = outcome_hash

        self._run_result = {
            "status": status,
            "error_type": error_type,
            "error_preview": error_preview_redacted,
            "linked_event_hash": linked_event_hash,
        }
        self._outcome_emitted = True
        return outcome_hash

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _emit_event(
        self,
        *,
        event_type: str,
        parameters: Any,
        execution: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        privacy_flags: Optional[List[str]] = None,
        redacted_fields: Optional[List[str]] = None,
        policy: Optional[Dict[str, Any]] = None,
        approval: Optional[Dict[str, Any]] = None,
        guardrail: Optional[Dict[str, Any]] = None,
    ) -> str:
        assert self._session_id is not None
        event_index = len(self._events)
        # Emit evidence in the Avro-aligned shape expected by SR (no legacy agent_name/agent_version).
        evt: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "event_index": event_index,
            "event_type": event_type,
            "timestamp": _iso(_dt.datetime.utcnow()),
            "agent": {"name": self.agent_name, "version": self.agent_version},
            "privacy_flags": privacy_flags or [],
            "redacted_fields": redacted_fields or [],
            "parameters_redacted": None,
            "context": {},
            "execution": None,
            "policy": None,
            "approval": None,
            "guardrail": None,
            "integrity": {"prev_hash": self._last_hash, "event_hash": None},
        }

        # Attach context: environment/model/prompts/org if available, plus caller-provided context.
        try:
            ctx_block: Dict[str, Any] = {}
            if context:
                ctx_block.update(context)
            # Environment info (from start_session)
            if self._environment_info:
                ctx_block.setdefault("environment", self._environment_info)
            # Model info
            if self._model_id:
                ctx_block.setdefault("model", {"name": self._model_id})
            # Prompts (hashes/previews) if captured
            if getattr(self, "_prompt_hashes", None):
                ctx_block.setdefault("prompts", self._prompt_hashes)
            # Organization linkage
            if self._organization_info.get("org_id"):
                org = {"org_id": self._organization_info.get("org_id")}
                key_prefix = self._organization_info.get("key_id_prefix")
                if key_prefix:
                    org["key_id_prefix"] = key_prefix
                org_name = self._organization_info.get("org_name")
                if org_name:
                    org["org_name"] = org_name
                ctx_block.setdefault("organization", {}).update(org)
            if ctx_block:
                evt["context"] = ctx_block
            else:
                evt["context"] = None
        except Exception:
            evt["context"] = None

        # Execution metadata
        if execution:
            evt["execution"] = execution

        # Policy/approval/guardrail blocks
        if policy:
            evt["policy"] = policy
        elif self._policy_info:
            evt["policy"] = dict(self._policy_info)
        if approval:
            evt["approval"] = approval
        if guardrail:
            evt["guardrail"] = guardrail
        elif self._guardrail_info:
            evt["guardrail"] = dict(self._guardrail_info)

        # Populate signature field before hashing so signature (even null) is part of the hash.
        if self._signing_key is not None:
            signable = json.loads(canonical_json(evt))
            signable["integrity"].pop("signature", None)
            signature_bytes = hmac.new(
                self._signing_key,
                canonical_json(signable).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            evt["integrity"]["signature"] = signature_bytes
            if self._signing_key_id:
                evt.setdefault("context", {}).setdefault("signing", {})["key_id"] = self._signing_key_id
        else:
            # Explicitly include signature=None so downstream hash matches canonical form.
            evt["integrity"]["signature"] = None

        # Compute event hash after all enrichments and after integrity.signature is present
        event_hash = compute_event_hash(evt)
        evt["integrity"]["event_hash"] = event_hash

        if self._first_hash is None:
            self._first_hash = event_hash
        self._last_hash = event_hash

        # Append and enforce buffer limit (soft enforcement: summarize on overflow)
        self._events.append(evt)
        self._bytes_estimate += _estimate_size(evt)
        if self._bytes_estimate > self.max_buffer_bytes:
            # Replace middle events with a single summary to cap memory.
            # Keep the first and last few events intact.
            head = self._events[:3]
            tail = self._events[-3:]
            summary = {
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id,
                "event_index": len(head),
                "event_type": "agent.buffer_compacted",
                "timestamp": _iso(_dt.datetime.utcnow()),
                "agent": {"name": self.agent_name, "version": self.agent_version},
                "parameters_redacted": {"dropped_events": len(self._events) - 6},
                "privacy_flags": [],
                "redacted_fields": [],
                "context": {},
                "execution": {"status": "compacted"},
                "policy": dict(self._policy_info),
                "approval": None,
                "guardrail": dict(self._guardrail_info),
                "integrity": {"prev_hash": self._last_hash, "event_hash": None, "signature": None},
            }
            summary_hash = compute_event_hash(summary)
            summary["integrity"]["event_hash"] = summary_hash
            self._events = head + [summary] + tail
            self._bytes_estimate = sum(_estimate_size(e) for e in self._events)

        return event_hash


def setorra_collector(agent_name: str, agent_version: str) -> SetorraCollector:
    """Factory for a Setorra collector with sensible defaults and storage directory.

    Usage:
        collector = setorra_collector("name", "1.0.0")
    """
    environment = os.getenv("SETORRA_ENVIRONMENT", "local")
    hostname = os.getenv("SETORRA_HOSTNAME")
    signing_key = os.getenv("SETORRA_SIGNING_KEY")
    signing_key_id = os.getenv("SETORRA_SIGNING_KEY_ID")
    policy_path = os.getenv("SETORRA_POLICY_PATH")
    policy_loader = None
    if policy_path:
        resolved_path = Path(policy_path)
        if not resolved_path.exists():
            raise RuntimeError(f"SETORRA_POLICY_PATH points to missing file: {resolved_path}")
        policy_loader = PolicyLoader(resolved_path)
        try:
            policy_loader.get_bundle()
        except Exception as exc:  # noqa: BLE001 - surface loader errors early
            raise RuntimeError(f"Failed to load policy bundle at {resolved_path}: {exc}") from exc
    guardrail_path = os.getenv("SETORRA_GUARDRAIL_PATH")
    guardrail_loader = None
    if guardrail_path:
        resolved_guardrail = Path(guardrail_path)
        if not resolved_guardrail.exists():
            raise RuntimeError(f"SETORRA_GUARDRAIL_PATH points to missing file: {resolved_guardrail}")
        guardrail_loader = GuardrailLoader(resolved_guardrail)
        try:
            guardrail_loader.get_bundle()
        except Exception as exc:  # noqa: BLE001 - surface loader errors early
            raise RuntimeError(f"Failed to load guardrail bundle at {resolved_guardrail}: {exc}") from exc
    auto_wait_raw = os.getenv("SETORRA_POLICY_AUTO_WAIT", "true").strip().lower()
    auto_wait = auto_wait_raw not in {"0", "false", "no"}
    # Construct the collector first; then optionally perform a handshake
    # and attach organization identity. This preserves existing behavior
    # when no API key is provided and avoids failing closed for networking.
    collector = SetorraCollector(
        agent_name=agent_name,
        agent_version=agent_version,
        storage_dir=DEFAULT_DIR,
        environment=environment,
        hostname=hostname,
        signing_key=signing_key,
        signing_key_id=signing_key_id,
        policy_loader=policy_loader,
        auto_wait_for_approval=auto_wait,
        guardrail_loader=guardrail_loader,
    )

    api_key = os.getenv("SETORRA_API_KEY", "").strip()
    # Support both SETORRA_BACKEND (canonical) and legacy/internal SETORRA_CONTROL_URL
    backend_url = os.getenv("SETORRA_BACKEND", "").strip() or os.getenv("SETORRA_CONTROL_URL", "").strip()
    # Convenience for local development: if environment is local and no backend
    # URL is provided, default to IPv4 loopback to avoid IPv6/localhost pitfalls.
    if not backend_url and environment.strip().lower() == "local":
        backend_url = "http://127.0.0.1:5001"
    if api_key and backend_url:
        try:
            ua = f"SetorraSDK/{collector.sdk_version} ({collector.runtime})"
            client = HandshakeClient(base_url=backend_url, user_agent=ua)
            try:
                resp = client.handshake(api_key)
            except (TransientNetworkError, HandshakeError):
                # Retry once by forcing IPv4 loopback if caller used 'localhost'
                # which may resolve to ::1 where servers typically bind only to
                # 127.0.0.1.
                if "localhost" in backend_url:
                    fallback_url = backend_url.replace("localhost", "127.0.0.1")
                    client = HandshakeClient(base_url=fallback_url, user_agent=ua)
                    resp = client.handshake(api_key)
                else:
                    raise
            # Store minimal identity in memory only; never persist secrets.
            collector._organization_info = {
                "org_id": resp.org_id,
                "key_id_prefix": (resp.key_id[:8] if resp.key_id else None),
            }
            # Include optional organization name when provided by backend
            if getattr(resp, "org_name", None):
                collector._organization_info["org_name"] = resp.org_name
            collector._remote_session_token = resp.session_token
            # Normalize to naive UTC for consistency with other timestamps
            collector._remote_token_expires_at = resp.expires_at.astimezone(_dt.timezone.utc)
        except (HandshakeError, InvalidApiKey, RevokedKey):
            # Offline-first: proceed without remote identity.
            pass
        except Exception:
            # Never let handshake side-effects break local behavior.
            pass

    return collector


def _estimate_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return sys.getsizeof(obj)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.utcnow().replace(tzinfo=None)


def _iso(dt: _dt.datetime) -> str:
    # Ensure Zulu suffix for UTC (even though naive here); keep consistent format
    return dt.replace(microsecond=0).isoformat() + "Z"


def _utcnow_tz() -> _dt.datetime:
    return _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)


def _iso_utc(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _prepare_prompts(
    system_prompt: Optional[str],
    user_prompt: Optional[str],
) -> Tuple[Dict[str, Dict[str, Optional[str]]], Dict[str, Dict[str, Optional[str]]], List[str], List[str]]:
    prompts: Dict[str, Dict[str, Optional[str]]] = {}
    event_parameters: Dict[str, Dict[str, Optional[str]]] = {}
    privacy_flags: Set[str] = set()
    redacted_fields: Set[str] = set()

    for key, prompt in (("system", system_prompt), ("user", user_prompt)):
        if prompt is None:
            prompts[key] = {"hash": None, "redacted": None}
            event_parameters[key] = {"hash": None, "redacted": None}
            continue

        redacted_prompt, flags, fields = redact(prompt)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompts[key] = {"hash": prompt_hash, "redacted": redacted_prompt}
        event_parameters[key] = {"hash": prompt_hash, "redacted": redacted_prompt}
        privacy_flags.update(flags)
        if fields:
            for field in fields:
                mapped = f"{key}.{field}" if field and field != "$" else key
                redacted_fields.add(mapped)
        elif flags:
            redacted_fields.add(key)

    return prompts, event_parameters, sorted(privacy_flags), sorted(redacted_fields)


def _build_tool_summary(tool_stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary = []
    for stats in tool_stats.values():
        summary.append(
            {
                "name": stats.get("name"),
                "count": stats.get("count", 0),
                "total_duration_ms": stats.get("total_duration_ms", 0),
                "last_input_hash": stats.get("last_input_hash"),
                "last_output_hash": stats.get("last_output_hash"),
            }
        )
    summary.sort(key=lambda item: item.get("name") or "")
    return summary


def _detect_sdk_version() -> str:
    try:
        return importlib_metadata.version("setorra")
    except Exception:  # pragma: no cover - fallback for local development
        return "0.0.0-dev"


def _detect_runtime() -> str:
    return f"python-{platform.python_version()}"


# ----------------------------------------------------------------------
# Minimal, deterministic classifier for authentication/permission failures.
# Operates on redacted previews only. Returns a tuple of:
# (auth_failed: bool, error_code: Optional[int], reason_code: Optional[str])
# ----------------------------------------------------------------------

_RX_401 = re.compile(r"\b401\b")
_RX_403 = re.compile(r"\b403\b")
_RX_UNAUTHORIZED = re.compile(r"\bunauthori[sz]ed\b", re.IGNORECASE)
_RX_NOT_AUTH = re.compile(r"\bnot\s+authorized\b", re.IGNORECASE)
_RX_FORBIDDEN = re.compile(r"\bforbidden\b", re.IGNORECASE)
_RX_PERM_DENIED = re.compile(r"\bpermission\s+denied\b", re.IGNORECASE)
_RX_INSUFF_SCOPES = re.compile(r"\binsufficient(?:\s|_)?(?:authentication)?scopes?\b", re.IGNORECASE)
_RX_INVALID_GRANT = re.compile(r"\binvalid[_\s-]?grant\b", re.IGNORECASE)
_RX_SMTP_535 = re.compile(r"\b535\b")
_RX_AUTH_FAILED = re.compile(r"\bauthentication\s+failed\b", re.IGNORECASE)
_RX_SMTP_AUTHERR = re.compile(r"\bSMTPAuthenticationError\b", re.IGNORECASE)
_RX_GRPC_UNAUTH = re.compile(r"\bUNAUTHENTICATED\b")
_RX_GRPC_PERM = re.compile(r"\bPERMISSION[_ ]DENIED\b", re.IGNORECASE)


def _looks_non_ascii(text: str) -> bool:
    try:
        return any(ord(ch) > 127 for ch in text)
    except Exception:
        return False


def _classify_tool_auth_failure(
    *, error_type: Optional[str], error_preview: Optional[str]
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Classify a tool error as an authentication/permission failure.

    - Uses only redacted previews and safe exception names.
    - Prefers explicit numeric/protocol cues (401/403/gRPC/SMTP) to reduce FPs.
    - Returns (auth_failed, error_code, reason_code).
    """
    # Normalize inputs
    preview = (error_preview or "").strip()
    etype = (error_type or "").strip()

    # Prefer protocol/explicit numeric cues
    if _RX_GRPC_UNAUTH.search(etype) or _RX_GRPC_UNAUTH.search(preview):
        return True, 401, "AUTH_UNAUTHENTICATED"
    if _RX_GRPC_PERM.search(etype) or _RX_GRPC_PERM.search(preview):
        return True, 403, "AUTH_FORBIDDEN"

    if _RX_401.search(preview):
        return True, 401, "AUTH_UNAUTHENTICATED"
    if _RX_403.search(preview):
        return True, 403, "AUTH_FORBIDDEN"

    # SMTP/IMAP auth failures
    if _RX_SMTP_535.search(preview) or _RX_SMTP_AUTHERR.search(etype) or _RX_SMTP_AUTHERR.search(preview):
        return True, None, "AUTH_SMTP_535"

    # OAuth-specific
    if _RX_INVALID_GRANT.search(preview):
        return True, 401, "AUTH_EXPIRED"
    if _RX_INSUFF_SCOPES.search(preview):
        return True, 403, "AUTH_SCOPE_INSUFFICIENT"

    # Generic keyword fallbacks (avoid in localized text without numeric cues)
    if preview and _looks_non_ascii(preview):
        return False, None, None

    if _RX_UNAUTHORIZED.search(preview) or _RX_NOT_AUTH.search(preview):
        return True, 401, "AUTH_UNAUTHENTICATED"
    if _RX_FORBIDDEN.search(preview) or _RX_PERM_DENIED.search(preview):
        return True, 403, "AUTH_FORBIDDEN"
    if _RX_AUTH_FAILED.search(preview):
        return True, None, "AUTH_GENERIC"

    return False, None, None


## (helper removed during revert)
