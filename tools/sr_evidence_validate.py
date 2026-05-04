#!/usr/bin/env python3
"""
Validate evidence NDJSON lines against the latest Schema Registry schema.

Usage:
  python3 tools/sr_evidence_validate.py --subject setorra.evidence.v1-value --file evidence/<session>/evidence.jsonl

Notes:
  - Env vars required: CONFLUENT_SCHEMA_REGISTRY_URL, CONFLUENT_SR_API_KEY, CONFLUENT_SR_API_SECRET.
  - If --file is omitted, the script picks the newest evidence/*/evidence.jsonl.
  - Reports the first validation error with line number; exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from fastavro import parse_schema
from fastavro.validation import validate_many

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass


def _find_latest_evidence() -> Path:
    root = Path("evidence")
    if not root.exists():
        raise FileNotFoundError("evidence/ directory not found; specify --file")
    dirs = [p for p in root.iterdir() if p.is_dir() and (p / "evidence.jsonl").exists()]
    if not dirs:
        raise FileNotFoundError("no evidence/*/evidence.jsonl found; specify --file")
    latest = max(dirs, key=lambda p: p.stat().st_mtime)
    return latest / "evidence.jsonl"


def _load_lines(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        raw_lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
    records = []
    for i, ln in enumerate(raw_lines, 1):
        try:
            records.append(json.loads(ln))
        except json.JSONDecodeError as e:
            raise ValueError(f"Line {i} is not valid JSON: {e}") from e
    if not records:
        raise ValueError("evidence file is empty")
    return records


def _fetch_latest_schema(subject: str) -> Dict[str, Any]:
    url = os.getenv("CONFLUENT_SCHEMA_REGISTRY_URL")
    user = os.getenv("CONFLUENT_SR_API_KEY")
    pwd = os.getenv("CONFLUENT_SR_API_SECRET")
    if not url or not user or not pwd:
        raise EnvironmentError("Missing SR env vars: CONFLUENT_SCHEMA_REGISTRY_URL, CONFLUENT_SR_API_KEY, CONFLUENT_SR_API_SECRET")
    resp = requests.get(
        f"{url.rstrip('/')}/subjects/{subject}/versions/latest",
        auth=(user, pwd),
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    schema_str = payload.get("schema")
    if not schema_str:
        raise ValueError("Schema registry response missing 'schema'")
    return json.loads(schema_str)


def validate_records(schema: Dict[str, Any], records: List[Dict[str, Any]]) -> Tuple[bool, str]:
    try:
        parsed = parse_schema(schema)
        validate_many(records, parsed)
        return True, ""
    except Exception as e:  # fastavro raises descriptive errors
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate evidence against SR schema")
    ap.add_argument("--subject", default="setorra.evidence.v1-value", help="Schema Registry subject for evidence")
    ap.add_argument("--file", help="Path to evidence NDJSON (defaults to newest evidence/*/evidence.jsonl)")
    args = ap.parse_args()

    try:
        evidence_path = Path(args.file) if args.file else _find_latest_evidence()
    except Exception as e:
        print(f"[!] {e}")
        return 2

    try:
        records = _load_lines(evidence_path)
    except Exception as e:
        print(f"[!] Failed to load evidence: {e}")
        return 2

    try:
        schema = _fetch_latest_schema(args.subject)
    except Exception as e:
        print(f"[!] Failed to fetch schema: {e}")
        return 1

    ok, err = validate_records(schema, records)
    if ok:
        print(f"[ok] validated {len(records)} records against {args.subject} using {evidence_path}")
        return 0
    print(f"[fail] schema validation error: {err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
