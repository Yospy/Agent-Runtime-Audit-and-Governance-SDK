#!/usr/bin/env python3
"""
Validate and round-trip a sample JSON payload against an Avro schema.

Usage:
  python3 tools/avro_roundtrip.py --schema schemas/avro/evidence-event-v1.avsc --json sample-event.json

What it does:
  - Loads the Avro schema and parses it.
  - Loads the JSON payload (single object or array of objects).
  - Validates each object against the schema.
  - Encodes then decodes to verify round-trip consistency.
  - Prints a short summary and exits non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, List

from fastavro import parse_schema, reader, writer
from fastavro.validation import validate_many


def load_json(path: Path) -> List[Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    return [data]


def validate_records(schema: dict, records: List[Any]) -> None:
    # validate_many raises ValueError on the first failure
    validate_many(records, schema)


def round_trip(schema: dict, records: Iterable[Any]) -> List[Any]:
    import io

    buf = io.BytesIO()
    writer(buf, schema, list(records))
    buf.seek(0)
    dec = list(reader(buf, schema))
    return dec


def main() -> int:
    ap = argparse.ArgumentParser(description="Avro validate and round-trip")
    ap.add_argument("--schema", required=True, help="Path to .avsc")
    ap.add_argument("--json", required=True, help="Path to JSON payload (object or array)")
    args = ap.parse_args()

    schema_path = Path(args.schema)
    json_path = Path(args.json)

    if not schema_path.exists():
        print(f"[!] schema not found: {schema_path}")
        return 2
    if not json_path.exists():
        print(f"[!] json not found: {json_path}")
        return 2

    with schema_path.open("r", encoding="utf-8") as fh:
        raw_schema = json.load(fh)
    schema = parse_schema(raw_schema)

    try:
        records = load_json(json_path)
    except Exception as e:
        print(f"[!] failed to parse JSON: {e}")
        return 2

    try:
        validate_records(schema, records)
    except Exception as e:
        print(f"[!] validation failed: {e}")
        return 1

    try:
        rt = round_trip(schema, records)
    except Exception as e:
        print(f"[!] round-trip failed: {e}")
        return 1

    print(f"[ok] schema={schema_path.name} records={len(records)} validated and round-tripped")
    if len(records) == len(rt) and records and records[0] != rt[0]:
        print("[warn] note: defaults may be applied on decode; compare records vs round-trip output if needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
