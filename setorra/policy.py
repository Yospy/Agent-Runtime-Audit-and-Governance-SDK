"""Policy loading and decision evaluation for the Setorra SDK."""

from __future__ import annotations

import datetime as _dt
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .integrity import canonical_json


_DECISIONS = {"allow", "deny", "require_approval", "needs_more_context"}
_FAIL_MODES = {"fail-open", "fail-closed", "require-approval"}


class PolicyError(RuntimeError):
    """Raised when a policy bundle is invalid or cannot be applied."""


class PolicyDeniedError(RuntimeError):
    """Raised when a policy denies an action outright."""


class PolicyApprovalTimeout(RuntimeError):
    """Raised when an approval request expires without a token."""


class PolicyApprovalCancelled(RuntimeError):
    """Raised when an approval request is cancelled before completion."""


@dataclass(frozen=True)
class PolicyDefaults:
    decision: str
    fail_mode: str


@dataclass(frozen=True)
class PolicyApprovalSpec:
    requires_token: bool
    expires_in: int
    approver_roles: Tuple[str, ...] = tuple()
    metadata: Dict[str, Any] = field(default_factory=dict)


def _default_approval_spec() -> PolicyApprovalSpec:
    return PolicyApprovalSpec(
        requires_token=True,
        expires_in=900,
        approver_roles=tuple(),
        metadata={},
    )


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    decision: str
    match: Dict[str, Any]
    approval: Optional[PolicyApprovalSpec]
    rationale: Optional[str]


@dataclass(frozen=True)
class PolicyAction:
    name: str
    default_decision: str
    fail_mode: str
    rules: Tuple[PolicyRule, ...]
    approval: Optional[PolicyApprovalSpec]


@dataclass(frozen=True)
class PolicyBundle:
    version: str
    metadata: Dict[str, Any]
    defaults: PolicyDefaults
    sdk_defaults: PolicyDefaults
    actions: Dict[str, PolicyAction]
    source_path: Optional[Path]
    bundle_hash: str
    loaded_at: _dt.datetime


@dataclass
class PolicyApprovalRequirement:
    intent_hash: str
    expires_at: _dt.datetime
    approver_roles: Tuple[str, ...]
    metadata: Dict[str, Any]


@dataclass
class PolicyApprovalReceipt:
    intent_hash: str
    token: str
    approver_id: Optional[str]
    approved_at: _dt.datetime
    method: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyDecision:
    decision: str
    policy_version: str
    intent_hash: str
    bundle_hash: Optional[str]
    rule_id: Optional[str]
    rationale: Optional[str]
    fallback_mode: Optional[str]
    fallback_trigger: Optional[str]
    approval_requirement: Optional[PolicyApprovalRequirement] = None
    approval_receipt: Optional[PolicyApprovalReceipt] = None

    def with_receipt(self, receipt: PolicyApprovalReceipt) -> "PolicyDecision":
        updated = PolicyDecision(
            decision="allow",
            policy_version=self.policy_version,
            intent_hash=self.intent_hash,
            bundle_hash=self.bundle_hash,
            rule_id=self.rule_id,
            rationale=self.rationale,
            fallback_mode=self.fallback_mode,
            fallback_trigger=self.fallback_trigger,
            approval_requirement=self.approval_requirement,
            approval_receipt=receipt,
        )
        return updated


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _canonical_hash(payload: Any) -> str:
    return canonical_json(payload)


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_path(obj: Any, path: str) -> Any:
    parts = path.split(".") if path else []
    current = obj
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if 0 <= idx < len(current):
                current = current[idx]
                continue
            return None
        return None
    return current


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    raise PolicyError(f"Cannot interpret boolean from {value!r}")


def _ensure_decision(raw: str) -> str:
    if raw not in _DECISIONS:
        raise PolicyError(f"Unsupported decision '{raw}'")
    return raw


def _ensure_fail_mode(raw: str) -> str:
    if raw not in _FAIL_MODES:
        raise PolicyError(f"Unsupported fail mode '{raw}'")
    return raw


