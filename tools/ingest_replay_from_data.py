#!/usr/bin/env python3
"""
Replay an SDK-generated session from ./data/<session_id>/ to the backend ingest endpoints.

What it does:
- Picks a session directory (latest by mtime or --session).
- Reads evidence.jsonl, output.json, manifest.json as raw bytes.
- POSTs them in order to /ingest/evidence, /ingest/output, /ingest/manifest with Idempotency-Key=session_id.
- Logs status codes, elapsed ms, and short response snippets.

Usage examples:
  python3 tools/ingest_replay_from_data.py                     # replays latest session under ./data
  python3 tools/ingest_replay_from_data.py --session 01ABC...  # replays specific session
  python3 tools/ingest_replay_from_data.py --base-url http://127.0.0.1:5001
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


def _find_latest_session(data_root: Path) -> Optional[Path]:
    if not data_root.exists():
        return None
    dirs = [p for p in data_root.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _load_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _post(
    *,
    url: str,
    body: bytes,
    content_type: str,
    idempotency_key: str,
    auth: Optional[str],
    timeout_s: float,
) -> Tuple[int, float, str]:
    headers = {
        "Content-Type": content_type,
        "Accept": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": "SetorraSDK/replay-1",
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
                data = resp.read().decode("utf-8", errors="replace")
                resp_snippet = data[:500]
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
    ap = argparse.ArgumentParser(description="Replay SDK artifacts from ./data/<session_id>/ to ingest endpoints")
    ap.add_argument("--session", help="Session id under ./data to replay. If omitted, uses latest dir.")
    ap.add_argument("--base-url", default=os.getenv("SETORRA_BACKEND", "http://127.0.0.1:5001"))
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--auth", help="Optional Authorization header value, e.g., 'Bearer <token>'")
    ap.add_argument(
        "--fallback-session",
        default="01TESTSESSIONFALLBACK0000000000",
        help="Fallback session id to use if no data/* dir is found (useful for quick smoke).",
    )
    args = ap.parse_args()

    # Resolve data root (prefer ./data relative to repo root; works when run from repo or tools/)
    cwd = Path.cwd()
    candidates = [cwd / "data", cwd.parent / "data"]
    data_root = next((p for p in candidates if p.exists()), candidates[0])
    session_id = args.session
    if not session_id:
        latest = _find_latest_session(data_root)
        if latest:
            session_id = latest.name
        else:
            # Use fallback if provided; expect caller to place files under data/<fallback>
            session_id = args.fallback_session
            print(f"[!] No sessions found under ./data; using fallback session_id={session_id}")

    session_dir = data_root / session_id
    if not session_dir.exists():
        print(f"[!] Session directory not found: {session_dir}")
        return 1

    evidence_path = session_dir / "evidence.jsonl"
    output_path = session_dir / "output.json"
    manifest_path = session_dir / "manifest.json"
    for p in (evidence_path, output_path, manifest_path):
        if not p.exists():
            print(f"[!] Missing artifact: {p}")
            return 1

    evidence_bytes = _load_bytes(evidence_path)
    output_bytes = _load_bytes(output_path)
    manifest_bytes = _load_bytes(manifest_path)

    def _h(b: bytes) -> str:
        return sha256(b).hexdigest()

    print(f"[session] {session_id}")
    print(f"[base] {args.base_url} timeout_ms={args.timeout_ms}")
    print(
        f"[artifacts] evidence={len(evidence_bytes)}B sha256={_h(evidence_bytes)} | "
        f"output={len(output_bytes)}B sha256={_h(output_bytes)} | "
        f"manifest={len(manifest_bytes)}B sha256={_h(manifest_bytes)}"
    )

    timeout_s = max(0.5, args.timeout_ms / 1000.0)
    base = args.base_url.rstrip("/")

    # Evidence
    st_e, ms_e, resp_e = _post(
        url=f"{base}/ingest/evidence",
        body=evidence_bytes,
        content_type="application/x-ndjson",
        idempotency_key=session_id,
        auth=args.auth,
        timeout_s=timeout_s,
    )
    print(f"[post] evidence status={st_e} ms={ms_e:.1f} resp={resp_e}")
    if not (200 <= st_e < 300):
        print("[stop] evidence failed; skipping output/manifest")
        return 1

    # Output
    st_o, ms_o, resp_o = _post(
        url=f"{base}/ingest/output",
        body=output_bytes,
        content_type="application/json",
        idempotency_key=session_id,
        auth=args.auth,
        timeout_s=timeout_s,
    )
    print(f"[post] output   status={st_o} ms={ms_o:.1f} resp={resp_o}")
    if not (200 <= st_o < 300):
        print("[stop] output failed; skipping manifest")
        return 1

    # Manifest
    st_m, ms_m, resp_m = _post(
        url=f"{base}/ingest/manifest",
        body=manifest_bytes,
        content_type="application/json",
        idempotency_key=session_id,
        auth=args.auth,
        timeout_s=timeout_s,
    )
    print(f"[post] manifest status={st_m} ms={ms_m:.1f} resp={resp_m}")

    ok = all(200 <= c < 300 for c in (st_e, st_o, st_m))
    print(f"[result] ok={ok} session={session_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
