"""Backend ingest client for Setorra (minimal, production-safe).

This module provides a tiny HTTP poster used by the collector to deliver
redacted artifacts to the backend control plane. It does not modify policy
enforcement or evidence structures — it only handles transmission.

Endpoints (relative to base URL; prefix is configurable)
- Default: /ingest/evidence|output|manifest
- Configurable via SETORRA_INGEST_PREFIX (e.g., /v1/ingest/* if backend exposes aliases)

Headers
- Authorization: Bearer <session_token>
- X-Setorra-Org: <org_id> (when available)
- X-Setorra-Agent: <agent_name>/<agent_version>
- Idempotency-Key: <session_id> (bare ULID; manifest uses context.session_id)

Behavior
- Short timeouts; 1 retry on 429/5xx with small jitter. Never raises.
- Returns a concise metrics dict for logging by the caller.
"""

from __future__ import annotations

import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class PostResult:
    part: str
    status: int
    ms: float
    bytes: int


def _post(
    *,
    base_url: str,
    token: str,
    org_id: Optional[str],
    agent_label: str,
    idempotency_key: str,
    endpoint: str,
    body: bytes,
    content_type: str,
    timeout_s: float,
    part: str,
) -> PostResult:
    url = f"{base_url.rstrip('/')}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": "SetorraSDK/ingest-1",
        "X-Setorra-Agent": agent_label,
        "Idempotency-Key": idempotency_key,
    }
    if org_id:
        headers["X-Setorra-Org"] = org_id

    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    t0 = time.perf_counter()
    code = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - system CA
            try:
                _ = resp.read(0)
            except Exception:
                pass
            code = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
    except Exception:
        code = 0
    ms = (time.perf_counter() - t0) * 1000.0
    return PostResult(part=part, status=code, ms=ms, bytes=len(body))


def deliver_artifacts(
    *,
    base_url: str,
    session_token: str,
    org_id: Optional[str],
    agent_label: str,
    session_id: str,
    evidence_bytes: bytes,
    output_bytes: bytes,
    manifest_bytes: bytes,
    timeout_s: float = 2.5,
) -> Dict[str, PostResult]:
    """Send evidence → output → manifest to backend. Never raises.

    Returns a map of part → PostResult for concise logging.
    """
    results: Dict[str, PostResult] = {}
    debug_ingest = bool(os.getenv("SETORRA_DEBUG_INGEST"))

    ingest_prefix = os.getenv("SETORRA_INGEST_PREFIX", "/ingest").strip() or "/ingest"
    if not ingest_prefix.startswith("/"):
        ingest_prefix = "/" + ingest_prefix
    ingest_prefix = ingest_prefix.rstrip("/")

    # Evidence first
    ev_endpoint = f"{ingest_prefix}/evidence"
    if debug_ingest:
        print(
            f"[debug][ingest.post] part=evidence url={base_url.rstrip('/')}{ev_endpoint} "
            f"bytes={len(evidence_bytes)} timeout_ms={int(timeout_s*1000)} "
            f"idempotency_key={session_id} token_present={bool(session_token)}"
        )
    results["evidence"] = _post(
        base_url=base_url,
        token=session_token,
        org_id=org_id,
        agent_label=agent_label,
        idempotency_key=session_id,
        endpoint=ev_endpoint,
        body=evidence_bytes,
        content_type="application/x-ndjson",
        timeout_s=timeout_s,
        part="evidence",
    )
    if debug_ingest:
        print(f"[debug][ingest.result] part=evidence status={results['evidence'].status} ms={results['evidence'].ms:.1f}")

    # If evidence failed transiently, retry once with small jitter
    if results["evidence"].status in (429, 500, 502, 503, 504, 0):
        time.sleep(0.05 + random.random() * 0.1)
        results["evidence"] = _post(
            base_url=base_url,
            token=session_token,
            org_id=org_id,
            agent_label=agent_label,
            # Idempotency-Key must be the bare session_id (no suffixes)
            idempotency_key=session_id,
            endpoint=f"{ingest_prefix}/evidence",
            body=evidence_bytes,
            content_type="application/x-ndjson",
            timeout_s=timeout_s,
            part="evidence",
        )
        if debug_ingest:
            print(f"[debug][ingest.result.retry] part=evidence status={results['evidence'].status} ms={results['evidence'].ms:.1f}")

    # Output next (only try if evidence accepted)
    if 200 <= results["evidence"].status < 300:
        out_endpoint = f"{ingest_prefix}/output"
        if debug_ingest:
            print(
                f"[debug][ingest.post] part=output url={base_url.rstrip('/')}{out_endpoint} "
                f"bytes={len(output_bytes)} timeout_ms={int(timeout_s*1000)} "
                f"idempotency_key={session_id} token_present={bool(session_token)}"
            )
        results["output"] = _post(
            base_url=base_url,
            token=session_token,
            org_id=org_id,
            agent_label=agent_label,
            # Idempotency-Key must be the bare session_id (no suffixes)
            idempotency_key=session_id,
            endpoint=out_endpoint,
            body=output_bytes,
            content_type="application/json",
            timeout_s=timeout_s,
            part="output",
        )
        if debug_ingest:
            print(f"[debug][ingest.result] part=output status={results['output'].status} ms={results['output'].ms:.1f}")
    else:
        results["output"] = PostResult(part="output", status=0, ms=0.0, bytes=len(output_bytes))

    # Manifest last (only try if both prior accepted)
    if 200 <= results["evidence"].status < 300 and 200 <= results["output"].status < 300:
        man_endpoint = f"{ingest_prefix}/manifest"
        if debug_ingest:
            print(
                f"[debug][ingest.post] part=manifest url={base_url.rstrip('/')}{man_endpoint} "
                f"bytes={len(manifest_bytes)} timeout_ms={int(timeout_s*1000)} "
                f"idempotency_key={session_id} token_present={bool(session_token)}"
            )
        results["manifest"] = _post(
            base_url=base_url,
            token=session_token,
            org_id=org_id,
            agent_label=agent_label,
            # Manifest Idempotency-Key uses the same session_id (context.session_id authoritative)
            idempotency_key=session_id,
            endpoint=man_endpoint,
            body=manifest_bytes,
            content_type="application/json",
            timeout_s=timeout_s,
            part="manifest",
        )
        if debug_ingest:
            print(f"[debug][ingest.result] part=manifest status={results['manifest'].status} ms={results['manifest'].ms:.1f}")
    else:
        results["manifest"] = PostResult(part="manifest", status=0, ms=0.0, bytes=len(manifest_bytes))

    return results
