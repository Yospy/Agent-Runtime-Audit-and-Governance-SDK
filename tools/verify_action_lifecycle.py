#!/usr/bin/env python3
"""Verify action lifecycle completeness for a Setorra evidence JSONL file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from setorra.action_lifecycle import verify_action_lifecycle


def _load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_path", type=Path)
    args = parser.parse_args()

    report = verify_action_lifecycle(_load_jsonl(args.evidence_path))
    print(
        json.dumps(
            {
                "ok": report.ok,
                "actions_checked": report.actions_checked,
                "errors": report.errors,
            },
            indent=2,
        )
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
