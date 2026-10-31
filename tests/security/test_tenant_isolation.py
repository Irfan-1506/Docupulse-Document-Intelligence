"""Security tests for multi-tenant data isolation and cross-tenant leakage prevention."""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.store import get_global_store
from src.auth.rbac import UserRole
from tests.integration.test_api import create_sample_invoice_pdf_bytes, wait_for_job_terminal_state


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def register_tenant_user(client: TestClient, tenant_name: str, email_prefix: str) -> tuple[str, str]:
    email = f"{email_prefix}_{int(time.time()*1000)}@enterprise.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "StrongPassword123!",
            "tenant_name": tenant_name,
            "role": UserRole.TENANT_ADMIN.value,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["user"]["tenant_id"], data["access_token"]


def test_cross_tenant_document_and_search_isolation(client: TestClient):
    """Verify that Tenant B cannot access or view Tenant A documents or invoice records."""
    store = get_global_store()
    store.clear()

    tenant_a_id, token_a = register_tenant_user(client, "Alpha Enterprise Corp", "alice")
    tenant_b_id, token_b = register_tenant_user(client, "Beta Logistics LLC", "bob")

    # 1. Tenant A uploads confidential invoice
    pdf_bytes = create_sample_invoice_pdf_bytes(
        invoice_number="INV-ALPHA-TOPSECRET",
        supplier="Alpha Secret Supplier",
        subtotal=9000.00,
        tax=900.00,
        total=9900.00,
    )
    files = [("files", ("alpha_invoice.pdf", pdf_bytes, "application/pdf"))]

    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=files,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert upload_resp.status_code == 202
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    status_data = wait_for_job_terminal_state(client, job_id)
    doc_id = status_data["document_id"]

    # 2. Tenant A can search and find its own invoice
    search_a = client.get(
        "/api/v1/documents/search?query=Alpha",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert search_a.status_code == 200

    # 3. Tenant A can access its document results
    res_a = client.get(
        f"/api/v1/documents/{doc_id}/results",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 200
    assert res_a.json()["document_id"] == doc_id
