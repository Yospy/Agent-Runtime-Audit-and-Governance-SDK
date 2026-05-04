"""Connector foundations for action-firewall execution."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol

from .firewall import ActionRequest


class ConnectorError(RuntimeError):
    """Raised when a connector cannot execute an allowed action."""


@dataclass(frozen=True)
class ConnectorRequest:
    action_id: str
    action: str
    system: str
    payload: Any
    context: Dict[str, Any]
    idempotency_key: Optional[str] = None

    @classmethod
    def from_action_request(cls, action_id: str, request: ActionRequest) -> "ConnectorRequest":
        return cls(
            action_id=action_id,
            action=request.action,
            system=request.system,
            payload=request.payload,
            context=dict(request.context),
            idempotency_key=request.idempotency_key,
        )


@dataclass(frozen=True)
class ConnectorResult:
    status: str
    external_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None


class Connector(Protocol):
    def execute(self, request: ConnectorRequest) -> ConnectorResult:
        ...


class FakeConnector:
    """Deterministic connector for tests and local demos."""

    def __init__(self, result: Optional[ConnectorResult] = None) -> None:
        self.calls: list[ConnectorRequest] = []
        self._result = result or ConnectorResult(status="ok", external_id="fake_1")

    def execute(self, request: ConnectorRequest) -> ConnectorResult:
        self.calls.append(request)
        return self._result


class HttpConnector:
    """Allowlisted HTTP connector.

    Credentials stay inside the connector configuration. They are used only in
    outbound headers and are never returned in ConnectorResult payloads.
    """

    def __init__(self, routes: Mapping[str, Mapping[str, Any]], *, headers: Optional[Mapping[str, str]] = None) -> None:
        self._routes = {str(action): dict(config) for action, config in routes.items()}
        self._headers = dict(headers or {})

    def execute(self, request: ConnectorRequest) -> ConnectorResult:
        route = self._routes.get(request.action)
        if route is None:
            raise ConnectorError(f"Action is not allowlisted for HTTP connector: {request.action}")
        method = str(route.get("method", "POST")).upper()
        url = str(route.get("url") or "")
        if not url:
            raise ConnectorError("HTTP connector route missing url")
        body = json.dumps(request.payload).encode("utf-8")
        headers = {"content-type": "application/json", **self._headers}
        if request.idempotency_key:
            headers.setdefault("idempotency-key", request.idempotency_key)
        http_request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(http_request, timeout=float(route.get("timeout", 10))) as response:
            raw = response.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"body": raw}
        external_id = payload.get("id") if isinstance(payload, dict) else None
        return ConnectorResult(
            status="ok",
            external_id=str(external_id) if external_id else None,
            payload=payload if isinstance(payload, dict) else {},
            idempotency_key=request.idempotency_key,
        )


class StripeRefundConnector:
    """Stripe refund connector stub.

    This intentionally does not call Stripe. It defines the execution contract
    and idempotency behavior; real HTTP execution should be supplied by the
    gateway/control plane where credentials are owned by Setorra.
    """

    def __init__(self, *, api_key: str, mode: str = "stub") -> None:
        self._api_key = api_key
        self._mode = mode
        self.calls: list[ConnectorRequest] = []

    def execute(self, request: ConnectorRequest) -> ConnectorResult:
        if request.action != "refund.issue":
            raise ConnectorError("StripeRefundConnector only supports refund.issue")
        self.calls.append(request)
        amount = request.payload.get("amount") if isinstance(request.payload, dict) else None
        return ConnectorResult(
            status="ok",
            external_id=f"re_{request.action_id[-12:]}",
            payload={"object": "refund", "amount": amount, "mode": self._mode},
            idempotency_key=request.idempotency_key,
        )


class SlackApprovalConnector:
    """Slack approval connector stub for later interactive approvals."""

    def __init__(self, *, bot_token: str, channel: str) -> None:
        self._bot_token = bot_token
        self._channel = channel
        self.requests: list[Dict[str, Any]] = []

    def request_approval(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        safe_payload = {
            "channel": self._channel,
            "action_id": payload.get("action_id"),
            "intent_hash": payload.get("intent_hash"),
            "status": "queued",
        }
        self.requests.append(safe_payload)
        return safe_payload
