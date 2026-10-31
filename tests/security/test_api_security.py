"""Security tests for HTTP defense headers, CORS policies, and information leakage prevention."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_defense_in_depth_security_headers(client: TestClient):
    """Verify that all enterprise security headers are enforced across responses (SEC-08)."""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200

    headers = resp.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
    assert "geolocation=()" in headers.get("Permissions-Policy", "")


def test_cors_options_preflight(client: TestClient):
    """Verify CORS preflight handling for allowed cross-origin requests."""
    resp = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


def test_404_error_sanitization_no_stack_traces(client: TestClient):
    """Verify that not-found or malformed requests do not leak server stack traces or Python internals."""
    resp = client.get("/api/v1/non_existent_endpoint_for_security_probe")
    assert resp.status_code == 404
    body = resp.text
    assert "Traceback" not in body
    assert "aiosqlite" not in body
    assert "sqlalchemy" not in body
