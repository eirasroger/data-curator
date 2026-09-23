"""Tests for webhook signature verification."""

from __future__ import annotations

import json
import time

from domain import webhooks

SECRET = "whsec_test_do_not_use"
NOW = 1788539384.0


def signed(body: bytes, secret: str = SECRET, at: float = NOW) -> tuple[str, str]:
    ts = str(int(at))
    return webhooks.sign(secret, ts, body), ts


def test_a_correctly_signed_request_is_accepted():
    body = json.dumps({"epd_code": "S-P-05317", "version": 2}).encode()
    sig, ts = signed(body)
    ok, reason = webhooks.verify(SECRET, body, sig, ts, now=NOW)
    assert ok, reason


def test_a_request_signed_with_the_wrong_secret_is_rejected():
    body = b'{"epd_code": "S-P-05317"}'
    sig, ts = signed(body, secret="the-wrong-secret")
    ok, reason = webhooks.verify(SECRET, body, sig, ts, now=NOW)
    assert not ok
    assert "signature mismatch" in reason


def test_a_tampered_body_is_rejected():
    """The signature covers the body, so altering it in flight invalidates it."""
    original = b'{"epd_code": "S-P-05317", "gwp_total": 11.0}'
    sig, ts = signed(original)
    tampered = b'{"epd_code": "S-P-05317", "gwp_total": 1.0}'
    ok, reason = webhooks.verify(SECRET, tampered, sig, ts, now=NOW)
    assert not ok
    assert "signature mismatch" in reason


def test_a_single_flipped_byte_is_rejected():
    body = b'{"product_id": 6}'
    sig, ts = signed(body)
    ok, _ = webhooks.verify(SECRET, b'{"product_id": 7}', sig, ts, now=NOW)
    assert not ok


def test_a_replayed_request_expires():
    """An old timestamp is rejected, which stops replays."""
    body = b'{"epd_code": "S-P-05317"}'
    sig, ts = signed(body, at=NOW)

    fresh, _ = webhooks.verify(SECRET, body, sig, ts, now=NOW + 60)
    assert fresh, "a minute old is fine"

    stale, reason = webhooks.verify(SECRET, body, sig, ts, now=NOW + 3600)
    assert not stale
    assert "old" in reason


def test_a_timestamp_from_the_future_is_rejected():
    """A timestamp far in the future is rejected."""
    body = b"{}"
    sig, ts = signed(body, at=NOW + 86400)
    ok, reason = webhooks.verify(SECRET, body, sig, ts, now=NOW)
    assert not ok
    assert "future" in reason


def test_missing_headers_are_rejected():
    body = b"{}"
    sig, ts = signed(body)
    assert not webhooks.verify(SECRET, body, None, ts, now=NOW)[0]
    assert not webhooks.verify(SECRET, body, sig, None, now=NOW)[0]


def test_unparseable_timestamp_is_rejected():
    body = b"{}"
    sig, _ = signed(body)
    ok, reason = webhooks.verify(SECRET, body, sig, "yesterday", now=NOW)
    assert not ok
    assert "not a number" in reason


def test_an_unconfigured_source_is_rejected():
    """An empty secret rejects every request."""
    body = b"{}"
    sig, ts = signed(body, secret="")
    ok, reason = webhooks.verify("", body, sig, ts, now=NOW)
    assert not ok
    assert "no shared secret" in reason


def test_the_timestamp_is_part_of_what_is_signed():
    """Changing the timestamp without re-signing invalidates the request."""
    body = b'{"epd_code": "S-P-05317"}'
    sig, _ = signed(body, at=NOW - 3600)
    ok, reason = webhooks.verify(SECRET, body, sig, str(int(NOW)), now=NOW)
    assert not ok
    assert "signature mismatch" in reason


def test_signing_hashes_raw_bytes_not_reserialised_json():
    """Re-serialised JSON fails verification; only the raw bytes match."""
    # Compact bytes; json.dumps would add spaces and change the digest.
    body = b'{"b":2,"a":1}'
    reserialised = json.dumps(json.loads(body)).encode()
    assert body != reserialised, "setup: the two encodings must differ"
    ts = str(int(NOW))
    assert webhooks.sign(SECRET, ts, body) != webhooks.sign(SECRET, ts, reserialised)


def test_signature_is_stable_for_the_same_input():
    body = b'{"x": 1}'
    ts = "1788539384"
    assert webhooks.sign(SECRET, ts, body) == webhooks.sign(SECRET, ts, body)


def test_verification_works_against_the_real_clock():
    """A fresh request passes with the default max_age and clock."""
    body = b'{"live": true}'
    ts = str(int(time.time()))
    sig = webhooks.sign(SECRET, ts, body)
    ok, reason = webhooks.verify(SECRET, body, sig, ts)
    assert ok, reason
