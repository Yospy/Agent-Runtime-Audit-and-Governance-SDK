"""Runtime guardrail loading and enforcement (Setorra)."""

from __future__ import annotations

import datetime as _dt
import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .integrity import canonical_json

try:  # Optional dependency for YAML guardrail bundles.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - we validate availability when needed
    yaml = None  # type: ignore


class GuardrailError(RuntimeError):
    """Raised when a guardrail bundle is invalid or cannot be applied."""


class GuardrailViolationError(RuntimeError):
    """Raised when a guardrail denies an action at runtime."""


_SUPPORTED_EFFECTS = {"allow", "deny", "modify"}


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)


def _maybe_int(token: str) -> Optional[int]:
    try:
        return int(token)
    except ValueError:
        return None


def _resolve_path(payload: Any, path: str) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        idx = _maybe_int(part)
        if idx is not None:
            if not isinstance(current, list) or idx >= len(current):
                return None
            current = current[idx]
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _assign_path(payload: Any, path: str, value: Any) -> None:
    if not path:
        raise GuardrailError("Guardrail set path cannot be empty")
    parts = path.split(".")
    current = payload
    for part in parts[:-1]:
        idx = _maybe_int(part)
        if idx is not None:
            if not isinstance(current, list):
                raise GuardrailError(f"Cannot set index {idx} on non-list for path '{path}'")
            while len(current) <= idx:
                current.append({})
            current = current[idx]
            if not isinstance(current, (dict, list)):
                current_idx_type = type(current).__name__
                raise GuardrailError(
                    f"Intermediate path '{part}' resolved to unsupported type {current_idx_type} for path '{path}'"
                )
            continue
        if not isinstance(current, dict):
            raise GuardrailError(f"Cannot set key '{part}' on non-dict for path '{path}'")
        if part not in current or not isinstance(current[part], (dict, list)):
            current[part] = {}
        current = current[part]
    leaf = parts[-1]
    idx = _maybe_int(leaf)
    if idx is not None:
        if not isinstance(current, list):
            raise GuardrailError(f"Cannot set index {idx} on non-list for path '{path}'")
        while len(current) <= idx:
            current.append(None)
        current[idx] = value
    else:
        if not isinstance(current, dict):
            raise GuardrailError(f"Cannot set key '{leaf}' on non-dict for path '{path}'")
        current[leaf] = value


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
                should_exist = bool(value)
                exists = actual is not None
                if exists != should_exist:
                    return False
            else:
                raise GuardrailError(f"Unsupported operator '{op}' in guardrail condition")
        return True
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def _load_payload(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise GuardrailError("PyYAML is required to load YAML guardrail bundles (pip install pyyaml)")
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            if yaml is None:
                raise GuardrailError(f"Failed to parse guardrail JSON at {path}: {exc}") from exc
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise GuardrailError("Guardrail bundle must be a JSON/YAML object")
    return data


@dataclass(frozen=True)
class GuardrailRule:
    rule_id: str
    target: str
    effect: str
    match: Dict[str, Any]
    message: Optional[str]
    set_operations: Dict[str, Any]


@dataclass(frozen=True)
class GuardrailAction:
    name: str
    rules: Tuple[GuardrailRule, ...]


@dataclass(frozen=True)
class GuardrailBundle:
    version: str
    metadata: Dict[str, Any]
    actions: Dict[str, GuardrailAction]
    source_path: Optional[Path]
    bundle_hash: str
    loaded_at: _dt.datetime


@dataclass
class GuardrailEvaluation:
    action: str
    status: str
    rule_id: Optional[str]
    message: Optional[str]
    parameters: Any
    context: Dict[str, Any]
    applied_rules: Tuple[str, ...]
    modifications: Tuple[Dict[str, Any], ...]


class GuardrailLoader:
    """Load and cache a guardrail bundle from JSON or YAML."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cached_bundle: Optional[GuardrailBundle] = None
        self._mtime: Optional[float] = None

    @property
    def path(self) -> Path:
        return self._path

    def get_bundle(self) -> GuardrailBundle:
        with self._lock:
            stat = self._path.stat()
            need_reload = self._mtime is None or stat.st_mtime != self._mtime or self._cached_bundle is None
            if not need_reload and self._cached_bundle is not None:
                return self._cached_bundle
            payload = _load_payload(self._path)
            bundle = _validate_bundle(payload, self._path)
            self._cached_bundle = bundle
            self._mtime = stat.st_mtime
            return bundle


class GuardrailEnforcer:
    """Evaluate runtime intents against guardrail rules."""

    def __init__(self, loader: GuardrailLoader) -> None:
        self._loader = loader

    def current_bundle(self) -> Optional[GuardrailBundle]:
        try:
            return self._loader.get_bundle()
        except FileNotFoundError:
            return None

    def evaluate(
        self,
        action: str,
        *,
        actor: Dict[str, Any],
        parameters: Any,
        context: Dict[str, Any],
    ) -> GuardrailEvaluation:
        bundle = self._loader.get_bundle()
        action_cfg = bundle.actions.get(action)
        intent = {
            "actor": deepcopy(actor),
            "parameters": deepcopy(parameters),
            "context": deepcopy(context),
        }
        applied: List[str] = []
        modifications: List[Dict[str, Any]] = []
        status = "allow"
        rule_id = None
        message = None
        if action_cfg is None:
            return GuardrailEvaluation(
                action=action,
                status=status,
                rule_id=None,
                message=None,
                parameters=intent["parameters"],
                context=intent["context"],
                applied_rules=tuple(applied),
                modifications=tuple(modifications),
            )
        for rule in action_cfg.rules:
            if not _rule_matches(rule, intent):
                continue
            applied.append(rule.rule_id)
            if rule.effect == "deny":
                status = "deny"
                rule_id = rule.rule_id
                message = rule.message or "Guardrail denied the action"
                break
            if rule.effect == "modify":
                status = "modified"
                for path, value in rule.set_operations.items():
                    _assign_path(intent, path, value)
                    modifications.append({"rule_id": rule.rule_id, "path": path, "value": value})
                rule_id = rule.rule_id
                message = rule.message
            elif rule.effect == "allow":
                status = "allow"
                rule_id = rule.rule_id
                message = rule.message
        return GuardrailEvaluation(
            action=action,
            status=status,
            rule_id=rule_id,
            message=message,
            parameters=intent["parameters"],
            context=intent["context"],
            applied_rules=tuple(applied),
            modifications=tuple(modifications),
        )


def _rule_matches(rule: GuardrailRule, intent: Dict[str, Any]) -> bool:
    for path, expected in rule.match.items():
        actual = _resolve_path(intent, path)
        if not _evaluate_condition(actual, expected):
            return False
    return True


def _validate_bundle(payload: Dict[str, Any], source: Path) -> GuardrailBundle:
    version_raw = payload.get("version")
    if not isinstance(version_raw, str) or not version_raw.strip():
        raise GuardrailError("Guardrail bundle must include non-empty 'version'")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise GuardrailError("Guardrail metadata must be an object")
    actions_payload = payload.get("actions")
    if actions_payload is None:
        actions_payload = {}
    if not isinstance(actions_payload, dict):
        raise GuardrailError("Guardrail 'actions' must be an object")
    actions: Dict[str, GuardrailAction] = {}
    for action_name, action_config in actions_payload.items():
        if not isinstance(action_name, str) or not action_name.strip():
            raise GuardrailError("Guardrail action names must be non-empty strings")
        if not isinstance(action_config, dict):
            raise GuardrailError(f"Guardrail action '{action_name}' must be an object")
        rules_payload = action_config.get("rules")
        if rules_payload is None:
            rules_payload = []
        if not isinstance(rules_payload, list):
            raise GuardrailError(f"Guardrail action '{action_name}' rules must be a list")
        rules: List[GuardrailRule] = []
        for raw in rules_payload:
            if not isinstance(raw, dict):
                raise GuardrailError(f"Guardrail rule in action '{action_name}' must be an object")
            rule_id = str(raw.get("id") or "").strip()
            if not rule_id:
                raise GuardrailError(f"Guardrail rule in action '{action_name}' missing 'id'")
            effect = str(raw.get("effect", "deny")).strip().lower()
            if effect not in _SUPPORTED_EFFECTS:
                raise GuardrailError(
                    f"Guardrail rule '{rule_id}' uses unsupported effect '{effect}'. Expected one of {_SUPPORTED_EFFECTS}."
                )
            match_block = raw.get("match") or {}
            if not isinstance(match_block, dict):
                raise GuardrailError(f"Guardrail rule '{rule_id}' match must be an object")
            set_block = raw.get("set") or {}
            if not isinstance(set_block, dict):
                raise GuardrailError(f"Guardrail rule '{rule_id}' set must be an object when provided")
            rules.append(
                GuardrailRule(
                    rule_id=rule_id,
                    target=action_name,
                    effect=effect,
                    match=match_block,
                    message=raw.get("message") if isinstance(raw.get("message"), str) else None,
                    set_operations=set_block,
                )
            )
        actions[action_name] = GuardrailAction(name=action_name, rules=tuple(rules))
    bundle_hash = _sha256(canonical_json(payload))
    return GuardrailBundle(
        version=version_raw,
        metadata=metadata,
        actions=actions,
        source_path=source,
        bundle_hash=bundle_hash,
        loaded_at=_now(),
    )


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
