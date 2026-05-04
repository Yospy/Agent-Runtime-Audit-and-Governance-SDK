#!/usr/bin/env python3
"""
Minimal hash probe: post evidence only and print local vs payload hashes for a specific line.

Usage:
  python3 tools/ingest_hash_probe.py                # uses latest data/<session>, line 16
  python3 tools/ingest_hash_probe.py --session ID   # choose session dir under ./data
  python3 tools/ingest_hash_probe.py --line 9       # choose line index to inspect

Env/flags:
  --base-url (default SETORRA_BACKEND or http://127.0.0.1:5001)
  --timeout-ms (default 8000)
  --auth (optional Authorization header value, e.g., 'Bearer <token>')
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Optional, Tuple

# Ensure local imports work when running from tools/
sys.path.append(str(Path(__file__).resolve().parent.parent))
from setorra.integrity import compute_event_hash


def _find_latest_session(data_root: Path) -> Optional[Path]:
    if not data_root.exists():
        return None
    dirs = [p for p in data_root.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _post(url: str, body: bytes, content_type: str, idempotency_key: str, auth: Optional[str], timeout_s: float) -> Tuple[int, float, str]:
    headers = {
        "Content-Type": content_type,
        "Accept": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": "SetorraSDK/hash-probe-1",
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
                resp_snippet = resp.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                resp_snippet = ""
    except urllib.error.HTTPError as e:  # type: ignore
        status = int(getattr(e, "code", 0) or 0)
        try:
            resp_snippet = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            resp_snippet = str(e)[:200]
    except Exception as e:  # pragma: no cover
        resp_snippet = str(e)[:200]
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return status, elapsed_ms, resp_snippet


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe evidence hash alignment for a single session/line")
    ap.add_argument("--session", help="Session id under ./data (default: latest)")
    ap.add_argument("--line", type=int, default=16, help="Zero-based evidence line index to inspect")
    ap.add_argument("--base-url", default=os.getenv("SETORRA_BACKEND", "http://127.0.0.1:5001"))
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--auth", help="Optional Authorization header, e.g., 'Bearer <token>'")
    args = ap.parse_args()

    # Resolve data root (repo or tools/)
    cwd = Path.cwd()
    data_root_candidates = [cwd / "data", cwd.parent / "data"]
    data_root = next((p for p in data_root_candidates if p.exists()), data_root_candidates[0])

    session_id = args.session
    if not session_id:
        latest = _find_latest_session(data_root)
        if latest:
            session_id = latest.name
        else:
            print("[!] No session found under ./data")
            return 1

    session_dir = data_root / session_id
    evidence_path = session_dir / "evidence.jsonl"
    if not evidence_path.exists():
        print(f"[!] Missing evidence: {evidence_path}")
        return 1

    evidence_lines = evidence_path.read_text(encoding="utf-8").splitlines()
    if args.line < 0 or args.line >= len(evidence_lines):
        print(f"[!] Line index {args.line} out of range (0..{len(evidence_lines)-1})")
        return 1

    line_raw = evidence_lines[args.line]
    try:
        ev_obj = json.loads(line_raw)
    except Exception as e:
        print(f"[!] Failed to parse line {args.line}: {e}")
        return 1

    local_hash = compute_event_hash(ev_obj)
    provided = ev_obj.get("integrity", {}).get("event_hash")
    prev_hash = ev_obj.get("integrity", {}).get("prev_hash")

    evidence_bytes = "\n".join(evidence_lines).encode("utf-8")

    print(f"[session] {session_id} line={args.line}")
    print(f"[hash] local={local_hash} provided={provided} prev_hash={prev_hash}")
    print(f"[base] {args.base_url} timeout_ms={args.timeout_ms}")
    print(f"[evidence] bytes={len(evidence_bytes)} sha256={sha256(evidence_bytes).hexdigest()}")

    status, ms, resp = _post(
        url=f"{args.base_url.rstrip('/')}/ingest/evidence",
        body=evidence_bytes,
        content_type="application/x-ndjson",
        idempotency_key=session_id,
        auth=args.auth,
        timeout_s=max(0.5, args.timeout_ms / 1000.0),
    )
    print(f"[post] evidence status={status} ms={ms:.1f} resp={resp}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
