#!/usr/bin/env python3
"""Local no-network action gateway harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from setorra import FakeConnector, firewall, setorra_collector


def _read_request(path: Path | None):
    raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path)
    parser.add_argument("--storage-dir", default="evidence")
    args = parser.parse_args()

    request = _read_request(args.request)
    collector = setorra_collector("local-action-gateway", "0.1.0", storage_dir=args.storage_dir)
    collector.start_session()
    result = firewall(collector).execute_with_connector(request, FakeConnector())
    artifacts = collector.end_session(final_output={"decision": result.decision, "status": result.status})
    print(
        json.dumps(
            {
                "action_id": result.action_id,
                "decision": result.decision,
                "status": result.status,
                "artifact_dir": artifacts["artifact_dir"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