def _parse_defaults(data: Dict[str, Any], fallback_decision: str, fallback_fail_mode: str) -> PolicyDefaults:
    decision = _ensure_decision(str(data.get("decision", fallback_decision)))
    fail_mode = _ensure_fail_mode(str(data.get("fail_mode", fallback_fail_mode)))
    return PolicyDefaults(decision=decision, fail_mode=fail_mode)


def _parse_approval(spec: Optional[Dict[str, Any]]) -> Optional[PolicyApprovalSpec]:
    if spec is None:
        return None
    requires_token = _coerce_bool(spec.get("requires_token", True))
    expires_raw = spec.get("expires_in", 900)
    if not isinstance(expires_raw, int) or expires_raw <= 0:
        raise PolicyError("approval.expires_in must be a positive integer")
    approver_roles: Tuple[str, ...] = tuple()
    if "approver_roles" in spec:
        roles = spec["approver_roles"]
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise PolicyError("approval.approver_roles must be a list of strings")
        approver_roles = tuple(sorted(set(roles)))
    metadata = spec.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise PolicyError("approval.metadata must be an object")
    return PolicyApprovalSpec(
        requires_token=requires_token,
        expires_in=expires_raw,
        approver_roles=approver_roles,
        metadata=dict(metadata),
    )


def _parse_rule(action_name: str, raw: Dict[str, Any], index: int) -> PolicyRule:
    rule_id = str(raw.get("id")) if raw.get("id") else f"{action_name}:rule:{index}"
    decision_raw = raw.get("decision")
    if decision_raw is None:
        raise PolicyError(f"Rule {rule_id} for {action_name} missing decision")
    decision = _ensure_decision(str(decision_raw))
    match = raw.get("match", {})
    if not isinstance(match, dict):
        raise PolicyError(f"Rule {rule_id} match must be an object")
    approval = _parse_approval(raw.get("approval"))
    if decision == "require_approval" and approval is None:
        approval = _default_approval_spec()
    rationale = raw.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise PolicyError(f"Rule {rule_id} rationale must be a string")
    return PolicyRule(
        rule_id=rule_id,
        decision=decision,
        match=match,
        approval=approval,
        rationale=rationale,
    )


def _parse_action(name: str, raw: Dict[str, Any], defaults: PolicyDefaults) -> PolicyAction:
    default_decision_raw = raw.get("default_decision", defaults.decision)
    default_decision = _ensure_decision(str(default_decision_raw))
    fail_mode_raw = raw.get("fail_mode", defaults.fail_mode)
    fail_mode = _ensure_fail_mode(str(fail_mode_raw))
    rules_data = raw.get("rules", [])
    if not isinstance(rules_data, list):
        raise PolicyError(f"Action {name} rules must be a list")
    rules: List[PolicyRule] = []
    for idx, item in enumerate(rules_data):
        if not isinstance(item, dict):
            raise PolicyError(f"Action {name} rule entry {idx} must be an object")
        rules.append(_parse_rule(name, item, idx))
    approval = _parse_approval(raw.get("approval"))
    if default_decision == "require_approval" and approval is None:
        approval = _default_approval_spec()
    return PolicyAction(
        name=name,
        default_decision=default_decision,
        fail_mode=fail_mode,
        rules=tuple(rules),
        approval=approval,
    )


def _validate_bundle(payload: Dict[str, Any], source_path: Optional[Path]) -> PolicyBundle:
    version = payload.get("version")
    if not isinstance(version, str):
        raise PolicyError("Policy bundle requires a string version")
    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise PolicyError("metadata must be an object")
    sdk_defaults_raw = payload.get("sdk", {})
    if not isinstance(sdk_defaults_raw, dict):
        raise PolicyError("sdk defaults must be an object")
    defaults_raw = payload.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise PolicyError("defaults must be an object")
    sdk_defaults = _parse_defaults(sdk_defaults_raw, "allow", "fail-open")
    defaults = _parse_defaults(defaults_raw, sdk_defaults.decision, sdk_defaults.fail_mode)
    actions_raw = payload.get("actions", {})
    if actions_raw is None:
        actions_raw = {}
    if not isinstance(actions_raw, dict):
        raise PolicyError("actions must be an object")
    actions: Dict[str, PolicyAction] = {}
    for action_name, action_body in actions_raw.items():
        if not isinstance(action_body, dict):
            raise PolicyError(f"Action {action_name} must be an object")
        actions[action_name] = _parse_action(action_name, action_body, defaults)
    bundle_hash = _sha256(_canonical_hash(payload))
    return PolicyBundle(
        version=version,
        metadata=dict(metadata),
        defaults=defaults,
        sdk_defaults=sdk_defaults,
        actions=actions,
        source_path=source_path,
        bundle_hash=bundle_hash,
        loaded_at=_now(),
    )


