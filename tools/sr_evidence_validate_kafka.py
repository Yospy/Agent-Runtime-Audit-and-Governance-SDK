#!/usr/bin/env python3
"""
Validate evidence NDJSON lines against the latest Confluent Schema Registry schema using confluent-kafka.

Usage:
  python3 tools/sr_evidence_validate_kafka.py --subject setorra.evidence.v1-value --file evidence/<session>/evidence.jsonl

Env vars required:
  CONFLUENT_SCHEMA_REGISTRY_URL
  CONFLUENT_SR_API_KEY
  CONFLUENT_SR_API_SECRET

Notes:
  - If --file is omitted, the script picks the newest evidence/*/evidence.jsonl.
  - Exits non-zero on validation failure; prints the first error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import SerializationContext, MessageField

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


def _load_records(path: Path) -> List[Dict[str, Any]]:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate evidence against SR schema (confluent-kafka)")
    ap.add_argument("--subject", default="setorra.evidence-value", help="SR subject for evidence (used only when --file provided)")
    ap.add_argument("--file", help="Path to evidence NDJSON (defaults to newest evidence/*/evidence.jsonl)")
    ap.add_argument(
        "--use-sample",
        dest="use_sample",
        action="store_true",
        help="Validate a built-in canonical sample instead of reading evidence/*",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed debug info (subject/schema_id and first error).",
    )
    args = ap.parse_args()

    sr_url = os.getenv("CONFLUENT_SCHEMA_REGISTRY_URL")
    sr_user = os.getenv("CONFLUENT_SR_API_KEY")
    sr_pass = os.getenv("CONFLUENT_SR_API_SECRET")
    if not sr_url or not sr_user or not sr_pass:
        print("[!] Missing SR env vars: CONFLUENT_SCHEMA_REGISTRY_URL, CONFLUENT_SR_API_KEY, CONFLUENT_SR_API_SECRET")
        return 2

    # Build records per artifact
    def sample_evidence():
        return [
            {
                "schema_version": "1.3",
                "session_id": "01KBT0F3Z8R8K0J2H0B6V4Q9X2",
                "event_index": 0,
                "event_type": "agent.started",
                "timestamp": "2025-11-23T10:45:00Z",
                "agent": {"name": "sample-agent", "version": "1.0.0"},
                "privacy_flags": [],
                "redacted_fields": [],
                "parameters_redacted": None,
                "context": None,
                "execution": None,
                "policy": None,
                "approval": None,
                "guardrail": None,
                "integrity": {
                    "prev_hash": None,
                    "event_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "signature": None,
                },
            }
        ]

    def sample_output():
        return [
            {
                "schema_version": "1.3",
                "session_id": "01KBT0F3Z8R8K0J2H0B6V4Q9X2",
                "timestamp": "2025-11-23T10:45:02Z",
                "content": "Sample final answer.",
                "prompts": None,
                "tools_used": [],
                "errors": [],
                "integrity": {
                    "first_event_hash": None,
                    "final_event_hash": None,
                    "chain_length": None,
                },
            }
        ]

    def sample_manifest():
        return [
            {
                "schema_version": "manifest-v1",
                "session_id": "01KBT0F3Z8R8K0J2H0B6V4Q9X2",
                "context": {
                    "session_id": "01KBT0F3Z8R8K0J2H0B6V4Q9X2",
                    "env": None,
                    "toolchain_version": None,
                    "evidence_schema_version": None,
                    "started_at": None,
                    "finished_at": None,
                },
                "produced_at": "2025-11-23T10:45:03Z",
                "hash_alg": "sha256-v1",
                "objects": [
                    {
                        "name": "evidence.jsonl",
                        "content_type": "application/x-ndjson",
                        "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                        "bytes": 512,
                        "lines": None,
                    },
                    {
                        "name": "output.json",
                        "content_type": "application/json",
                        "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                        "bytes": 256,
                        "lines": None,
                    },
                ],
                "chain_summary": {
                    "first_event_hash": None,
                    "final_event_hash": None,
                    "chain_length": None,
                },
            }
        ]

    subjects_payloads = []
    if args.use_sample:
        subjects_payloads = [
            ("setorra.evidence-value", sample_evidence()),
            ("setorra.output-value", sample_output()),
            ("setorra.manifest-value", sample_manifest()),
        ]
    else:
        try:
            evidence_path = Path(args.file) if args.file else _find_latest_evidence()
            evidence_records = _load_records(evidence_path)
        except Exception as e:
            print(f"[!] {e}")
            return 2
        missing = [r for r in evidence_records if "event_index" not in r]
        if missing:
            print("[!] Loaded evidence lacks required field 'event_index'. Run a fresh SDK session or use --use-sample to test SR schema.")
            return 1
        subjects_payloads = [(args.subject, evidence_records)]

    client = SchemaRegistryClient({"url": sr_url, "basic.auth.user.info": f"{sr_user}:{sr_pass}"})

    for subject, records in subjects_payloads:
        try:
            latest = client.get_latest_version(subject)
            schema_id = latest.schema_id
            schema_str = latest.schema.schema_str
            serializer = AvroSerializer(
                client,
                schema_str,
                conf={
                    "auto.register.schemas": False,
                    # Force subject to the provided one to avoid TopicNameStrategy collisions.
                    "subject.name.strategy": lambda ctx, schema: subject,
                },
            )
            ctx = SerializationContext("validate-topic", MessageField.VALUE)
            for idx, rec in enumerate(records, 1):
                serializer(rec, ctx)
            print(f"[ok] validated {len(records)} records against {subject} (schema_id={schema_id}) using {'built-in sample' if args.use_sample else args.file or 'latest evidence'}")
            if args.verbose:
                print(f"[schema] {subject}:\n{schema_str}")
                print(f"[sample] {subject} payload:\n{json.dumps(records, indent=2)}")
        except Exception as e:
            if args.verbose:
                print(f"[fail] subject={subject} error={e}")
                print(f"[debug] schema_id={schema_id if 'schema_id' in locals() else 'n/a'}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
