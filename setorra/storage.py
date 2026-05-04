"""Atomic file storage utilities for Setorra SDK.

This module provides safe, atomic writers for session artifacts and small
utilities for hashing and counting lines. The SDK writes one folder per run
named by the session_id, containing:
- `output.json` (single JSON object)
- `evidence.jsonl` (one event per line)
- `manifest.json` (commit marker with sizes/hashes/chain summary)

All writes use temp files and atomic rename to avoid partial reads.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import hashlib
from typing import Iterable, Mapping


DEFAULT_DIR = Path("evidence")


def ensure_storage_dir(path: Path | None = None) -> Path:
    root = path or DEFAULT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def atomic_write_json(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), prefix=".tmp-", suffix=".json") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_name = tf.name
    os.replace(tmp_name, path)


def atomic_write_jsonl(path: Path, lines: Iterable[Mapping]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), prefix=".tmp-", suffix=".jsonl") as tf:
        for line in lines:
            tf.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
            tf.write("\n")
        tf.flush()
        os.fsync(tf.fileno())
        tmp_name = tf.name
    os.replace(tmp_name, path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 hex digest of the file at `path`.

    Uses a streaming hash to avoid large memory usage for big evidence files.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def count_lines(path: Path, chunk_size: int = 1024 * 1024) -> int:
    """Return the number of lines in a text file.

    This counts newline characters in streaming fashion and works for .jsonl.
    """
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            total += chunk.count(b"\n")
    return total
