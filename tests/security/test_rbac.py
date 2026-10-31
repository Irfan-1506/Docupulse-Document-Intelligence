"""Security tests for Role-Based Access Control (RBAC) and least privilege enforcement."""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.auth.rbac import Permission, UserRole, has_permission


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def register_user_token(client: TestClient, role: UserRole, email_prefix: str = "user") -> str:
    """Helper to register a real DB user with specified role and return valid access token."""
    email = f"{email_prefix}_{int(time.time()*1000)}@enterprise.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "StrongPassword123!",
            "full_name": f"Test {role.value}",
            "role": role.value,
        },
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


def test_rbac_role_permission_matrix():
    """Verify role-to-permission mapping rules."""
    assert has_permission(UserRole.SUPER_ADMIN, Permission.DOCUMENT_CREATE) is True
    assert has_permission(UserRole.SUPER_ADMIN, Permission.USER_MANAGE) is True

    assert has_permission(UserRole.DOCUMENT_REVIEWER, Permission.DOCUMENT_REVIEW) is True
    assert has_permission(UserRole.DOCUMENT_REVIEWER, Permission.USER_MANAGE) is False

    assert has_permission(UserRole.VIEWER, Permission.DOCUMENT_READ) is True
    assert has_permission(UserRole.VIEWER, Permission.DOCUMENT_CREATE) is False
    assert has_permission(UserRole.VIEWER, Permission.DOCUMENT_REVIEW) is False


def test_viewer_role_cannot_upload_or_correct(client: TestClient):
    """Verify that a user with VIEWER role is blocked with 403 on upload and correction."""
    viewer_token = register_user_token(client, UserRole.VIEWER, email_prefix="viewer")

    # 1. Attempt upload -> Must be 403 Forbidden
    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=[("files", ("test.pdf", b"%PDF-1.4 sample", "application/pdf"))],
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert upload_resp.status_code == 403
    assert "Forbidden" in upload_resp.json()["detail"]

    # 2. Attempt correction -> Must be 403 Forbidden
    correct_resp = client.patch(
        "/api/v1/records/non-existent-rec/correct",
        json={"total": 999.00},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert correct_resp.status_code == 403
    assert "Forbidden" in correct_resp.json()["detail"]


def test_reviewer_role_can_review_but_not_manage_users(client: TestClient):
    """Verify that a reviewer can review records but cannot perform tenant admin tasks."""
    reviewer_token = register_user_token(client, UserRole.DOCUMENT_REVIEWER, email_prefix="rev")

    # Reviewer calling /auth/me should succeed and display DOCUMENT_REVIEWER
    me_resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {reviewer_token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == UserRole.DOCUMENT_REVIEWER.value
    assert "document:review" in me_resp.json()["permissions"]
    assert "tenant:manage" not in me_resp.json()["permissions"]