class PolicyLoader:
    """Load and cache a policy bundle from a JSON file."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cached_bundle: Optional[PolicyBundle] = None
        self._mtime: Optional[float] = None

    @property
    def path(self) -> Path:
        return self._path

    def get_bundle(self) -> PolicyBundle:
        with self._lock:
            stat = self._path.stat()
            need_reload = self._mtime is None or stat.st_mtime != self._mtime or self._cached_bundle is None
            if not need_reload and self._cached_bundle is not None:
                return self._cached_bundle
            payload = _load_json(self._path)
            if not isinstance(payload, dict):
                raise PolicyError("Policy file must contain a JSON object")
            bundle = _validate_bundle(payload, self._path)
            self._cached_bundle = bundle
            self._mtime = stat.st_mtime
            return bundle


def _evaluate_condition(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        for op, value in expected.items():
            if op == "eq":
                if actual != value:
                    return False
            elif op == "neq":
                if actual == value:
                    return False
            elif op == "lt":
                if actual is None or float(actual) >= float(value):
                    return False
            elif op == "lte":
                if actual is None or float(actual) > float(value):
                    return False
            elif op == "gt":
                if actual is None or float(actual) <= float(value):
                    return False
            elif op == "gte":
                if actual is None or float(actual) < float(value):
                    return False
            elif op == "in":
                values = value if isinstance(value, (list, tuple, set)) else [value]
                if actual not in values:
                    return False
            elif op == "not_in":
                values = value if isinstance(value, (list, tuple, set)) else [value]
                if actual in values:
                    return False
            elif op == "contains":
                if isinstance(actual, (list, tuple, set)):
                    haystack = [str(item).casefold() for item in actual]
                    needle = str(value).casefold()
                    if needle not in haystack:
                        return False
                elif isinstance(actual, str):
                    haystack = actual.casefold()
                    needle = str(value).casefold()
                    if needle not in haystack:
                        return False
                else:
                    return False
            elif op == "exists":
                should_exist = _coerce_bool(value)
                exists = actual is not None
                if exists != should_exist:
                    return False
            else:
                raise PolicyError(f"Unsupported operator '{op}' in rule condition")
        return True
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def _rule_matches(rule: PolicyRule, intent: Dict[str, Any]) -> bool:
    for path, expected in rule.match.items():
        actual = _resolve_path(intent, path)
        if not _evaluate_condition(actual, expected):
            return False
    return True


def _fail_mode_to_decision(mode: str) -> str:
    if mode == "fail-open":
        return "allow"
    if mode == "fail-closed":
        return "deny"
    if mode == "require-approval":
        return "require_approval"
    raise PolicyError(f"Unsupported fail mode {mode}")


class PolicyDecisionPoint:
    """Evaluate intents against a loaded policy bundle."""

    def __init__(
        self,
        loader: PolicyLoader,
        *,
        intent_filters: Optional[List[Callable[[Dict[str, Any]], Dict[str, Any]]]] = None,
    ) -> None:
        self._loader = loader
        self._intent_filters = intent_filters or []

    def current_bundle(self) -> Optional[PolicyBundle]:
        try:
            return self._loader.get_bundle()
        except FileNotFoundError:
            return None

    def evaluate(
        self,
        action: str,
        intent: Dict[str, Any],
    ) -> PolicyDecision:
        bundle = self._loader.get_bundle()
        filtered_intent = dict(intent)
        for filter_fn in self._intent_filters:
            filtered_intent = filter_fn(filtered_intent)
        intent_hash_input = {
            "version": bundle.version,
            "action": action,
            "intent": filtered_intent,
        }
        intent_hash = _sha256(_canonical_hash(intent_hash_input))
        action_cfg = bundle.actions.get(action)
        fallback_mode = bundle.defaults.fail_mode
        if action_cfg is not None:
            fallback_mode = action_cfg.fail_mode
        fallback_mode = fallback_mode or bundle.defaults.fail_mode
        fallback_mode = fallback_mode or bundle.sdk_defaults.fail_mode
        try:
            decision, rule_id, rationale, approval_spec = self._apply_rules(action_cfg, bundle, filtered_intent)
            return PolicyDecision(
                decision=decision,
                policy_version=bundle.version,
                intent_hash=intent_hash,
                bundle_hash=bundle.bundle_hash,
                rule_id=rule_id,
                rationale=rationale,
                fallback_mode=fallback_mode,
                fallback_trigger=None,
                approval_requirement=self._build_requirement(intent_hash, approval_spec),
            )
        except Exception as exc:
            fallback_decision = _fail_mode_to_decision(fallback_mode)
            rationale = f"fallback:{type(exc).__name__}"
            return PolicyDecision(
                decision=fallback_decision,
                policy_version=bundle.version,
                intent_hash=intent_hash,
                bundle_hash=bundle.bundle_hash,
                rule_id=None,
                rationale=None,
                fallback_mode=fallback_mode,
                fallback_trigger=rationale,
                approval_requirement=self._build_requirement(intent_hash, action_cfg.approval if action_cfg else None)
                if fallback_decision == "require_approval"
                else None,
            )

    def _apply_rules(
        self,
        action_cfg: Optional[PolicyAction],
        bundle: PolicyBundle,
        filtered_intent: Dict[str, Any],
    ) -> Tuple[str, Optional[str], Optional[str], Optional[PolicyApprovalSpec]]:
        if action_cfg is None:
            default_decision = bundle.defaults.decision
            approval_spec = _default_approval_spec() if default_decision == "require_approval" else None
            return (
                default_decision,
                None,
                None,
                approval_spec,
            )
        for rule in action_cfg.rules:
            if _rule_matches(rule, filtered_intent):
                return (
                    rule.decision,
                    rule.rule_id,
                    rule.rationale,
                    rule.approval if rule.decision == "require_approval" else None,
                )
        approval_spec = action_cfg.approval if action_cfg.default_decision == "require_approval" else None
        return (
            action_cfg.default_decision,
            None,
            None,
            approval_spec,
        )

    def _build_requirement(
        self,
        intent_hash: str,
        spec: Optional[PolicyApprovalSpec],
    ) -> Optional[PolicyApprovalRequirement]:
        if spec is None or not spec.requires_token:
            return None
        expires_at = _now() + _dt.timedelta(seconds=spec.expires_in)
        return PolicyApprovalRequirement(
            intent_hash=intent_hash,
            expires_at=expires_at,
            approver_roles=spec.approver_roles,
            metadata=dict(spec.metadata),
        )


@dataclass
class PendingApproval:
    action: str
    decision: PolicyDecision
    created_at: _dt.datetime
    condition: threading.Condition
    receipt: Optional[PolicyApprovalReceipt] = None
    cancelled: bool = False

    def wait(self, timeout: Optional[float] = None) -> PolicyApprovalReceipt:
        end_time = None if timeout is None else _dt.datetime.utcnow().timestamp() + timeout
        with self.condition:
            while self.receipt is None and not self.cancelled:
                if timeout is not None:
                    remaining = end_time - _dt.datetime.utcnow().timestamp()
                    if remaining <= 0:
                        break
                    self.condition.wait(timeout=remaining)
                else:
                    self.condition.wait()
            if self.cancelled:
                raise PolicyApprovalCancelled("Approval request cancelled")
            if self.receipt is None:
                raise PolicyApprovalTimeout("Approval token not received")
            return self.receipt

    def provide(self, receipt: PolicyApprovalReceipt) -> None:
        with self.condition:
            self.receipt = receipt
            self.condition.notify_all()

    def cancel(self) -> None:
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()


ApprovalNotifier = Callable[[PolicyApprovalRequirement], None]
