"""Integration test suite for FastAPI REST endpoints, background processing, and HITL workflow."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Generator

import fitz  # PyMuPDF
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.schemas import JobStage
from src.api.store import get_global_store


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """TestClient that enters the app lifespan context."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_store_state():
    """Clear memory store between test executions for clean isolation."""
    store = get_global_store()
    store.clear()
    yield


def create_sample_invoice_pdf_bytes(
    invoice_number: str = "INV-2026-901",
    date: str = "2026-03-15",
    supplier: str = "Acme Cloud Services",
    customer: str = "Apex Logistics Corp",
    subtotal: float = 1000.00,
    tax: float = 100.00,
    total: float = 1100.00,
) -> bytes:
    """Generate in-memory PDF invoice bytes with selectable text for ingestion testing."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    text = (
        f"INVOICE\n\n"
        f"Invoice Number: {invoice_number}\n"
        f"Date: {date}\n"
        f"Supplier: {supplier}\n"
        f"Bill To: {customer}\n\n"
        f"Description               Qty   Unit Price   Total\n"
        f"Cloud Database Hosting      1     1000.00   1000.00\n\n"
        f"Subtotal: ${subtotal:,.2f}\n"
        f"Tax (10%): ${tax:,.2f}\n"
        f"Total Due: ${total:,.2f}\n"
    )
    page.insert_text((50, 70), text, fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_sample_ledger_csv_bytes() -> bytes:
    """Generate in-memory CSV ledger bytes."""
    csv_content = (
        "Invoice Number,Vendor,Customer,Invoice Date,Total Amount\n"
        "INV-2026-901,Acme Cloud Services,Apex Logistics Corp,2026-03-15,1100.00\n"
        "INV-2026-902,Beta Networks,Global Freight Ltd,2026-03-16,2500.00\n"
    )
    return csv_content.encode("utf-8")


def wait_for_job_terminal_state(
    client: TestClient,
    job_id: str,
    timeout: float = 15.0,
    expected_stages: tuple[str, ...] = (
        JobStage.COMPLETED.value,
        JobStage.PARTIAL_SUCCESS.value,
        JobStage.FAILED.value,
    ),
) -> dict:
    """Poll job status endpoint until terminal stage is reached."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        resp = client.get(f"/api/v1/jobs/{job_id}/status")
        if resp.status_code == 200:
            data = resp.json()
            if data["stage"] in expected_stages:
                return data
        time.sleep(0.08)
    raise TimeoutError(f"Job {job_id} did not reach expected stage within {timeout}s")


# =============================================================================
# Test Cases
# =============================================================================

