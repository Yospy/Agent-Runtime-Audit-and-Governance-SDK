"""Framework-agnostic integration surface (Setorra).

Expose a single `invoke` wrapper that:
- Starts a session (allocates session_id; binds metadata)
- Attempts to attach instrumentation (best-effort; fallback safe)
- Invokes the agent/executor callable (supports common patterns)
- On exception, emits agent.error and finalizes artifacts
- On success, emits agent.finished and finalizes artifacts

Instrumentation note:
MVP uses fallback-only capture (agent.* events) unless users manually call
`collector.record_action` in tool boundaries or LLM wrappers. Future versions
will add framework-specific adapters while preserving the same API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Callable
import os

from .collector import SetorraCollector, setorra_collector
from .guardrails import GuardrailViolationError
from .policy import PolicyApprovalCancelled, PolicyApprovalTimeout, PolicyDeniedError


@dataclass
class InvokeResult:
    session_id: str
    output: Any


def _sanitize_for_guardrails(value: Any, *, _depth: int = 0, _max_depth: int = 5) -> Any:
    """Convert complex objects into guardrail-friendly structures."""
    if _depth > _max_depth:
        return "<depth-limit>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _sanitize_for_guardrails(item, _depth=_depth + 1, _max_depth=_max_depth)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_for_guardrails(item, _depth=_depth + 1, _max_depth=_max_depth)
            for item in value
        ]
    if hasattr(value, "__dict__") and not callable(value):
        try:
            return _sanitize_for_guardrails(value.__dict__, _depth=_depth + 1, _max_depth=_max_depth)
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return f"<non-serializable:{type(value).__name__}>"


def _maybe_int(segment: str) -> Optional[int]:
    try:
        return int(segment)
    except (TypeError, ValueError):
        return None


def _assign_guardrail_path(target: Any, path: str, value: Any) -> None:
    parts = [p for p in path.split(".") if p]
    if not parts:
        return
    current = target
    for segment in parts[:-1]:
        idx = _maybe_int(segment)
        if idx is None:
            if not isinstance(current, dict):
                return
            if segment not in current or current[segment] is None:
                current[segment] = {}
            current = current[segment]
        else:
            if not isinstance(current, list):
                return
            while len(current) <= idx:
                current.append({})
            current = current[idx]
    leaf = parts[-1]
    leaf_idx = _maybe_int(leaf)
    if leaf_idx is None:
        if isinstance(current, dict):
            current[leaf] = value
    else:
        if isinstance(current, list):
            while len(current) <= leaf_idx:
                current.append(None)
            current[leaf_idx] = value


def invoke(
    collector: SetorraCollector,
    agent_or_executor: Any,
    inputs: Any = None,
    meta: Optional[dict] = None,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
    policy_version: str = "0",
    capability_scope: Optional[Any] = None,
    environment: Optional[dict] = None,
    errors_as_output: Optional[bool] = None,
    **invoke_kwargs: Any,
) -> InvokeResult:
    """Run an agent/executor under Setorra capture and return the session + output.

    This function is synchronous. If the agent is async, pass a wrapper that
    runs the coroutine (e.g., via `asyncio.run`).
    """
    inferred_user_prompt = user_prompt or _guess_user_prompt(inputs)

    session_id = collector.start_session(
        meta=meta,
        policy_version=policy_version,
        capability_scope=capability_scope,
        environment=environment,
        system_prompt=system_prompt,
        user_prompt=inferred_user_prompt,
        model_id=model_id,
    )
    action_context = {
        "meta": meta or {},
        "model_id": model_id,
        "environment": environment or {},
    }
    parameters_context = {
        "inputs": inputs,
        "kwargs": invoke_kwargs,
    }
    # Resolve errors-as-output behavior (arg > env > default False)
    env_eao = os.getenv("SETORRA_ERRORS_AS_OUTPUT", "").strip().lower()
    errors_as_output_enabled = (
        errors_as_output
        if errors_as_output is not None
        else env_eao in {"1", "true", "yes", "on"}
    )

    try:
        guardrail_parameters = _sanitize_for_guardrails(parameters_context)
        guardrail_context = _sanitize_for_guardrails(action_context)
        guardrail_result = collector.apply_guardrails(
            "agent.invoke",
            actor={"type": "agent", "name": collector.agent_name, "version": collector.agent_version},
            parameters=guardrail_parameters,
            context=guardrail_context,
        )
    except GuardrailViolationError as exc:
        collector.record_outcome(
            status="failure",
            error_type="GuardrailViolation",
            error_message=str(exc),
        )
        collector.end_session(final_output={"status": "guardrail_denied", "reason": str(exc)}, model_id=model_id)
        if errors_as_output_enabled:
            return InvokeResult(
                session_id=session_id,
                output={
                    "output": "Blocked by guardrails.",
                    "status": "guardrail_denied",
                    "public_message": "Blocked by guardrails.",
                    "reason_code": "GUARDRAIL_DENY",
                },
            )
        raise
    else:
        if guardrail_result.modifications:
            for modification in guardrail_result.modifications:
                path = modification.get("path")
                if not isinstance(path, str):
                    continue
                value = modification.get("value")
                if path.startswith("parameters."):
                    _assign_guardrail_path(parameters_context, path[len("parameters.") :], value)
                elif path.startswith("context."):
                    _assign_guardrail_path(action_context, path[len("context.") :], value)
        if isinstance(parameters_context, dict):
            inputs_candidate = parameters_context.get("inputs")
            if inputs_candidate is not None:
                inputs = inputs_candidate
            kwargs_candidate = parameters_context.get("kwargs")
            if isinstance(kwargs_candidate, dict):
                invoke_kwargs = kwargs_candidate
    try:
        collector.enforce_policy(
            "agent.invoke",
            actor={"type": "agent", "name": collector.agent_name, "version": collector.agent_version},
            parameters=parameters_context,
            context=action_context,
        )
    except PolicyDeniedError as exc:
        collector.record_action(
            "agent.policy_denied",
            status="denied",
            error_type="PolicyDenied",
            error_preview=str(exc),
        )
        collector.record_outcome(
            status="failure",
            error_type="PolicyDenied",
            error_message=str(exc),
        )
        collector.end_session(final_output={"status": "denied", "reason": str(exc)}, model_id=model_id)
        if errors_as_output_enabled:
            return InvokeResult(
                session_id=session_id,
                output={
                    "output": "Blocked by policy.",
                    "status": "policy_denied",
                    "public_message": "Blocked by policy.",
                    "reason_code": "POLICY_DENY",
                },
            )
        raise
    except PolicyApprovalTimeout as exc:
        collector.record_action(
            "agent.policy_timeout",
            status="error",
            error_type="PolicyApprovalTimeout",
            error_preview=str(exc),
        )
        collector.record_outcome(
            status="failure",
            error_type="PolicyApprovalTimeout",
            error_message=str(exc),
        )
        collector.end_session(final_output={"status": "approval_timeout", "reason": str(exc)}, model_id=model_id)
        if errors_as_output_enabled:
            return InvokeResult(
                session_id=session_id,
                output={
                    "output": "Approval required but timed out.",
                    "status": "approval_timeout",
                    "public_message": "Approval required but timed out.",
                    "reason_code": "POLICY_TIMEOUT",
                },
            )
        raise
    except PolicyApprovalCancelled as exc:
        collector.record_action(
            "agent.policy_cancelled",
            status="error",
            error_type="PolicyApprovalCancelled",
            error_preview=str(exc),
        )
        collector.record_outcome(
            status="failure",
            error_type="PolicyApprovalCancelled",
            error_message=str(exc),
        )
        collector.end_session(final_output={"status": "approval_cancelled", "reason": str(exc)}, model_id=model_id)
        if errors_as_output_enabled:
            return InvokeResult(
                session_id=session_id,
                output={
                    "output": "Approval was cancelled.",
                    "status": "approval_cancelled",
                    "public_message": "Approval was cancelled.",
                    "reason_code": "POLICY_CANCELLED",
                },
            )
        raise
    try:
        output = _invoke_call(agent_or_executor, inputs, **invoke_kwargs)
        collector.record_outcome(status="success")
        collector.end_session(
            final_output=output,
            model_id=model_id,
        )
        return InvokeResult(session_id=session_id, output=output)
    except Exception as exc:  # noqa: BLE001 - capture and finalize
        # Record the error and finalize with failure state
        collector.record_action(
            "agent.exception",
            parameters={"type": type(exc).__name__},
            status="error",
            error_type=type(exc).__name__,
            error_preview=str(exc),
        )
        collector.record_outcome(
            status="failure",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        collector.end_session(final_output=str(exc), model_id=model_id)
        raise


def _invoke_call(agent_or_executor: Any, inputs: Any, **kwargs: Any) -> Any:
    # Common invocation patterns across frameworks
    if hasattr(agent_or_executor, "invoke") and callable(agent_or_executor.invoke):
        try:
            return agent_or_executor.invoke(inputs, **kwargs)
        except TypeError:
            return agent_or_executor.invoke(inputs)
    if hasattr(agent_or_executor, "run") and callable(agent_or_executor.run):
        try:
            return agent_or_executor.run(inputs, **kwargs)
        except TypeError:
            return agent_or_executor.run(inputs)
    if callable(agent_or_executor):
        if inputs is None:
            return agent_or_executor()
        return agent_or_executor(inputs)
    raise TypeError("agent_or_executor is not invokable: expected .invoke/.run or callable")


def _guess_user_prompt(inputs: Any) -> Optional[str]:
    if isinstance(inputs, str):
        return inputs
    if isinstance(inputs, dict):
        for key in ("input", "prompt", "query", "question"):
            if key in inputs and isinstance(inputs[key], str):
                return inputs[key]
    return None


###############################################################################
# NEW INTEGRATION (Runner pattern)
# --------------------------------
# This section adds a thin Runner facade and a `runner(...)` factory that
# standardize the developer experience to three steps:
#   1) import runner
#   2) initialize once with agent + policy + guardrails + environment
#   3) call run.invoke(agent_or_executor, inputs=...)
#
# IMPORTANT: Behavior is unchanged. The Runner delegates to the legacy
# `invoke(...)` function above and constructs the collector via the existing
# `setorra_collector(...)` factory. Guardrail/policy enforcement, redaction,
# evidence emission, integrity chaining, storage, and optional handshake stay
# exactly the same.
#
# Scope: Changes are limited to this file only, per request.
###############################################################################

class Runner:
    """Standardized integration facade (no behavior change).

    - Creates a Setorra collector using the existing factory.
    - Optionally seeds environment for policy/guardrail path and environment
      name just for collector construction, then restores env.
    - Delegates all execution to the legacy `invoke(...)` wrapper to preserve
      guardrail/policy enforcement and evidence semantics identically.
    """

    def __init__(
        self,
        agent_name: str,
        agent_version: str,
        *,
        policy: Optional[str] = None,
        guardrails: Optional[str] = None,
        environment: Optional[str] = None,
        # Default to True for runner-based integrations to prefer
        # privacy-safe error surfaces in user-facing apps.
        errors_as_output: bool = True,
    ) -> None:
        # Temporarily set env vars recognized by the collector factory to
        # configure policy, guardrails, and environment. Restore afterward to
        # avoid persistent global side-effects.
        to_set: list[tuple[str, str]] = []
        if policy:
            to_set.append(("SETORRA_POLICY_PATH", str(policy)))
        if guardrails:
            to_set.append(("SETORRA_GUARDRAIL_PATH", str(guardrails)))
        if environment:
            to_set.append(("SETORRA_ENVIRONMENT", str(environment)))

        prev: dict[str, Optional[str]] = {}
        try:
            for key, val in to_set:
                prev[key] = os.environ.get(key)
                os.environ[key] = val
            # Construct via existing factory to preserve handshake/loaders.
            self._collector: SetorraCollector = setorra_collector(agent_name, agent_version)
        finally:
            for key, _ in to_set:
                old = prev.get(key)
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

        self._errors_as_output_default = bool(errors_as_output)

    def invoke(self, agent_or_executor: Any, inputs: Any = None, **kwargs: Any) -> InvokeResult:
        """Run the agent/executor with preserved enforcement/evidence semantics.

        Notes on framework support:
        - LangChain/LangGraph: pass the Runnable/graph (uses .invoke when present).
        - Custom agents: objects with .invoke/.run or plain callables are supported.
        - OpenAI/Anthropic: wrap client calls in a callable/adapter and pass here.
        """
        if "errors_as_output" not in kwargs:
            kwargs["errors_as_output"] = self._errors_as_output_default
        return invoke(self._collector, agent_or_executor, inputs, **kwargs)

    def invoker(self) -> Callable[[Any, Any], InvokeResult]:
        """Return a callable sugar that mirrors `.invoke(...)`.

        Allows the 3-line integration pattern:
            run = runner(...)
            invoke_agent = run.invoker()
            result = invoke_agent(agent, inputs)
        """

        def _call(agent_or_executor: Any, inputs: Any = None, **kwargs: Any) -> InvokeResult:
            return self.invoke(agent_or_executor, inputs, **kwargs)

        return _call

    def close(self) -> None:
        """No-op placeholder for future resource management."""
        return None

    @property
    def collector(self) -> SetorraCollector:
        """Expose the underlying collector for advanced use-cases.

        This enables framework callbacks and tool-level policy/guardrail
        enforcement to continue working without changing semantics.
        """
        return self._collector


def runner(
    agent_name: str,
    agent_version: str,
    *,
    policy: Optional[str] = None,
    guardrails: Optional[str] = None,
    environment: Optional[str] = None,
    # Default to True for runner-based integrations; callers can override.
    errors_as_output: bool = True,
) -> Runner:
    """Factory for the standardized Runner facade.

    Usage:
        run = runner("my-agent", "1.0.0",
                     policy="policy.json",
                     guardrails="guardrails.yaml",
                     environment="prod",
                     errors_as_output=True)

        res = run.invoke(agent_or_chain, inputs={"input": "..."})

    Behavior is identical to the legacy path because execution delegates to
    `invoke(...)` above and the collector is built via `setorra_collector(...)`.
    """
    return Runner(
        agent_name,
        agent_version,
        policy=policy,
        guardrails=guardrails,
        environment=environment,
        errors_as_output=errors_as_output,
    )
