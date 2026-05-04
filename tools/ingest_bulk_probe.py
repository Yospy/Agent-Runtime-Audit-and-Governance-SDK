#!/usr/bin/env python3
"""
Bulk ingest probe (synthetic): generates a single in-memory session and posts it to /ingest/*.

Defaults (no args):
- base_url: SETORRA_BACKEND or http://127.0.0.1:5001
- auth: none (uses SETORRA_API_KEY if set for Bearer)
- timeout: 8000 ms
- endpoints: /ingest/evidence, /ingest/output, /ingest/manifest
- idempotency: Idempotency-Key = synthetic session_id

Artifacts:
- Evidence NDJSON with 2 events (agent.started, agent.finished) and correct integrity hashes.
- Output JSON with integrity summary.
- Manifest JSON with numeric lines for evidence/output and correct sha256/bytes.

Exit non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from hashlib import sha256
from typing import Dict, Optional, Tuple

# Allow local imports when run from tools/
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from setorra.integrity import compute_event_hash  # type: ignore


def _hash(b: bytes) -> str:
    return sha256(b).hexdigest()


def _post(url: str, body: bytes, content_type: str, idempotency_key: str, auth: Optional[str], timeout_s: float) -> Tuple[int, float, str]:
    headers = {
        "Content-Type": content_type,
        "Accept": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": "SetorraSDK/bulk-probe-1",
    }
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    t0 = time.perf_counter()
    status = 0
    resp_snippet = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - local/backend
            status = int(getattr(resp, "status", 200))
            try:
                resp_snippet = resp.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                resp_snippet = ""
    except urllib.error.HTTPError as e:  # type: ignore
        status = int(getattr(e, "code", 0) or 0)
        try:
            resp_snippet = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            resp_snippet = str(e)[:200]
    except Exception as e:  # pragma: no cover
        resp_snippet = str(e)[:200]
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return status, elapsed_ms, resp_snippet


def _pseudo_ulid() -> str:
    # ULID-like: 26 chars upper-hex slice of uuid4 (good enough for local validation)
    return uuid.uuid4().hex.upper()[:26]


def _synthetic_session() -> Dict[str, bytes]:
    session_id = _pseudo_ulid()
    ts = "2025-12-05T00:00:00Z"

    base_event = {
        "schema_version": "1.3",
        "session_id": session_id,
        "privacy_flags": [],
        "redacted_fields": [],
        "parameters_redacted": None,
        "context": {
            "environment": {"sdk_version": "0.0.0-dev", "environment": "local", "hostname": "synthetic", "runtime": "python"},
            "model": {"name": "gpt-4o-mini"},
            "prompts": {"system": "system-hash", "user": "user-hash"},
            "organization": {"org_id": "org_setorra", "key_id_prefix": "key_0c15", "org_name": "Setorra"},
        },
        "execution": {"status": "started"},
        "policy": {"version": "2025-10-28", "decision": "not_evaluated"},
        "approval": None,
        "guardrail": {"status": "unconfigured"},
        "integrity": {"prev_hash": None, "event_hash": None, "signature": None},
    }

    e0 = dict(base_event)
    e0.update({"event_index": 0, "event_type": "agent.started", "timestamp": ts})
    e0["integrity"] = {"prev_hash": None, "event_hash": None, "signature": None}
    h0 = compute_event_hash(e0)
    e0["integrity"]["event_hash"] = h0

    e1 = dict(base_event)
    e1.update({"event_index": 1, "event_type": "agent.finished", "timestamp": ts})
    e1["execution"] = {"status": "success"}
    e1["integrity"] = {"prev_hash": h0, "event_hash": None, "signature": None}
    h1 = compute_event_hash(e1)
    e1["integrity"]["event_hash"] = h1

    evidence_lines = [
        json.dumps(e0, ensure_ascii=False, separators=(",", ":")),
        json.dumps(e1, ensure_ascii=False, separators=(",", ":")),
    ]
    evidence_bytes = ("\n".join(evidence_lines)).encode("utf-8")

    output = {
        "schema_version": "1.3",
        "session_id": session_id,
        "timestamp": ts,
        "content": "synthetic output",
        "prompts": {"system": "system-hash", "user": "user-hash"},
        "tools_used": [],
        "errors": [],
        "integrity": {"first_event_hash": h0, "final_event_hash": h1, "chain_length": 2},
    }
    output_bytes = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    manifest = {
        "schema_version": "manifest-v1",
        "hash_alg": "sha256-v1",
        "produced_at": ts,
        "session_id": session_id,
        "context": {
            "session_id": session_id,
            "env": "local",
            "toolchain_version": "0.0.0-dev",
            "evidence_schema_version": "1.3",
            "started_at": ts,
            "finished_at": ts,
        },
        "objects": [
            {
                "name": "evidence.jsonl",
                "bytes": len(evidence_bytes),
                "sha256": _hash(evidence_bytes),
                "content_type": "application/x-ndjson",
                "lines": 2,
            },
            {
                "name": "output.json",
                "bytes": len(output_bytes),
                "sha256": _hash(output_bytes),
                "content_type": "application/json",
                "lines": 1,
            },
        ],
        "chain_summary": {"first_event_hash": h0, "final_event_hash": h1, "chain_length": 2},
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    return {
        "session_id": session_id,
        "evidence": evidence_bytes,
        "output": output_bytes,
        "manifest": manifest_bytes,
    }


def main() -> int:
    base_url = os.getenv("SETORRA_BACKEND", "http://127.0.0.1:5001").rstrip("/")
    auth = None
    if os.getenv("SETORRA_API_KEY"):
        auth = f"Bearer {os.getenv('SETORRA_API_KEY')}"
    timeout_ms = int(os.getenv("INGEST_BULK_TIMEOUT_MS", "8000"))
    timeout_s = max(0.5, timeout_ms / 1000.0)

    sess = _synthetic_session()
    session_id = sess["session_id"]
    evidence_bytes = sess["evidence"]
    output_bytes = sess["output"]
    manifest_bytes = sess["manifest"]

    print(
        f"[session] {session_id} evidence={len(evidence_bytes)}B sha256={_hash(evidence_bytes)} | "
        f"output={len(output_bytes)}B sha256={_hash(output_bytes)} | "
        f"manifest={len(manifest_bytes)}B sha256={_hash(manifest_bytes)}"
    )

    # Evidence
    st_e, ms_e, resp_e = _post(
        url=f"{base_url}/ingest/evidence",
        body=evidence_bytes,
        content_type="application/x-ndjson",
        idempotency_key=session_id,
        auth=auth,
        timeout_s=timeout_s,
    )
    print(f"[post] evidence status={st_e} ms={ms_e:.1f} resp={resp_e}")
    if not (200 <= st_e < 300):
        return 1

    # Output
    st_o, ms_o, resp_o = _post(
        url=f"{base_url}/ingest/output",
        body=output_bytes,
        content_type="application/json",
        idempotency_key=session_id,
        auth=auth,
        timeout_s=timeout_s,
    )
    print(f"[post] output   status={st_o} ms={ms_o:.1f} resp={resp_o}")
    if not (200 <= st_o < 300):
        return 1

    # Manifest
    st_m, ms_m, resp_m = _post(
        url=f"{base_url}/ingest/manifest",
        body=manifest_bytes,
        content_type="application/json",
        idempotency_key=session_id,
        auth=auth,
        timeout_s=timeout_s,
    )
    print(f"[post] manifest status={st_m} ms={ms_m:.1f} resp={resp_m}")
    ok = all(200 <= c < 300 for c in (st_e, st_o, st_m))
    print(f"[result] ok={ok} session={session_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
