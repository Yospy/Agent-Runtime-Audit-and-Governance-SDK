"""ULID generation (UUIDv7-preferred semantics via ULID fallback) for Setorra.

Generates a 26-character Crockford Base32 ULID string suitable for
monotonic, time-ordered identifiers in filenames (e.g., `<id>-output.json`).

We implement ULID locally to avoid external dependencies.
Spec: https://github.com/ulid/spec
"""

from __future__ import annotations

import os
import time
from typing import Final

_CROCKFORD32_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a 26-char ULID string (time-ordered, URL-safe)."""
    # 48-bit timestamp in milliseconds since Unix epoch
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    # 80 bits of randomness
    rand_bytes = os.urandom(10)

    # Combine into 128-bit integer: timestamp (48 bits) || randomness (80 bits)
    value = (ts_ms << 80) | int.from_bytes(rand_bytes, "big", signed=False)

    # Encode into 26 chars base32 (Crockford)
    encoded = _base32_crockford_encode(value, 26)
    return encoded


def _base32_crockford_encode(value: int, length: int) -> str:
    chars = []
    mask = 0b11111
    for _ in range(length):
        idx = value & mask
        chars.append(_CROCKFORD32_ALPHABET[idx])
        value >>= 5
    chars.reverse()
    return "".join(chars)