def test_health_check(client: TestClient):
    """Verify system health endpoint and latency middleware header."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "document-intelligence-engine"
    assert "X-Process-Time-Ms" in response.headers


def test_batch_upload_and_pipeline_execution(client: TestClient):
    """Test multi-file document upload, background queue execution, and status polling."""
    pdf_bytes = create_sample_invoice_pdf_bytes()
    csv_bytes = create_sample_ledger_csv_bytes()

    files = [
        ("files", ("invoice_acme.pdf", pdf_bytes, "application/pdf")),
        ("files", ("master_ledger.csv", csv_bytes, "text/csv")),
    ]

    upload_resp = client.post("/api/v1/documents/upload", files=files)
    assert upload_resp.status_code == 202
    upload_data = upload_resp.json()

    assert "batch_id" in upload_data
    assert upload_data["total_files"] == 2
    assert len(upload_data["jobs"]) == 2

    job_ids = [j["job_id"] for j in upload_data["jobs"]]

    # Poll status of both jobs until complete
    for jid in job_ids:
        status_data = wait_for_job_terminal_state(client, jid)
        assert status_data["stage"] in (JobStage.COMPLETED.value, JobStage.PARTIAL_SUCCESS.value)
        assert status_data["progress_percent"] == 100
        assert status_data["document_id"] is not None

    # Check batch status endpoint
    batch_resp = client.get(f"/api/v1/jobs/batches/{upload_data['batch_id']}/status")
    assert batch_resp.status_code == 200
    batch_data = batch_resp.json()
    assert batch_data["total_jobs"] == 2
    assert batch_data["completed_jobs"] == 2
    assert batch_data["failed_jobs"] == 0


def test_document_results_retrieval(client: TestClient):
    """Verify retrieval of complete document extraction and validation results."""
    pdf_bytes = create_sample_invoice_pdf_bytes(invoice_number="INV-2026-888")
    files = [("files", ("test_invoice.pdf", pdf_bytes, "application/pdf"))]

    upload_resp = client.post("/api/v1/documents/upload", files=files)
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    status_data = wait_for_job_terminal_state(client, job_id)
    doc_id = status_data["document_id"]

    results_resp = client.get(f"/api/v1/documents/{doc_id}/results")
    assert results_resp.status_code == 200
    results_data = results_resp.json()

    assert results_data["document_id"] == doc_id
    assert results_data["file_name"] == "test_invoice.pdf"
    assert results_data["extraction"] is not None
    assert len(results_data["extraction"]["records"]) >= 1

    extracted_rec = results_data["extraction"]["records"][0]
    assert extracted_rec["invoice_number"] == "INV-2026-888"
    assert extracted_rec["total"] == 1100.00

    # Validation must be present
    assert len(results_data["validation"]) >= 1
    assert results_data["validation"][0]["validation_status"] == "AUTO_VERIFIED"


def test_search_documents(client: TestClient):
    """Test full-text and field-level search queries across extracted records."""
    pdf_bytes = create_sample_invoice_pdf_bytes(
        invoice_number="INV-SEARCH-101",
        supplier="Zenith Electronics",
        total=4500.00,
    )
    files = [("files", ("zenith_inv.pdf", pdf_bytes, "application/pdf"))]

    upload_resp = client.post("/api/v1/documents/upload", files=files)
    job_id = upload_resp.json()["jobs"][0]["job_id"]
    wait_for_job_terminal_state(client, job_id)

    # Search via GET with supplier filter
    get_search = client.get("/api/v1/documents/search?supplier=Zenith")
    assert get_search.status_code == 200
    data = get_search.json()
    assert data["total"] >= 1
    assert any(item["record"]["supplier_name"] == "Zenith Electronics" for item in data["items"])

    # Search via POST with numeric range
    post_search = client.post(
        "/api/v1/documents/search",
        json={"min_total": 4000.00, "max_total": 5000.00},
    )
    assert post_search.status_code == 200
    pdata = post_search.json()
    assert pdata["total"] >= 1
    assert pdata["items"][0]["record"]["total"] == 4500.00

    # Negative search
    empty_search = client.get("/api/v1/documents/search?supplier=NonExistentCorp")
    assert empty_search.status_code == 200
    assert empty_search.json()["total"] == 0


def test_manual_correction_and_audit_trail(client: TestClient):
    """Test human reviewer correction, field mutation audit trail, and validation re-evaluation."""
    # Create invoice with intentional math mismatch: Subtotal 1000 + Tax 100 != Total 1500
    pdf_bytes = create_sample_invoice_pdf_bytes(
        invoice_number="INV-MATH-ERR",
        subtotal=1000.00,
        tax=100.00,
        total=1500.00,
    )
    files = [("files", ("math_err.pdf", pdf_bytes, "application/pdf"))]

    upload_resp = client.post("/api/v1/documents/upload", files=files)
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    status_data = wait_for_job_terminal_state(client, job_id)
    doc_id = status_data["document_id"]
    doc_results = client.get(f"/api/v1/documents/{doc_id}/results").json()
    record_id = doc_results["extraction"]["records"][0]["record_id"]

    # Initially, record should require manual review due to math error
    rec_detail_resp = client.get(f"/api/v1/records/{record_id}")
    assert rec_detail_resp.status_code == 200
    initial_detail = rec_detail_resp.json()
    assert initial_detail["validation"]["requires_manual_review"] is True

    # Apply manual correction to fix total to 1100.00
    correction_payload = {
        "corrected_by": "lead_accountant_jane",
        "correction_notes": "Corrected OCR typo on total amount to match subtotal + tax",
        "total": 1100.00,
        "mark_as_verified": True,
    }
    patch_resp = client.patch(f"/api/v1/records/{record_id}/correct", json=correction_payload)
    assert patch_resp.status_code == 200
    patched_data = patch_resp.json()

    # Verify updated values
    assert patched_data["record"]["total"] == 1100.00
    assert patched_data["validation"]["requires_manual_review"] is False
    assert patched_data["validation"]["validation_status"] == "MANUALLY_VERIFIED"

    # Verify audit log entries
    assert len(patched_data["audit_trail"]) >= 1
    total_audit = next(a for a in patched_data["audit_trail"] if a["field_changed"] == "total")
    assert float(total_audit["old_value"]) == 1500.00
    assert float(total_audit["new_value"]) == 1100.00
    assert total_audit["actor"] == "lead_accountant_jane"


def test_corrupted_file_handling_and_reprocessing(client: TestClient):
    """Test graceful isolation of corrupted uploads and reprocessing endpoint."""
    corrupted_bytes = b"CORRUPTED_NOT_A_REAL_PDF_HEADER_12345678"
    files = [("files", ("corrupt.pdf", corrupted_bytes, "application/pdf"))]

    upload_resp = client.post("/api/v1/documents/upload", files=files)
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    # Job must enter FAILED stage
    job_data = wait_for_job_terminal_state(client, job_id, expected_stages=(JobStage.FAILED.value,))
    assert job_data["stage"] == JobStage.FAILED.value
    assert job_data["error_message"] is not None

    # Test reprocessing endpoint
    reprocess_resp = client.post(f"/api/v1/jobs/{job_id}/reprocess", json={"force_ocr": True})
    assert reprocess_resp.status_code == 200
    reprocessed_data = reprocess_resp.json()
    assert reprocessed_data["retry_count"] == 1

    # Wait again and verify it handled retry
    retried_status = wait_for_job_terminal_state(client, job_id, expected_stages=(JobStage.FAILED.value,))
    assert retried_status["stage"] == JobStage.FAILED.value
    assert retried_status["retry_count"] == 1


def test_jobs_dashboard_listing(client: TestClient):
    """Test GET /api/v1/jobs with pagination and stage summary aggregation."""
    resp = client.get("/api/v1/jobs?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "summary_by_stage" in data
    assert isinstance(data["jobs"], list)


def test_serve_dashboard(client: TestClient):
    """Test serving the unified web dashboard at root URL."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "DocuPulse Enterprise AP" in resp.text
    assert "Ingestion Pipeline" in resp.text
    assert "HITL Workbench" in resp.text
