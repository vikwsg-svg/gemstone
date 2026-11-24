"""Prefixed, time-sortable identifiers (``val_01JD...``)."""

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Return ``<prefix>_<10 chars of ms timestamp><16 random chars>`` (ULID layout)."""
    millis = int(time.time() * 1000)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    return f"{prefix}_{_b32(millis, 10)}{_b32(rand, 16)}"
