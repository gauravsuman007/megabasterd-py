"""MEGA's hashcash proof-of-work challenge, ported from HashcashSolver.java.

Challenge header: ``1:<easiness>:<timestamp>:<token>`` where token is 64
base64url characters (48 raw bytes). The solver must find a 4-byte
big-endian nonce such that the first 4 bytes of
``SHA256(nonce || token * 262144)`` (interpreted as a big-endian uint32) are
<= a threshold derived from ``easiness``.
"""
from __future__ import annotations

import hashlib
import struct

from app.core.crypto import bin_to_url_base64, url_base64_to_bin

TOKEN_REPEAT = 262144


def _threshold(easiness: int) -> int:
    return (((easiness & 63) << 1) + 1) << ((easiness >> 6) * 7 + 3)


def parse_challenge(header: str) -> tuple[int, int, bytes]:
    """Parse an 'X-Hashcash' challenge header, returns (easiness, timestamp, token_bytes)."""
    _version, easiness_s, ts_s, token_s = header.split(":", 3)
    return int(easiness_s), int(ts_s), url_base64_to_bin(token_s)


def solve(header: str, max_nonce: int = 2**32) -> str:
    """Solve a hashcash challenge header and return the response header value:
    ``1:<token>:<nonce_base64url>``."""
    easiness, _timestamp, token = parse_challenge(header)
    _version, _easiness_s, _ts_s, token_s = header.split(":", 3)
    threshold = _threshold(easiness)
    buffer_tail = token * TOKEN_REPEAT

    nonce = 0
    while nonce < max_nonce:
        candidate = struct.pack(">I", nonce) + buffer_tail
        digest = hashlib.sha256(candidate).digest()
        value = struct.unpack(">I", digest[:4])[0]
        if value <= threshold:
            nonce_b64 = bin_to_url_base64(struct.pack(">I", nonce))
            return f"1:{token_s}:{nonce_b64}"
        nonce += 1

    raise RuntimeError("Hashcash: no solution found within max_nonce")
