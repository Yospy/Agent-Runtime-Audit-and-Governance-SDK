"""Action lifecycle completeness checks for evidence replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


TERMINAL_EVENTS = {
    "action.allowed",
    "action.blocked",
    "action.needs_approval",
    "action.needs_more_context",
    "action.succeeded",
    "action.failed",
}


@dataclass(frozen=True)
class ActionLifecycleReport:
    ok: bool
    actions_checked: int
    errors: List[str]


def verify_action_lifecycle(events: Iterable[Dict[str, Any]]) -> ActionLifecycleReport:
    actions: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    for event in events:
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith("action."):
            continue
        params = event.get("parameters_redacted")
        if not isinstance(params, dict):
            params = {}
        execution = event.get("execution")
        if not isinstance(execution, dict):
            execution = {}
        if not params:
            preview = execution.get("parameters_preview")
            if isinstance(preview, dict):
                params = preview
        action_id = params.get("action_id") or execution.get("action_id")
        if not action_id:
            errors.append(f"{event_type} missing action_id")
            continue
        action = actions.setdefault(str(action_id), {"events": set(), "executed": False, "terminal": False})
        action["events"].add(event_type)
        if event_type == "action.executed":
            action["executed"] = True
        if event_type in TERMINAL_EVENTS:
            action["terminal"] = True

    for action_id, state in sorted(actions.items()):
        event_types = state["events"]
        if "action.requested" not in event_types:
            errors.append(f"{action_id} missing action.requested")
        if not state["terminal"]:
            errors.append(f"{action_id} missing terminal action event")
        if state["executed"] and not ({"action.succeeded", "action.failed"} & event_types):
            errors.append(f"{action_id} executed without action.succeeded/action.failed")

    return ActionLifecycleReport(
        ok=not errors,
        actions_checked=len(actions),
        errors=errors,
    )
