"""HMAC-SHA256 webhook signatures over `timestamp + "." + body`.

The signed timestamp limits how long a captured request can be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Maximum request age; allows for clock skew.
MAX_AGE_SECONDS = 300

SIGNATURE_HEADER = "x-curator-signature"
TIMESTAMP_HEADER = "x-curator-timestamp"


def signing_payload(timestamp: str, body: bytes) -> bytes:
    """What both sides hash. `body` must be the raw bytes as received."""
    return timestamp.encode() + b"." + body


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Produce the hex digest a sender puts in the signature header."""
    return hmac.new(
        secret.encode(), signing_payload(timestamp, body), hashlib.sha256
    ).hexdigest()


def verify(
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    now: float | None = None,
    max_age: int = MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """Check a request. Returns (accepted, reason); the reason is for logs only."""
    if not secret:
        return False, "no shared secret configured for this source"
    if not signature:
        return False, f"missing {SIGNATURE_HEADER}"
    if not timestamp:
        return False, f"missing {TIMESTAMP_HEADER}"

    try:
        sent_at = float(timestamp)
    except ValueError:
        return False, "timestamp is not a number"

    age = (now if now is not None else time.time()) - sent_at
    if age > max_age:
        return False, f"timestamp is {age:.0f}s old, limit is {max_age}s"
    if age < -max_age:
        # A future timestamp would extend the replay window.
        return False, f"timestamp is {-age:.0f}s in the future"

    expected = sign(secret, timestamp, body)

    # Constant-time comparison, so timing reveals nothing about the digest.
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"

    return True, "ok"
