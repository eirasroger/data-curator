"""Tests for the webhook receiver as an HTTP endpoint.

test_webhooks.py covers the signature arithmetic. This covers the service around
it: what reaches Pub/Sub, what status codes go back, and what an unverified
caller is told.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SECRET = "whsec_test"


@pytest.fixture(scope="module")
def service(monkeypatch_session=None):
    """Import the service with its environment in place.

    Configuration is read at import time, so it has to be set first.
    """
    import os

    os.environ["GCP_PROJECT"] = "test-project"
    os.environ["PUBSUB_TOPIC"] = "test-topic"
    os.environ["WEBHOOK_SECRET_MANUFACTURER"] = SECRET
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "webhook_service", ROOT / "services" / "webhook" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFuture:
    def __init__(self, error=None):
        self._error = error

    def result(self, timeout=None):
        if self._error:
            raise self._error
        return "message-id"


class FakePublisher:
    def __init__(self, error=None):
        self.published = []
        self._error = error

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic, data, **attrs):
        self.published.append({"topic": topic, "data": data, "attrs": attrs})
        return FakeFuture(self._error)


@pytest.fixture
def publisher(service, monkeypatch):
    fake = FakePublisher()
    monkeypatch.setattr(service, "_publisher", fake)
    return fake


@pytest.fixture
def client(service):
    return TestClient(service.app)


def record() -> dict:
    """Minimal valid EPDProduct: product_id and flag are required."""
    return {
        "product_id": 6,
        "flag": 0,
        "epd_code": "S-P-05317",
        "prod_name": "SmartRoof Base",
        "date": "2031-06-30",
        "impacts": {"gwp_total": 10.3},
    }


def post(client, body: bytes, secret: str = SECRET, at: float | None = None,
         source: str = "manufacturer"):
    from domain import webhooks

    ts = str(int(at if at is not None else time.time()))
    return client.post(
        f"/webhooks/{source}", content=body,
        headers={
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: webhooks.sign(secret, ts, body),
            webhooks.TIMESTAMP_HEADER: ts,
        },
    )


def payload(rec=None) -> bytes:
    return json.dumps({"event": "epd.republished", "record": rec or record()}).encode()


# --- accepted ---------------------------------------------------------------

def test_a_signed_republication_is_queued(client, publisher):
    response = post(client, payload())
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert len(publisher.published) == 1

    message = json.loads(publisher.published[0]["data"])
    change = message["payload"]
    assert change["kind"] == "record_replacement"
    assert change["source"] == "manufacturer_feed"
    assert change["submitted_by"] == "webhook:manufacturer"
    assert change["product_id"] == 6
    assert change["replacement"]["epd_code"] == "S-P-05317"


def test_health_lists_configured_sources(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "manufacturer" in body["sources"]


# --- rejected before anything happens ---------------------------------------

def test_wrong_secret_is_401_and_publishes_nothing(client, publisher):
    assert post(client, payload(), secret="wrong").status_code == 401
    assert publisher.published == []


def test_unsigned_request_is_401(client, publisher):
    response = client.post("/webhooks/manufacturer", content=payload())
    assert response.status_code == 401
    assert publisher.published == []


def test_stale_request_is_401(client, publisher):
    assert post(client, payload(), at=time.time() - 3600).status_code == 401
    assert publisher.published == []


def test_tampered_body_is_401(client, publisher):
    """Signed one payload, sent another."""
    from domain import webhooks

    signed_body = payload()
    ts = str(int(time.time()))
    signature = webhooks.sign(SECRET, ts, signed_body)

    altered = payload(dict(record(), impacts={"gwp_total": 0.001}))
    response = client.post(
        "/webhooks/manufacturer", content=altered,
        headers={webhooks.SIGNATURE_HEADER: signature,
                 webhooks.TIMESTAMP_HEADER: ts})
    assert response.status_code == 401
    assert publisher.published == []


def test_unknown_source_is_401(client, publisher):
    assert post(client, payload(), source="acme").status_code == 401
    assert publisher.published == []


def test_rejection_reveals_nothing(client):
    """A stranger must not learn whether the signature or the clock was wrong."""
    response = post(client, payload(), secret="wrong")
    assert response.content in (b"", b"null")


# --- authenticated but wrong ------------------------------------------------

def test_a_payload_that_is_not_an_epd_is_422(client, publisher):
    """Signed correctly, so the sender is known and can be told what is wrong."""
    body = json.dumps({"record": {"prod_name": "missing required fields"}}).encode()
    response = post(client, body)
    assert response.status_code == 422
    assert "EPDProduct" in response.json()["error"]
    assert publisher.published == []


def test_missing_record_key_is_422(client, publisher):
    assert post(client, json.dumps({"event": "x"}).encode()).status_code == 422
    assert publisher.published == []


# --- downstream failure -----------------------------------------------------

def test_publish_failure_is_503(client, service, monkeypatch):
    """5xx makes the sender retry. A 2xx here would lose the event."""
    monkeypatch.setattr(service, "_publisher",
                        FakePublisher(error=RuntimeError("pubsub down")))
    assert post(client, payload()).status_code == 503
