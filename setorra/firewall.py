"""Action-first firewall API for controlled agent actions."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from .approvals import ApprovalRecord, InMemoryApprovalStore
from .collector import SetorraCollector
from .guardrails import GuardrailViolationError
from .id import new_ulid
from .integrity import canonical_json
from .policy import PolicyDecision, PolicyDeniedError, PolicyError


ActionDecisionStatus = str


@dataclass(frozen=True)
class ActionRequest:
    """Canonical request shape for a business action attempted by an agent."""

    agent_id: str
    action: str
    system: str
    payload: Any = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None
    compensation: Optional[Dict[str, Any]] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ActionRequest":
        context = data.get("context") or {}
        if not isinstance(context, dict):
            context = {"value": context}
        return cls(
            agent_id=str(data.get("agentId") or data.get("agent_id") or ""),
            action=str(data.get("action") or ""),
            system=str(data.get("system") or ""),
            payload=data.get("payload") if "payload" in data else {},
            context=dict(context),
            idempotency_key=(
                str(data.get("idempotencyKey") or data.get("idempotency_key"))
                if data.get("idempotencyKey") or data.get("idempotency_key")
                else None
            ),
            compensation=(
                dict(data.get("compensation"))
                if isinstance(data.get("compensation"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class ActionDecision:
    """Firewall decision returned before any external execution occurs."""

    decision: ActionDecisionStatus
    action_id: str
    policy_version: str
    intent_hash: Optional[str]
    payload_hash: str
    context_hash: str
    rule_id: Optional[str] = None
    rationale: Optional[str] = None
    reason_code: Optional[str] = None
    approval: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ActionResult:
    """Structured result for `firewall.execute(...)`.

    The MVP does not call external systems. `status` describes the control
    outcome only; connector execution is introduced in later sprint plans.
    """

    action_id: str
    decision: ActionDecisionStatus
    status: str
    allowed: bool
    request: ActionRequest
    payload_hash: str
    context_hash: str
    policy: Dict[str, Any]
    approval: Optional[Dict[str, Any]] = None
    missing_context: tuple[str, ...] = tuple()
    result_hash: Optional[str] = None
    output: Any = None


class ActionFirewall:
    """Runtime control plane for high-risk agent actions."""

    def __init__(self, collector: SetorraCollector, approval_store: Optional[InMemoryApprovalStore] = None) -> None:
        self._collector = collector
        self._approval_store = approval_store or InMemoryApprovalStore()

    def execute(
        self,
        request: ActionRequest | Mapping[str, Any],
        executor: Optional[Callable[[ActionRequest], Any]] = None,
    ) -> ActionResult:
        req = request if isinstance(request, ActionRequest) else ActionRequest.from_mapping(request)
        action_id = _derive_action_id(req)
        payload_hash = _hash_payload(req.payload)
        context_hash = _hash_payload(req.context)
        missing = _missing_required_fields(req)

        request_parameters = {
            "action_id": action_id,
            "agent_id": req.agent_id,
            "action": req.action,
            "system": req.system,
            "idempotency_key": req.idempotency_key,
            "payload": req.payload,
            "context": req.context,
            "payload_hash": payload_hash,
            "context_hash": context_hash,
            "compensation": req.compensation,
        }

        self._collector.record_action(
            "action.requested",
            parameters=request_parameters,
            execution={
                "status": "requested",
                "action_id": action_id,
                "system": req.system,
                "idempotency_key": req.idempotency_key,
                "payload_hash": payload_hash,
                "context_hash": context_hash,
            },
        )

        if missing:
            return self._finish_without_policy(
                req,
                action_id=action_id,
                decision="needs_more_context",
                status="needs_more_context",
                payload_hash=payload_hash,
                context_hash=context_hash,
                reason_code="MISSING_REQUIRED_FIELDS",
                missing_context=tuple(missing),
            )

        actor = {"type": "agent", "id": req.agent_id}
        firewall_context = {
            "action_id": action_id,
            "system": req.system,
            "idempotency_key": req.idempotency_key,
            **req.context,
        }
        payload = req.payload

        try:
            guardrail = self._collector.apply_guardrails(
                req.action,
                actor=actor,
                parameters=payload,
                context=firewall_context,
            )
            payload = guardrail.parameters
            firewall_context = guardrail.context
            req = ActionRequest(
                agent_id=req.agent_id,
                action=req.action,
                system=req.system,
                payload=payload,
                context=dict(firewall_context),
                idempotency_key=req.idempotency_key,
                compensation=req.compensation,
            )
            payload_hash = _hash_payload(req.payload)
            context_hash = _hash_payload(req.context)
        except GuardrailViolationError as exc:
            return self._finish_without_policy(
                req,
                action_id=action_id,
                decision="deny",
                status="blocked",
                payload_hash=payload_hash,
                context_hash=context_hash,
                reason_code="GUARDRAIL_DENY",
                rationale=str(exc),
            )

        try:
            policy_decision = self._collector.enforce_policy(
                req.action,
                actor=actor,
                parameters={
                    "system": req.system,
                    "payload": payload,
                    "idempotency_key": req.idempotency_key,
                    "payload_hash": payload_hash,
                },
                context={
                    **firewall_context,
                    "context_hash": context_hash,
                },
                wait_for_approval=False,
            )
        except PolicyDeniedError as exc:
            policy_decision = getattr(self._collector, "_last_policy_decision", None)
            return self._finish_with_policy(
                req,
                action_id=action_id,
                decision="deny",
                status="blocked",
                payload_hash=payload_hash,
                context_hash=context_hash,
                policy_decision=policy_decision,
                reason_code="POLICY_DENY",
                rationale=str(exc),
            )
        except PolicyError as exc:
            return self._finish_without_policy(
                req,
                action_id=action_id,
                decision="deny",
                status="blocked",
                payload_hash=payload_hash,
                context_hash=context_hash,
                reason_code="POLICY_ERROR",
                rationale=str(exc),
            )

        mapped = _map_policy_decision(policy_decision.decision)
        status = {
            "allow": "allowed",
            "deny": "blocked",
            "needs_approval": "pending_approval",
            "needs_more_context": "needs_more_context",
        }[mapped]
        result = self._finish_with_policy(
            req,
            action_id=action_id,
            decision=mapped,
            status=status,
            payload_hash=payload_hash,
            context_hash=context_hash,
            policy_decision=policy_decision,
        )
        if mapped == "allow" and executor is not None:
            return self._execute_allowed_action(
                req,
                action_id=action_id,
                payload_hash=payload_hash,
                context_hash=context_hash,
                policy_decision=policy_decision,
                executor=executor,
            )
        return result

    def approve(
        self,
        *,
        action_id: str,
        intent_hash: str,
        approver_id: str,
        approver_email: Optional[str] = None,
        token: Optional[str] = None,
        method: str = "local",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRecord:
        record = self._approval_store.resolve(
            action_id=action_id,
            intent_hash=intent_hash,
            status="approved",
            approver_id=approver_id,
            approver_email=approver_email,
            metadata=metadata,
        )
        receipt_metadata = dict(metadata or {})
        if approver_email:
            receipt_metadata["approver_email"] = approver_email
        self._collector.resume_with_approval(
            intent_hash=intent_hash,
            token=token or f"approval:{action_id}",
            approver_id=approver_id,
            method=method,
            metadata=receipt_metadata,
        )
        self._record_approval_resolved(record)
        return record

    def deny(
        self,
        *,
        action_id: str,
        intent_hash: str,
        approver_id: Optional[str] = None,
        approver_email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        record = self._approval_store.resolve(
            action_id=action_id,
            intent_hash=intent_hash,
            status="denied",
            approver_id=approver_id,
            approver_email=approver_email,
            metadata={"reason": reason} if reason else None,
        )
        self._collector.cancel_pending_approval(intent_hash=intent_hash, reason=reason, status="denied")
        self._record_approval_resolved(record)
        return record

    def request_more_context(
        self,
        *,
        action_id: str,
        intent_hash: str,
        approver_id: Optional[str] = None,
        approver_email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        record = self._approval_store.resolve(
            action_id=action_id,
            intent_hash=intent_hash,
            status="needs_more_context",
            approver_id=approver_id,
            approver_email=approver_email,
            metadata={"reason": reason} if reason else None,
        )
        self._collector.cancel_pending_approval(intent_hash=intent_hash, reason=reason, status="needs_more_context")
        self._record_approval_resolved(record)
        return record

    def cancel(self, *, action_id: str, intent_hash: str, reason: Optional[str] = None) -> ApprovalRecord:
        record = self._approval_store.resolve(
            action_id=action_id,
            intent_hash=intent_hash,
            status="cancelled",
            metadata={"reason": reason} if reason else None,
        )
        self._collector.cancel_pending_approval(intent_hash=intent_hash, reason=reason, status="cancelled")
        self._record_approval_resolved(record)
        return record

    def expire(self, *, action_id: str, intent_hash: str) -> ApprovalRecord:
        record = self._approval_store.resolve(
            action_id=action_id,
            intent_hash=intent_hash,
            status="expired",
        )
        self._collector.cancel_pending_approval(intent_hash=intent_hash, reason="approval_expired", status="expired")
        self._record_approval_resolved(record)
        return record

    def execute_approved(
        self,
        request: ActionRequest | Mapping[str, Any],
        *,
        action_id: str,
        intent_hash: str,
        executor: Callable[[ActionRequest], Any],
    ) -> ActionResult:
        req = request if isinstance(request, ActionRequest) else ActionRequest.from_mapping(request)
        payload_hash = _hash_payload(req.payload)
        effective_context = {
            "action_id": action_id,
            "system": req.system,
            "idempotency_key": req.idempotency_key,
            **req.context,
        }
        context_hash = _hash_payload(effective_context)
        self._approval_store.assert_approved(
            action_id=action_id,
            intent_hash=intent_hash,
            payload_hash=payload_hash,
            context_hash=context_hash,
        )
        req = ActionRequest(
            agent_id=req.agent_id,
            action=req.action,
            system=req.system,
            payload=req.payload,
            context=effective_context,
            idempotency_key=req.idempotency_key,
            compensation=req.compensation,
        )
        return self._execute_allowed_action(
            req,
            action_id=action_id,
            payload_hash=payload_hash,
            context_hash=context_hash,
            policy_decision=getattr(self._collector, "_last_policy_decision", None),
            executor=executor,
        )

    def execute_with_connector(self, request: ActionRequest | Mapping[str, Any], connector: Any) -> ActionResult:
        req = request if isinstance(request, ActionRequest) else ActionRequest.from_mapping(request)
        action_id_holder: Dict[str, str] = {}

        def _call_connector(approved_request: ActionRequest) -> Any:
            from .connectors import ConnectorRequest

            action_id = action_id_holder["action_id"]
            return connector.execute(ConnectorRequest.from_action_request(action_id, approved_request))

        result = self.execute(req)
        action_id_holder["action_id"] = result.action_id
        if result.decision == "allow":
            return self._execute_allowed_action(
                result.request,
                action_id=result.action_id,
                payload_hash=result.payload_hash,
                context_hash=result.context_hash,
                policy_decision=getattr(self._collector, "_last_policy_decision", None),
                executor=_call_connector,
            )
        return result

    def execute_approved_with_connector(
        self,
        request: ActionRequest | Mapping[str, Any],
        *,
        action_id: str,
        intent_hash: str,
        connector: Any,
    ) -> ActionResult:
        def _call_connector(approved_request: ActionRequest) -> Any:
            from .connectors import ConnectorRequest

            return connector.execute(ConnectorRequest.from_action_request(action_id, approved_request))

        return self.execute_approved(
            request,
            action_id=action_id,
            intent_hash=intent_hash,
            executor=_call_connector,
        )

    def _execute_allowed_action(
        self,
        request: ActionRequest,
        *,
        action_id: str,
        payload_hash: str,
        context_hash: str,
        policy_decision: Optional[PolicyDecision],
        executor: Callable[[ActionRequest], Any],
    ) -> ActionResult:
        policy_payload = _policy_payload(policy_decision)
        self._collector.record_action(
            "action.executed",
            parameters={
                "action_id": action_id,
                "action": request.action,
                "system": request.system,
                "idempotency_key": request.idempotency_key,
                "payload_hash": payload_hash,
                "context_hash": context_hash,
                "compensation": _compensation_payload(request.compensation),
            },
            execution={
                "status": "executed",
                "action_id": action_id,
                "payload_hash": payload_hash,
                "context_hash": context_hash,
            },
            policy=policy_payload or None,
        )
        try:
            output = executor(request)
        except Exception as exc:
            self._collector.record_action(
                "action.failed",
                parameters={
                    "action_id": action_id,
                    "action": request.action,
                    "system": request.system,
                    "idempotency_key": request.idempotency_key,
                    "payload_hash": payload_hash,
                    "context_hash": context_hash,
                    "compensation": _compensation_payload(request.compensation),
                },
                status="failed",
                error_type=type(exc).__name__,
                error_preview=str(exc),
                policy=policy_payload or None,
            )
            raise
        output_record = _jsonable(output)
        result_hash = _hash_payload(output_record)
        self._collector.record_action(
            "action.succeeded",
            parameters={
                "action_id": action_id,
                "action": request.action,
                "system": request.system,
                "idempotency_key": request.idempotency_key,
                "payload_hash": payload_hash,
                "context_hash": context_hash,
                "result_hash": result_hash,
                "compensation": _compensation_payload(request.compensation),
            },
            status="succeeded",
            output_preview=output_record,
            output_hash=result_hash,
            policy=policy_payload or None,
        )
        return ActionResult(
            action_id=action_id,
            decision="allow",
            status="succeeded",
            allowed=True,
            request=request,
            payload_hash=payload_hash,
            context_hash=context_hash,
            policy=policy_payload,
            result_hash=result_hash,
            output=output_record,
        )

    def _finish_without_policy(
        self,
        request: ActionRequest,
        *,
        action_id: str,
        decision: ActionDecisionStatus,
        status: str,
        payload_hash: str,
        context_hash: str,
        reason_code: Optional[str] = None,
        rationale: Optional[str] = None,
        missing_context: tuple[str, ...] = tuple(),
    ) -> ActionResult:
        self._collector.record_action(
            _terminal_event_type(decision),
            parameters={
                "action_id": action_id,
                "action": request.action,
                "system": request.system,
                "decision": decision,
                "reason_code": reason_code,
                "rationale": rationale,
                "missing_context": list(missing_context),
            },
            status=status,
        )
        return ActionResult(
            action_id=action_id,
            decision=decision,
            status=status,
            allowed=decision == "allow",
            request=request,
            payload_hash=payload_hash,
            context_hash=context_hash,
            policy={},
            missing_context=missing_context,
            result_hash=None,
        )

    def _finish_with_policy(
        self,
        request: ActionRequest,
        *,
        action_id: str,
        decision: ActionDecisionStatus,
        status: str,
        payload_hash: str,
        context_hash: str,
        policy_decision: Optional[PolicyDecision],
        reason_code: Optional[str] = None,
        rationale: Optional[str] = None,
    ) -> ActionResult:
        policy_payload = _policy_payload(policy_decision)
        approval_payload = _approval_payload(policy_decision)
        if decision == "needs_approval" and policy_decision is not None:
            approval_payload = dict(approval_payload or {})
            approval_payload["action_id"] = action_id
            self._approval_store.create(
                ApprovalRecord(
                    action_id=action_id,
                    intent_hash=policy_decision.intent_hash,
                    payload_hash=payload_hash,
                    context_hash=context_hash,
                    status="pending",
                    expires_at=approval_payload.get("expires_at"),
                    metadata={"policy_version": policy_decision.policy_version},
                )
            )
            self._collector.record_action(
                "approval.requested",
                parameters={
                    "action_id": action_id,
                    "action": request.action,
                    "intent_hash": policy_decision.intent_hash,
                    "payload_hash": payload_hash,
                    "context_hash": context_hash,
                    "expires_at": approval_payload.get("expires_at"),
                },
                status="pending",
                approval=approval_payload,
                policy=policy_payload or None,
            )
        if reason_code:
            policy_payload["reason_code"] = reason_code
        if rationale:
            policy_payload["rationale"] = rationale
        self._collector.record_action(
            _terminal_event_type(decision),
            parameters={
                "action_id": action_id,
                "action": request.action,
                "system": request.system,
                "decision": decision,
            },
            status=status,
            policy=policy_payload or None,
            approval=approval_payload,
        )
        return ActionResult(
            action_id=action_id,
            decision=decision,
            status=status,
            allowed=decision == "allow",
            request=request,
            payload_hash=payload_hash,
            context_hash=context_hash,
            policy=policy_payload,
            approval=approval_payload,
            result_hash=None,
        )

    def _record_approval_resolved(self, record: ApprovalRecord) -> None:
        approval = {
            "status": record.status,
            "action_id": record.action_id,
            "intent_hash": record.intent_hash,
            "approver_id": record.approver_id,
            "approved_at": record.resolved_at,
            "metadata": record.metadata,
        }
        if record.approver_email:
            approval["approver_email"] = record.approver_email
        self._collector.record_action(
            "approval.resolved",
            parameters={
                "action_id": record.action_id,
                "intent_hash": record.intent_hash,
                "status": record.status,
                "payload_hash": record.payload_hash,
                "context_hash": record.context_hash,
            },
            status=record.status,
            approval=approval,
        )


def firewall(collector: SetorraCollector, approval_store: Optional[InMemoryApprovalStore] = None) -> ActionFirewall:
    """Create an action firewall around an existing collector."""

    return ActionFirewall(collector, approval_store=approval_store)


def _missing_required_fields(request: ActionRequest) -> list[str]:
    missing = []
    if not request.agent_id:
        missing.append("agent_id")
    if not request.action:
        missing.append("action")
    if not request.system:
        missing.append("system")
    return missing


def _derive_action_id(request: ActionRequest) -> str:
    if request.idempotency_key:
        basis = {
            "agent_id": request.agent_id,
            "action": request.action,
            "system": request.system,
            "idempotency_key": request.idempotency_key,
        }
        return "act_" + hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()[:32]
    return new_ulid()


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _compensation_payload(compensation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not compensation:
        return {"available": False}
    return {
        "available": bool(compensation.get("available", True)),
        "type": compensation.get("type"),
        "reference": compensation.get("reference"),
    }


def _map_policy_decision(decision: str) -> ActionDecisionStatus:
    if decision == "require_approval":
        return "needs_approval"
    if decision in {"allow", "deny", "needs_more_context"}:
        return decision
    return "deny"


def _terminal_event_type(decision: ActionDecisionStatus) -> str:
    if decision == "allow":
        return "action.allowed"
    if decision == "needs_approval":
        return "action.needs_approval"
    if decision == "needs_more_context":
        return "action.needs_more_context"
    return "action.blocked"


def _policy_payload(decision: Optional[PolicyDecision]) -> Dict[str, Any]:
    if decision is None:
        return {}
    payload: Dict[str, Any] = {
        "version": decision.policy_version,
        "decision": decision.decision,
        "intent_hash": decision.intent_hash,
    }
    if decision.bundle_hash:
        payload["bundle_hash"] = decision.bundle_hash
    if decision.rule_id:
        payload["rule_id"] = decision.rule_id
    if decision.rationale:
        payload["rationale"] = decision.rationale
    if decision.fallback_mode:
        payload["fallback_mode"] = decision.fallback_mode
    if decision.fallback_trigger:
        payload["fallback_trigger"] = decision.fallback_trigger
    return payload


def _approval_payload(decision: Optional[PolicyDecision]) -> Optional[Dict[str, Any]]:
    if decision is None or decision.approval_requirement is None:
        return None
    requirement = decision.approval_requirement
    payload: Dict[str, Any] = {
        "status": "pending",
        "intent_hash": requirement.intent_hash,
        "expires_at": requirement.expires_at.isoformat().replace("+00:00", "Z"),
    }
    if requirement.approver_roles:
        payload["approver_roles"] = list(requirement.approver_roles)
    if requirement.metadata:
        payload["metadata"] = dict(requirement.metadata)
    return payload
