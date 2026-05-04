"""Execution context for Setorra sessions.

We maintain per-thread/async context for stable identifiers so that
instrumentation can attach events to the right session and planning thread.
"""

from __future__ import annotations

import contextvars
from typing import Optional


session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "setorra_session_id", default=None
)

# Planning context (optional). When planning capture is enabled, the collector
# threads a current plan_id through action events for traceability.
plan_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "setorra_plan_id", default=None
)


def get_session_id() -> Optional[str]:
    return session_id_var.get()


def set_session_id(value: Optional[str]) -> None:
    session_id_var.set(value)


def get_plan_id() -> Optional[str]:
    return plan_id_var.get()


def set_plan_id(value: Optional[str]) -> None:
    plan_id_var.set(value)
