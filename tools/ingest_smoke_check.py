#!/usr/bin/env python3
"""Minimal ingress smoke test using bare Idempotency-Key (session_id).

What it does
- Loads SDK-generated artifacts from evidence/<session_id>.
- Verifies local integrity hashes (compute_event_hash, chain continuity).
- Posts evidence → output → manifest with Idempotency-Key exactly equal to session_id.
- Prints concise statuses so you can mirror backend logs by hand.

Usage examples
  python3 tools/ingest_smoke_check.py --session 01K... --base-url http://127.0.0.1:5001
  python3 tools/ingest_smoke_check.py --session 01K... --timeout-ms 8000 --verbose

Notes
- This does not retry; it is intentionally simple for debugging idempotency/ingest.
- Assumes artifacts already exist under evidence/<session_id>/.
- Does not append :evidence/:output/:manifest to Idempotency-Key; it uses the bare session_id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Make repo root importable when run from tools/
try:
    _THIS = Path(__file__).resolve()
    _ROOT = _THIS.parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
except Exception:
    pass

# Best-effort .env load (project root preferred)
try:
    from dotenv import load_dotenv  # type: ignore

    _DOTENV = (_ROOT / ".env") if "_ROOT" in globals() else None
    if _DOTENV and _DOTENV.exists():
        load_dotenv(dotenv_path=str(_DOTENV))
    else:
        load_dotenv()
except Exception:
    pass

from setorra.integrity import compute_event_hash


def _handshake(api_key: str, base_url: str, timeout_s: float) -> Tuple[str, Optional[str]]:
    """Return (session_token, org_id). Raises on failure."""
    url = f"{base_url}/v1/auth/handshake"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "SetorraSDK-IngestSmoke/1",
    }
    body = json.dumps({"env": "local", "sdk_version": "smoke-0.0.1"}).encode("utf-8")
    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec
        data = resp.read().decode("utf-8")
        payload = json.loads(data or "{}")
        token = payload.get("session_token") or payload.get("token") or ""
        org_id = payload.get("org_id")
        if not token:
            raise RuntimeError("handshake response missing session_token")
        return token, org_id


def _load_bytes(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read()


def _normalize_event_index(evidence_bytes: bytes) -> bytes:
    """Normalize legacy evidence payloads to the current event_index field."""
    lines = [ln for ln in evidence_bytes.decode("utf-8").split("\n") if ln.strip()]
    normalized: list[str] = []
    for ln in lines:
        evt = json.loads(ln)
        normalized.append(json.dumps(evt))
    return ("\n".join(normalized)).encode("utf-8")


def _strip_blank_lines_ndjson(raw: bytes) -> bytes:
    """Remove blank/whitespace-only lines to avoid ingest rejections."""
    lines = raw.decode("utf-8").split("\n")
    cleaned = [ln for ln in lines if ln.strip()]
    # Do not append a trailing newline to avoid backends flagging an empty line.
    return "\n".join(cleaned).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _summarize_payloads(
    *, evidence: bytes, output: bytes, manifest: bytes, verbose: bool
) -> None:
    lines = [ln for ln in evidence.decode("utf-8").split("\n") if ln.strip()]
    print(
        "[payload] evidence=%dB lines=%d sha256=%s | output=%dB sha256=%s | manifest=%dB sha256=%s"
        % (
            len(evidence),
            len(lines),
            _sha256_hex(evidence),
            len(output),
            _sha256_hex(output),
            len(manifest),
            _sha256_hex(manifest),
        )
    )
    if verbose and lines:
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        print(
            "[payload.verbose] first_event=%s last_event=%s first_idx=%s last_idx=%s"
            % (
                first.get("event_type"),
                last.get("event_type"),
                first.get("event_index"),
                last.get("event_index"),
            )
        )


def _generate_ulid_like() -> str:
    # Not a strict ULID, but matches charset/length; good enough for smoke.
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(alphabet[int(secrets.randbits(5)) % len(alphabet)] for _ in range(26))


def _build_synthetic_payloads(sid: str) -> Tuple[bytes, bytes, bytes]:
    ts_start = "2025-11-23T10:45:00Z"
    ts_end = "2025-11-23T10:45:02Z"
    ev0 = {
        "schema_version": "1.3",
        "session_id": sid,
        "event_index": 0,
        "event_type": "agent.started",
        "timestamp": ts_start,
        "agent": {"name": "smoke-agent", "version": "1.0.0"},
        "privacy_flags": [],
        "redacted_fields": [],
        "parameters_redacted": None,
        "context": None,
        "execution": None,
        "policy": None,
        "approval": None,
        "guardrail": None,
        "integrity": {"prev_hash": None, "event_hash": "", "signature": None},
    }
    ev1 = {
        "schema_version": "1.3",
        "session_id": sid,
        "event_index": 1,
        "event_type": "agent.finished",
        "timestamp": ts_end,
        "agent": {"name": "smoke-agent", "version": "1.0.0"},
        "privacy_flags": [],
        "redacted_fields": [],
        "parameters_redacted": None,
        "context": None,
        "execution": None,
        "policy": None,
        "approval": None,
        "guardrail": None,
        "integrity": {"prev_hash": "", "event_hash": "", "signature": None},
    }
    events = [ev0, ev1]
    prev = None
    for evt in events:
        evt["integrity"]["prev_hash"] = prev
        evt["integrity"]["event_hash"] = compute_event_hash(evt)
        prev = evt["integrity"]["event_hash"]
    ndjson = "\n".join(json.dumps(e) for e in events).encode("utf-8")
    out = {
        "schema_version": "1.3",
        "session_id": sid,
        "timestamp": ts_end,
        "content": "Smoke test output.",
        "prompts": None,
        "tools_used": [],
        "errors": [],
        "integrity": {
            "first_event_hash": events[0]["integrity"]["event_hash"],
            "final_event_hash": events[-1]["integrity"]["event_hash"],
            "chain_length": len(events),
        },
    }
    ev_sha = hashlib.sha256(ndjson).hexdigest()
    out_bytes = json.dumps(out).encode("utf-8")
    out_sha = hashlib.sha256(out_bytes).hexdigest()
    manifest = {
        "schema_version": "manifest-v1",
        "session_id": sid,
        "context": {
            "session_id": sid,
            "env": None,
            "toolchain_version": "1.0.0",
            "evidence_schema_version": "1.3",
            "started_at": ts_start,
            "finished_at": ts_end,
        },
        "produced_at": "2025-11-23T10:45:03Z",
        "hash_alg": "sha256-v1",
        "objects": [
            {
                "name": "evidence.jsonl",
                "content_type": "application/x-ndjson",
                "sha256": ev_sha,
                "bytes": len(ndjson),
                "lines": len(events),
            },
            {
                "name": "output.json",
                "content_type": "application/json",
                "sha256": out_sha,
                "bytes": len(out_bytes),
                "lines": 1,
            },
        ],
        "chain_summary": {
            "first_event_hash": events[0]["integrity"]["event_hash"],
            "final_event_hash": events[-1]["integrity"]["event_hash"],
            "chain_length": len(events),
        },
    }
    return ndjson, out_bytes, json.dumps(manifest).encode("utf-8")


def _verify_integrity(evidence_bytes: bytes) -> Dict[str, Any]:
    result = {"total": 0, "mismatches": [], "chain_ok": True, "sequence_ok": False}
    lines = [ln for ln in evidence_bytes.decode("utf-8").split("\n") if ln.strip()]
    result["total"] = len(lines)
    if not lines:
        return result
    events = [json.loads(ln) for ln in lines]
    result["sequence_ok"] = events[0].get("event_type") == "agent.started" and events[-1].get(
        "event_type"
    ) == "agent.finished"
    prev_hash = None
    for idx, evt in enumerate(events):
        stored = evt.get("integrity", {}).get("event_hash")
        computed = compute_event_hash(evt)
        if stored != computed:
            result["mismatches"].append({"event_index": evt.get("event_index", idx), "stored": stored, "computed": computed})
        if idx == 0:
            if evt.get("integrity", {}).get("prev_hash") is not None:
                result["chain_ok"] = False
        else:
            if evt.get("integrity", {}).get("prev_hash") != prev_hash:
                result["chain_ok"] = False
        prev_hash = stored
    return result


def _post(
    *,
    base_url: str,
    token: str,
    org_id: Optional[str],
    agent_label: str,
    idempotency_key: str,
    content_type: str,
    body: bytes,
    endpoint: str,
    timeout_s: float,
) -> Tuple[int, float, Optional[str]]:
    url = f"{base_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "X-Setorra-Agent": agent_label,
        # Bare session_id only; manifest uses context.session_id too.
        "Idempotency-Key": idempotency_key,
        "Accept": "application/json",
        "User-Agent": "SetorraSDK-IngestSmoke/1",
    }
    if org_id:
        headers["X-Setorra-Org"] = org_id
    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    t0 = time.perf_counter()
    code = 0
    err_msg = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec
            try:
                _ = resp.read(0)
            except Exception:
                pass
            code = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as e:  # type: ignore
        code = int(getattr(e, "code", 0) or 0)
        try:
            data = e.read().decode("utf-8")
            j = json.loads(data)
            err_msg = json.dumps(j)[:500]
        except Exception:
            err_msg = str(e)[:200]
    except Exception as e:  # pragma: no cover
        err_msg = str(e)
    ms = (time.perf_counter() - t0) * 1000.0
    return code, ms, err_msg


def main() -> int:
    ap = argparse.ArgumentParser(description="Setorra ingest smoke test (bare Idempotency-Key)")
    ap.add_argument(
        "--session",
        help="Session ULID (matches evidence/<session_id>). If omitted, uses the most recent evidence/<*> dir.",
    )
    ap.add_argument("--base-url", default=os.getenv("SETORRA_BACKEND", "http://127.0.0.1:5001"))
    ap.add_argument("--agent-label", default="smoke/0.0.1")
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--token", default=os.getenv("SETORRA_SESSION_TOKEN", ""))
    ap.add_argument("--api-key", default=os.getenv("SETORRA_API_KEY", ""))
    ap.add_argument("--org-id", default=os.getenv("SETORRA_ORG_ID", ""))
    ap.add_argument(
        "--use-synthetic",
        action="store_true",
        default=True,
        dest="use_synthetic",
        help="Generate synthetic payloads (evidence/output/manifest) for a fresh session instead of reading evidence/* (default: on)",
    )
    ap.add_argument(
        "--no-synthetic",
        action="store_false",
        dest="use_synthetic",
        help="Use artifacts in evidence/<session_id>/ instead of synthetic payloads",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print high-signal payload details (event types/indexes, hashes)",
    )
    args = ap.parse_args()

    # Resolve session id
    session_id = (args.session or "").strip()
    if args.use_synthetic and not session_id:
        session_id = _generate_ulid_like()
    if not session_id:
        ev_root = Path("evidence")
        if ev_root.exists():
            dirs = [p for p in ev_root.iterdir() if p.is_dir()]
            if dirs:
                session_id = max(dirs, key=lambda p: p.stat().st_mtime).name
    if not session_id:
        print("[!] --session not provided and no evidence/* directories found")
        return 3
    base = args.base_url.rstrip("/")
    timeout_s = max(0.5, args.timeout_ms / 1000.0)

    token = args.token.strip()
    org_id = args.org_id.strip() or None
    if not token:
        if not args.api_key:
            print("[!] Provide --token or set SETORRA_SESSION_TOKEN; alternatively set SETORRA_API_KEY to handshake")
            return 2
        try:
            token, org_id = _handshake(args.api_key, base, timeout_s)
            print("[handshake] ok (session token acquired)")
        except Exception as e:
            print(f"[handshake] failed: {e}")
            return 2

    synthetic = args.use_synthetic
    if synthetic and not session_id:
        session_id = _generate_ulid_like()

    if synthetic:
        ndjson_b = None
        out_b = None
        man_b = None
        try:
            ndjson_b, out_b, man_b = _build_synthetic_payloads(session_id)
        except Exception as e:
            print(f"[!] failed to build synthetic payloads: {e}")
            return 2
        stripped = False
        print(f"[session] session_id={session_id} idempotency_key={session_id} base={base} timeout_ms={args.timeout_ms}")
        print(f"[load] synthetic evidence={len(ndjson_b)}B output={len(out_b)}B manifest={len(man_b)}B")
    else:
        ev_p = Path("evidence") / session_id / "evidence.jsonl"
        out_p = Path("evidence") / session_id / "output.json"
        man_p = Path("evidence") / session_id / "manifest.json"
        for p in (ev_p, out_p, man_p):
            if not p.exists():
                print(f"[!] missing artifact: {p}")
                return 3

        ndjson_b_raw = _load_bytes(ev_p)
        ndjson_norm = _normalize_event_index(ndjson_b_raw)
        ndjson_b = _strip_blank_lines_ndjson(ndjson_norm)
        stripped = len(ndjson_b_raw) != len(ndjson_b)
        out_b = _load_bytes(out_p)
        man_b = _load_bytes(man_p)
        print(f"[load] evidence={len(ndjson_b)}B output={len(out_b)}B manifest={len(man_b)}B")
        if stripped:
            print("[note] stripped blank lines from evidence NDJSON before ingest")

    if ndjson_b is None or out_b is None or man_b is None:
        print("[!] payloads not prepared")
        return 2

    if not synthetic:
        print(
            f"[session] session_id={session_id} idempotency_key={session_id} base={base} timeout_ms={args.timeout_ms} source=evidence/{session_id}"
        )

    _summarize_payloads(evidence=ndjson_b, output=out_b, manifest=man_b, verbose=args.verbose)

    integ = _verify_integrity(ndjson_b)
    if integ["mismatches"]:
        print(f"[integrity] mismatches={len(integ['mismatches'])} chain_ok={integ['chain_ok']} seq_ok={integ['sequence_ok']}")
        if args.verbose:
            for mis in integ["mismatches"][:5]:
                print(f"  [integrity.mismatch] idx={mis.get('event_index')} stored={mis.get('stored')} computed={mis.get('computed')}")
    else:
        print(f"[integrity] ok chain_ok={integ['chain_ok']} seq_ok={integ['sequence_ok']}")

    idk = session_id

    code_e, ms_e, err_e = _post(
        base_url=base,
        token=token,
        org_id=org_id,
        agent_label=args.agent_label,
        idempotency_key=idk,
        content_type="application/x-ndjson",
        body=ndjson_b,
        endpoint="/v1/ingest/evidence",
        timeout_s=timeout_s,
    )
    print(f"[post] evidence status={code_e} ms={ms_e:.1f} err={err_e or ''}")
    if not (200 <= code_e < 300):
        print("[skip] evidence failed; skipping output/manifest")
        return 1

    code_o, ms_o, err_o = _post(
        base_url=base,
        token=token,
        org_id=org_id,
        agent_label=args.agent_label,
        idempotency_key=idk,
        content_type="application/json",
        body=out_b,
        endpoint="/v1/ingest/output",
        timeout_s=timeout_s,
    )
    print(f"[post] output   status={code_o} ms={ms_o:.1f} err={err_o or ''}")
    if not (200 <= code_o < 300):
        print("[skip] output failed; skipping manifest")
        return 1

    code_m, ms_m, err_m = _post(
        base_url=base,
        token=token,
        org_id=org_id,
        agent_label=args.agent_label,
        idempotency_key=idk,
        content_type="application/json",
        body=man_b,
        endpoint="/v1/ingest/manifest",
        timeout_s=timeout_s,
    )
    print(f"[post] manifest status={code_m} ms={ms_m:.1f} err={err_m or ''}")

    ok = all(200 <= c < 300 for c in (code_e, code_o, code_m))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
