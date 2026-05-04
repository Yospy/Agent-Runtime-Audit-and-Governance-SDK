"""Setorra SDK public API (primary implementation).

Behavior is identical to the previous Tisac SDK; Setorra is now the source of
truth and exports the same surfaces.
"""

from .collector import SetorraCollector, setorra_collector
from .guardrails import GuardrailEnforcer, GuardrailError, GuardrailLoader, GuardrailViolationError
from .policy import (
    PolicyApprovalCancelled,
    PolicyApprovalTimeout,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyDeniedError,
    PolicyError,
    PolicyLoader,
)
from .integration import invoke
# NEW standard integration facade (Runner pattern). Behavior unchanged; delegates
# to legacy invoke under the hood. Exposed at top level for one-line import.
from .integration import runner, Runner
from .firewall import ActionFirewall, ActionRequest, ActionDecision, ActionResult, firewall
from .approvals import ApprovalRecord, ApprovalRuntimeError, FileApprovalStore, InMemoryApprovalStore
from .connectors import (
    ConnectorError,
    ConnectorRequest,
    ConnectorResult,
    FakeConnector,
    HttpConnector,
    SlackApprovalConnector,
    StripeRefundConnector,
)
from .adapters.langchain import SetorraLangChainCallback
from .multiagent import MessageBus

# Optional adapters (lightweight shims for multi-agent capture). These modules
# are safe to import even if the target frameworks are not installed. They are
# no-ops unless explicitly used by the application.
try:  # pragma: no cover - optional
    from .adapters.langgraph import SetorraLangGraphCallback  # type: ignore
except Exception:  # pragma: no cover - absent dependency
    SetorraLangGraphCallback = None  # type: ignore

try:  # pragma: no cover - optional
    from .adapters.openai_assistants import SetorraOpenAIAssistants  # type: ignore
except Exception:  # pragma: no cover - absent dependency
    SetorraOpenAIAssistants = None  # type: ignore

# Canonical Setorra names (aliases)
# Canonical Setorra names are exported above

__all__ = [
    "SetorraCollector",
    "setorra_collector",
    "invoke",
    "runner",
    "Runner",
    "ActionFirewall",
    "ActionRequest",
    "ActionDecision",
    "ActionResult",
    "firewall",
    "ApprovalRecord",
    "ApprovalRuntimeError",
    "FileApprovalStore",
    "InMemoryApprovalStore",
    "ConnectorError",
    "ConnectorRequest",
    "ConnectorResult",
    "FakeConnector",
    "HttpConnector",
    "SlackApprovalConnector",
    "StripeRefundConnector",
    "PolicyLoader",
    "PolicyDecisionPoint",
    "PolicyDecision",
    "PolicyError",
    "PolicyDeniedError",
    "PolicyApprovalTimeout",
    "PolicyApprovalCancelled",
    "GuardrailLoader",
    "GuardrailEnforcer",
    "GuardrailError",
    "GuardrailViolationError",
    "SetorraLangChainCallback",
    "SetorraLangGraphCallback",
    "SetorraOpenAIAssistants",
    "MessageBus",
]
