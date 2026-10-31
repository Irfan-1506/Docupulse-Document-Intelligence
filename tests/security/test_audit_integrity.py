"""Security tests for reviewer audit trail integrity, actor spoofing prevention, and hash chaining."""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from src.api.main import app
from src.api.store import get_global_store
from src.auth.rbac import UserRole
from src.db.models import AuditLogModel
from src.db.session import get_session_factory
from tests.integration.test_api import create_sample_invoice_pdf_bytes, wait_for_job_terminal_state


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def register_user(client: TestClient, role: str, email_prefix: str = "auditor") -> tuple[str, str]:
    email = f"{email_prefix}_{int(time.time()*1000)}@enterprise.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "StrongPassword123!",
            "role": role,
        },
    )
    assert resp.status_code == 201
    return email, resp.json()["access_token"]


def test_reviewer_spoofing_prevented_and_actor_derived_from_session(client: TestClient):
    """Verify that client-supplied `corrected_by` is discarded in favor of authenticated session email (SEC-04)."""
    store = get_global_store()
    store.clear()

    # 1. Register admin to upload document
    _, admin_token = register_user(client, UserRole.TENANT_ADMIN.value, "admin")

    pdf_bytes = create_sample_invoice_pdf_bytes(
        invoice_number="INV-AUDIT-001",
        subtotal=500.00,
        tax=50.00,
        total=550.00,
    )
    files = [("files", ("invoice.pdf", pdf_bytes, "application/pdf"))]

    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=files,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert upload_resp.status_code == 202
    job_id = upload_resp.json()["jobs"][0]["job_id"]
    status_data = wait_for_job_terminal_state(client, job_id)
    doc_id = status_data["document_id"]

    results = client.get(f"/api/v1/documents/{doc_id}/results").json()
    record_id = results["extraction"]["records"][0]["record_id"]

    # 2. Register reviewer to submit correction
    reviewer_email, reviewer_token = register_user(client, UserRole.DOCUMENT_REVIEWER.value, "rev")

    # Reviewer submits correction with a SPOOFED corrected_by name
    spoofed_payload = {
        "corrected_by": "chief_financial_officer_boss@company.com",  # FAKE IDENTITY
        "correction_notes": "Legitimate tax adjustment",
        "tax": 55.00,
        "total": 555.00,
    }

    correct_resp = client.patch(
        f"/api/v1/records/{record_id}/correct",
        json=spoofed_payload,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert correct_resp.status_code == 200
    patched_data = correct_resp.json()

    # Verify that the audit entry recorded the REAL authenticated email, NOT the spoofed name!
    audits = patched_data["audit_trail"]
    assert len(audits) >= 1
    for entry in audits:
        assert entry["actor"] == reviewer_email
        assert entry["actor"] != "chief_financial_officer_boss@company.com"


def test_cryptographic_hash_chain_verification_and_tamper_detection(client: TestClient):
    """Verify unbroken SHA-256 hash chaining and direct DB mutation tamper detection (SEC-11)."""
    store = get_global_store()
    store.clear()

    _, admin_token = register_user(client, UserRole.TENANT_ADMIN.value, "admin_chain")

    pdf_bytes = create_sample_invoice_pdf_bytes(invoice_number="INV-CHAIN-001")
    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=[("files", ("test.pdf", pdf_bytes, "application/pdf"))],
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert upload_resp.status_code == 202
    job_id = upload_resp.json()["jobs"][0]["job_id"]
    status_data = wait_for_job_terminal_state(client, job_id)
    doc_id = status_data["document_id"]

    results = client.get(f"/api/v1/documents/{doc_id}/results").json()
    record_id = results["extraction"]["records"][0]["record_id"]

    _, rev_token = register_user(client, UserRole.DOCUMENT_REVIEWER.value, "chain_rev")

    # 1. Apply two sequential corrections to form a 2-link chain
    client.patch(
        f"/api/v1/records/{record_id}/correct",
        json={"subtotal": 1200.00, "correction_notes": "First edit"},
        headers={"Authorization": f"Bearer {rev_token}"},
    )
    client.patch(
        f"/api/v1/records/{record_id}/correct",
        json={"tax": 120.00, "total": 1320.00, "correction_notes": "Second edit"},
        headers={"Authorization": f"Bearer {rev_token}"},
    )

    # 2. Check hash chain verification endpoint -> Must be valid
    verify_resp = client.get(
        f"/api/v1/records/{record_id}/audit/verify",
        headers={"Authorization": f"Bearer {rev_token}"},
    )
    assert verify_resp.status_code == 200
    chain_data = verify_resp.json()
    assert chain_data["is_valid"] is True
    assert chain_data["tamper_detected"] is False

    # 3. Simulate malicious database tampering: attacker directly changes new_value of an audit log in DB
    session_factory = get_session_factory()
    import asyncio

    async def tamper():
        async with session_factory() as s:
            await s.execute(
                update(AuditLogModel)
                .where(AuditLogModel.record_id == record_id)
                .values(new_value="MALICIOUS_TAMPERED_VALUE")
            )
            await s.commit()

    asyncio.run(tamper())

    # 4. Check verification endpoint again -> MUST detect tampering!
    tamper_check = client.get(
        f"/api/v1/records/{record_id}/audit/verify",
        headers={"Authorization": f"Bearer {rev_token}"},
    )
    assert tamper_check.status_code == 200
    tampered_data = tamper_check.json()
    assert tampered_data["is_valid"] is False
    assert tampered_data["tamper_detected"] is True
    assert "Tampered" in tampered_data["details"] or "Broken" in tampered_data["details"]
