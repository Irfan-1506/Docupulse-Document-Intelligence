"""Comprehensive integration test suite verifying FastAPI REST endpoints, background queue, and database persistence."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Generator

import fitz
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.schemas import JobStage
from src.api.store import get_global_store
from src.db.repository import SQLAlchemyRepository
from src.db.session import get_session_factory, init_db


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """TestClient entering FastAPI lifespan."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure in-memory store and DB state are clean for every test."""
    store = get_global_store()
    store.clear()
    yield


def generate_sample_pdf_bytes(
    invoice_num: str = "INV-E2E-100",
    date: str = "2026-03-25",
    supplier: str = "Vertex Supply Chain Solutions",
    subtotal: float = 2000.00,
    tax: float = 200.00,
    total: float = 2200.00,
) -> bytes:
    """Generate in-memory sample PDF bytes."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    text = (
        f"COMMERCIAL INVOICE\n\n"
        f"Supplier: {supplier}\n"
        f"Customer: Acme Global Operations\n"
        f"Invoice Number: {invoice_num}\n"
        f"Invoice Date: {date}\n"
        f"Currency: USD\n\n"
        f"Item: Warehouse Logistics Fleet Operations | Qty: 1 | Amount: ${subtotal:.2f}\n"
        f"Subtotal: ${subtotal:.2f}\n"
        f"Tax (10%): ${tax:.2f}\n"
        f"Total: ${total:.2f}\n"
    )
    page.insert_text(fitz.Point(50, 70), text, fontsize=11)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def wait_for_job(client: TestClient, job_id: str, max_seconds: float = 10.0) -> dict:
    """Poll job status until it reaches a terminal state."""
    start = time.time()
    while time.time() - start < max_seconds:
        resp = client.get(f"/api/v1/jobs/{job_id}/status")
        if resp.status_code == 200:
            data = resp.json()
            if data["stage"] in (JobStage.COMPLETED.value, JobStage.FAILED.value):
                return data
        time.sleep(0.15)
    raise TimeoutError(f"Job {job_id} did not finish within {max_seconds} seconds")


def test_api_db_full_lifecycle_and_search(client: TestClient):
    """Test full document lifecycle: upload -> background worker -> DB persistence -> search -> correction."""
    pdf_bytes = generate_sample_pdf_bytes(
        invoice_num="INV-E2E-777",
        supplier="Apex Global Cloud Tech",
        subtotal=1200.00,
        tax=120.00,
        total=1320.00,
    )

    # 1. POST upload
    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=[("files", ("invoice_e2e_777.pdf", pdf_bytes, "application/pdf"))],
        data={"force_ocr": "false"},
    )
    assert upload_resp.status_code == 202
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    # 2. Wait for background completion
    job_status = wait_for_job(client, job_id)
    assert job_status["stage"] == JobStage.COMPLETED.value
    assert job_status["progress_percent"] == 100
    doc_id = job_status["document_id"]
    assert doc_id is not None

    # 3. Retrieve document results
    results_resp = client.get(f"/api/v1/documents/{doc_id}/results")
    assert results_resp.status_code == 200
    doc_res = results_resp.json()
    assert doc_res["document_id"] == doc_id
    assert doc_res["extraction"] is not None
    assert len(doc_res["extraction"]["records"]) == 1
    record = doc_res["extraction"]["records"][0]
    record_id = record["record_id"]
    assert record["invoice_number"] == "INV-E2E-777"
    assert record["total"] == 1320.00

    # 4. Hybrid Search via API
    search_resp = client.post(
        "/api/v1/documents/search",
        json={
            "query": "Apex Global Cloud Tech logistics",
            "supplier": "Apex",
            "page_size": 5,
        },
    )
    assert search_resp.status_code == 200
    search_data = search_resp.json()
    assert search_data["total"] >= 1
    matched = search_data["items"][0]
    assert "Apex" in (matched["record"]["supplier_name"] or "")

    # 5. Manual Correction with Audit Logging & Re-validation
    correct_resp = client.patch(
        f"/api/v1/records/{record_id}/correct",
        json={
            "total": 1350.00,
            "corrected_by": "senior_auditor_mark",
            "correction_notes": "Verified against purchase order contract",
            "mark_as_verified": True,
        },
    )
    assert correct_resp.status_code == 200
    corrected = correct_resp.json()
    assert corrected["validation"]["validation_status"] == "MANUALLY_VERIFIED"
    assert len(corrected["audit_trail"]) >= 1
    total_audit = next(a for a in corrected["audit_trail"] if a["field_changed"] == "total")
    assert total_audit["actor"] == "senior_auditor_mark"
    assert float(total_audit["new_value"]) == 1350.00


def test_api_db_corrupted_file_handling(client: TestClient):
    """Test API handling of corrupted upload and failed-job reprocessing."""
    corrupt_data = b"NOT_A_VALID_PDF_HEADER_CORRUPTED_12345"
    upload_resp = client.post(
        "/api/v1/documents/upload",
        files=[("files", ("corrupt_test.pdf", corrupt_data, "application/pdf"))],
    )
    assert upload_resp.status_code == 202
    job_id = upload_resp.json()["jobs"][0]["job_id"]

    job_status = wait_for_job(client, job_id)
    assert job_status["stage"] == JobStage.FAILED.value
    assert job_status["error_message"] is not None

    # Reprocess request
    reprocess_resp = client.post(f"/api/v1/jobs/{job_id}/reprocess", json={"force_ocr": True})
    assert reprocess_resp.status_code == 200
    assert reprocess_resp.json()["retry_count"] == 1
