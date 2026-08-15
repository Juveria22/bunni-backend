"""
the request size guard

form fields are parsed before the twilio signature is checked, so this is the
only thing standing between an unauthenticated caller and unbounded buffering
"""

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    # never entered as a context manager: that would run lifespan, which wants
    # a real postgres
    return TestClient(main.app)


def test_an_ordinary_webhook_passes_through(client):
    resp = client.post("/message", data={"From": "+12015551234", "Body": "hi"})
    assert resp.status_code != 413
    assert resp.status_code != 411


def test_an_oversized_body_is_refused(client):
    resp = client.post("/message", content=b"a" * (main.MAX_REQUEST_BYTES + 1))
    assert resp.status_code == 413


def test_a_body_right_at_the_limit_is_allowed_through(client):
    resp = client.post("/message", content=b"a" * main.MAX_REQUEST_BYTES)
    assert resp.status_code != 413


def test_a_bogus_content_length_is_refused(client):
    resp = client.post(
        "/message",
        content=b"hello",
        headers={"content-length": "not-a-number"},
    )
    assert resp.status_code == 400


def test_a_body_with_no_declared_length_is_refused(client):
    """
    chunked transfer sends no content-length, which used to skip the check
    entirely and let the form be buffered unbounded
    """

    def chunks():
        yield b"a" * 1024

    resp = client.post("/message", content=chunks())
    assert resp.status_code == 411


def test_bodyless_methods_are_not_caught_by_the_length_rule(client):
    """GET carries no content-length and must not be refused"""
    assert client.get("/health").status_code in (200, 503)


def test_api_docs_are_not_served(client):
    """a public repo plus a public schema is a map for probing"""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_dead_test_endpoint_is_gone(client):
    assert client.get("/test").status_code == 404
