"""Remote integration utilities for Setorra (handshake only).

This module implements a minimal, industry-standard handshake client used to
link an SDK instance to a backend control plane using only an API key.

Design goals
- Small, self-contained adapter (no new third-party deps)
- TLS-ready (urllib uses system CA by default for https URLs)
- Short timeouts and clear error mapping
- No secret persistence or verbose logging

Usage (SDK internal):

    from .remote import HandshakeClient, HandshakeError

    client = HandshakeClient(base_url=os.getenv("SETORRA_BACKEND", ""),
                             user_agent=f"SetorraSDK/{sdk_version}")
    resp = client.handshake(api_key)
    # resp = { org_id, key_id, session_token, expires_in, expires_at }

Only handshake is implemented in this milestone. Ingest and token refresh can
be added as separate adapters without touching collector logic.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass(frozen=True)
class HandshakeResponse:
    """Parsed handshake response from the backend control plane.

    Fields
    - org_id: Required tenant identifier provided by backend.
    - key_id: Stable audit-safe identifier of the API key (no raw key).
    - session_token: Short-lived bearer token for subsequent remote calls.
    - expires_in: Token TTL in seconds.
    - expires_at: Absolute expiry timestamp (UTC).
    - org_name: Optional human-readable organization name.
    """

    org_id: str
    key_id: str
    session_token: str
    expires_in: int
    expires_at: datetime
    org_name: Optional[str] = None


class HandshakeError(Exception):
    """Base error for handshake failures (non-transient)."""


class InvalidApiKey(HandshakeError):
    """Provided API key was not recognized by the backend (HTTP 401)."""


class RevokedKey(HandshakeError):
    """Provided API key is revoked or disabled (HTTP 403)."""


class TransientNetworkError(HandshakeError):
    """Temporary network/transport error; callers may choose to retry."""


class HandshakeClient:
    """Minimal client for control-plane handshake.

    Parameters
    - base_url: Backend base URL (e.g., "https://api.setorra.com").
                If empty/None, handshake is considered disabled.
    - user_agent: Value for the User-Agent header.
    - connect_timeout_s: Socket connect timeout seconds.
    - read_timeout_s: Response read timeout seconds.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str],
        user_agent: Optional[str] = None,
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 5.0,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._user_agent = user_agent or "SetorraSDK/unknown"
        # urllib has a single timeout param; we approximate by using the
        # larger of connect/read to avoid premature termination.
        self._timeout = max(connect_timeout_s, read_timeout_s)

    def enabled(self) -> bool:
        return bool(self._base_url)

    def handshake(self, api_key: str) -> HandshakeResponse:
        if not self.enabled():
            raise HandshakeError("Handshake disabled: backend base_url is empty")
        if not api_key:
            raise HandshakeError("Handshake requires a non-empty API key")

        url = f"{self._base_url}/v1/auth/handshake"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }

        req = urllib.request.Request(url=url, headers=headers, method="POST")
        # Minimal body; backend does not require it in this milestone.
        data = json.dumps({}).encode("utf-8")

        try:
            with urllib.request.urlopen(req, data=data, timeout=self._timeout) as resp:  # nosec - relies on system CA for https
                body = resp.read()
                payload = json.loads(body.decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:  # HTTP status codes
            if e.code == 401:
                raise InvalidApiKey("invalid_api_key") from None
            if e.code == 403:
                raise RevokedKey("revoked_key") from None
            # Surface other HTTP errors generically
            raise HandshakeError(f"handshake http_error status={e.code}") from None
        except urllib.error.URLError as e:
            # Network/DNS/TLS errors → transient
            raise TransientNetworkError(f"handshake network_error: {e.reason}") from None
        except Exception as e:  # noqa: BLE001
            raise HandshakeError(f"handshake unexpected_error: {e}") from None

        try:
            org_id = str(payload["org_id"]).strip()
            key_id = str(payload["key_id"]).strip()
            session_token = str(payload["session_token"]).strip()
            expires_in_raw = int(payload.get("expires_in", 0))
            if not org_id or not key_id or not session_token or expires_in_raw <= 0:
                raise KeyError("missing required fields in handshake response")
            org_name = payload.get("org_name")
            if org_name is not None:
                org_name = str(org_name).strip() or None
        except Exception as e:  # noqa: BLE001
            raise HandshakeError(f"invalid handshake payload: {e}") from None

        now = int(time.time())
        expires_at = datetime.fromtimestamp(now + expires_in_raw, tz=timezone.utc)
        return HandshakeResponse(
            org_id=org_id,
            key_id=key_id,
            session_token=session_token,
            expires_in=expires_in_raw,
            expires_at=expires_at,
            org_name=org_name,
        )
