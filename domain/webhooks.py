"""Signature verification for inbound webhooks.

A webhook endpoint is reachable by anyone who learns the URL, so the signature
is the only thing separating a real sender from a stranger. Sender and receiver
hold a shared secret; the sender hashes the request with it and sends the digest
in a header; the receiver recomputes and compares.

The signed payload is `timestamp + "." + raw body`, which is the scheme Stripe
and GitHub use. Including the timestamp inside the hash is what makes a captured
request expire: without it, a valid request stays valid forever and can be
replayed indefinitely.

No network, no framework, no I/O. Everything here is testable offline.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

# How far out of date a request may be. Long enough to survive clock skew and a
# slow network, short enough that a captured request is useless by the time
# anyone gets round to replaying it.
MAX_AGE_SECONDS = 300

SIGNATURE_HEADER = "x-curator-signature"
TIMESTAMP_HEADER = "x-curator-timestamp"


def signing_payload(timestamp: str, body: bytes) -> bytes:
    """What both sides hash.

    `body` must be the exact bytes as received. The most common webhook bug is
    parsing the JSON and re-serialising it before hashing: json.dumps reorders
    keys and changes whitespace, so the digest never matches and every request
    is rejected while the code looks correct.
    """
    return timestamp.encode() + b"." + body


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Produce the hex digest a sender puts in the signature header."""
    return hmac.new(
        secret.encode(), signing_payload(timestamp, body), hashlib.sha256
    ).hexdigest()


def verify(
    secret: str,
    body: bytes,
    signature: Optional[str],
    timestamp: Optional[str],
    now: Optional[float] = None,
    max_age: int = MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """Check a request. Returns (accepted, reason).

    The reason is for logging only. It is never returned to the caller: telling
    an unauthenticated stranger whether their signature or their timestamp was
    the problem helps them fix it.
    """
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
        # A timestamp far in the future means a broken clock or a forgery
        # attempt to buy an unlimited replay window.
        return False, f"timestamp is {-age:.0f}s in the future"

    expected = sign(secret, timestamp, body)

    # compare_digest, never ==. A normal comparison returns as soon as two
    # characters differ, so the time it takes reveals how many leading
    # characters were correct, and an attacker can recover the digest one
    # character at a time. compare_digest always takes the same time.
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"

    return True, "ok"
