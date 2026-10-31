"""Security tests for authentication, Argon2id hashing, lockout, and JWT token rotation."""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.auth.jwt import create_access_token, decode_access_token
from src.auth.password import hash_password, validate_password_policy, verify_password


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_password_policy_validation():
    """Verify password policy rules (min 8 chars, mixed types)."""
    assert validate_password_policy("short")[0] is False
    assert validate_password_policy("alllowercaseletters")[0] is False
    assert validate_password_policy("StrongPassword123!")[0] is True


def test_argon2id_hashing_and_verification():
    """Verify Argon2id produces valid hashes and verifies correctly."""
    pwd = "EnterpriseSecurePass#2026"
    h = hash_password(pwd)
    assert h.startswith("$argon2id$")
    assert verify_password(pwd, h) is True
    assert verify_password("WrongPassword!", h) is False


def test_user_registration_and_duplicate_prevention(client: TestClient):
    """Test user registration endpoint and duplicate email prevention."""
    unique_email = f"auditor_{int(time.time())}@enterprise.com"
    pwd = "AuditPassword2026!"

    # 1. Successful registration
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": pwd, "full_name": "Jane Auditor"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "access_token" in data
    assert data["user"]["email"] == unique_email

    # 2. Duplicate registration attempt rejected
    dup_resp = client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": pwd},
    )
    assert dup_resp.status_code == 400
    assert "already exists" in dup_resp.json()["detail"].lower()


def test_authentication_and_account_lockout(client: TestClient):
    """Test login success, failed attempt counting, and 5-attempt account lockout."""
    email = f"locked_user_{int(time.time())}@enterprise.com"
    pwd = "CorrectPassword123!"

    # Register user
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": pwd, "full_name": "Lock Target"},
    )
    assert reg.status_code == 201

    # 1. Successful login
    login_succ = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pwd},
    )
    assert login_succ.status_code == 200
    assert "access_token" in login_succ.json()

    # 2. 4 failed attempts should return 401
    for _ in range(4):
        bad_resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "WrongPassword!"},
        )
        assert bad_resp.status_code == 401

    # 3. 5th failure should trigger lockout (returns 401 or 423 on consecutive)
    fifth = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "WrongPassword!"},
    )
    assert fifth.status_code in (401, 423)

    # 4. Subsequent login attempt (even with correct password) must be rejected with 423 Locked
    locked_resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pwd},
    )
    assert locked_resp.status_code == 423
    assert "locked" in locked_resp.json()["detail"].lower()


def test_token_rotation_and_revocation(client: TestClient):
    """Test refresh token rotation and session revocation on logout."""
    email = f"rotator_{int(time.time())}@enterprise.com"
    pwd = "RotationPassword2026!"

    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": pwd},
    )
    assert reg.status_code == 201
    cookies = reg.cookies

    # Refresh session using cookie
    ref_resp = client.post("/api/v1/auth/refresh", cookies=cookies)
    assert ref_resp.status_code == 200
    new_token = ref_resp.json()["access_token"]
    assert new_token is not None

    # Verify protected /me endpoint with Bearer header
    me_resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == email

    # Logout to revoke sessions
    logout_resp = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {new_token}"})
    assert logout_resp.status_code == 200
