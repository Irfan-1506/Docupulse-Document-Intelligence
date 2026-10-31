"""Integration test suite for the distributed Redis job queue and standalone worker daemon.

Verifies:
1. End-to-end pipeline: Job enqueue in Redis -> standalone worker consumption -> database persistence -> API status COMPLETED.
2. Unreachable Redis failure handling: HTTP 503 Service Unavailable when Redis is down.
3. Corrupted document quarantine: Non-retryable errors immediately moved to Dead-Letter Queue (DLQ).
4. Distributed idempotency: Duplicate submissions with identical keys are rejected / de-duplicated.
5. Standalone WorkerDaemon graceful shutdown and state settlement.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
import pytest
import fakeredis.aioredis
import fitz
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.queue import (
    REDIS_KEY_DEAD_LETTER,
    REDIS_KEY_QUEUE,
    NonRetryableJobError,
    QueueUnavailableError,
    RedisJobQueue,
    reset_global_queue,
)
from src.api.schemas import JobStage, JobStatusResponse
from src.api.store import DocumentStore, get_global_store
from src.api.worker import WorkerDaemon


def create_sample_invoice_pdf(path: Path, invoice_number: str = "INV-REDIS-999") -> Path:
    """Create a minimal valid single-page PDF invoice file on disk."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    text = (
        f"INVOICE\n\n"
        f"Invoice Number: {invoice_number}\n"
        f"Date: 2026-04-01\n"
        f"Supplier: Distributed Cloud Inc\n"
        f"Bill To: Enterprise AP Corp\n\n"
        f"Description               Qty   Unit Price   Total\n"
        f"Serverless Worker Unit      2      500.00   1000.00\n\n"
        f"Subtotal: $1,000.00\n"
        f"Tax (10%): $100.00\n"
        f"Total Due: $1,100.00\n"
    )
    page.insert_text((50, 70), text, fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def fake_redis():
    """Create an isolated FakeRedis client instance."""
    server = fakeredis.FakeServer()
    client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return client


@pytest.fixture
def test_store(tmp_path):
    """Create an isolated test DocumentStore."""
    store = DocumentStore(upload_dir=tmp_path)
    return store


@pytest.mark.asyncio
async def test_end_to_end_redis_worker_pipeline(tmp_path, fake_redis, monkeypatch):
    """Test full flow: Redis enqueue -> WorkerDaemon consumption -> DB persistence -> Completion."""
    store = DocumentStore(upload_dir=tmp_path)
    queue = RedisJobQueue(store=store, redis_url="redis://fake-redis:6379/0", concurrency=1)

    # Monkeypatch get_client to return our fake_redis instance
    async def mock_get_client():
        return fake_redis

    monkeypatch.setattr(queue, "get_client", mock_get_client)

    # 1. Create a valid test invoice
    unique_inv = f"INV-E2E-{uuid.uuid4().hex[:6]}"
    invoice_path = tmp_path / "test_invoice.pdf"
    create_sample_invoice_pdf(invoice_path, invoice_number=unique_inv)

    job_id = f"job-e2e-redis-{uuid.uuid4().hex[:6]}"
    job = JobStatusResponse(
        job_id=job_id,
        batch_id="batch-e2e-001",
        file_name="test_invoice.pdf",
        file_type="PDF",
        stage=JobStage.QUEUED,
        progress_percent=0,
        pages_processed=0,
        total_pages=0,
        idempotency_key=f"idemp-{unique_inv}",
    )
    options = {
        "force_ocr": False,
        "provider_override": "heuristic",
        "original_filename": "test_invoice.pdf",
        "idempotency_key": f"idemp-{unique_inv}",
    }
    store.save_job(job=job, file_path=invoice_path, options=options)

    # 2. Enqueue into Redis
    enqueued = await queue.enqueue(
        job_id=job_id,
        file_path=invoice_path,
        options=options,
        idempotency_key=f"idemp-{unique_inv}",
    )
    assert enqueued is True

    # Verify job is sitting in Redis queue
    queue_len = await fake_redis.llen(REDIS_KEY_QUEUE)
    assert queue_len == 1

    # 3. Spin up WorkerDaemon
    daemon = WorkerDaemon(queue=queue, store=store, concurrency=1)
    await daemon.start()

    # Wait for queue to drain and job to complete
    idle = await queue.wait_idle(timeout=10.0)
    assert idle is True

    await daemon.stop()

    # 4. Verify terminal status in store
    settled_job = store.get_job(job_id)
    assert settled_job is not None
    assert settled_job.stage == JobStage.COMPLETED
    assert settled_job.progress_percent == 100
    assert settled_job.document_id is not None

    # 5. Verify results persisted in repository
    results = store.get_document_results(settled_job.document_id)
    assert results is not None
    assert results.extraction is not None
    assert results.extraction.records[0].invoice_number == unique_inv
    assert results.extraction.records[0].total == 1100.00
    assert len(results.validation) > 0
    assert results.validation[0].validation_status in ("AUTO_VERIFIED", "NEEDS_MANUAL_REVIEW")


@pytest.mark.asyncio
async def test_redis_corrupted_document_quarantined_to_dlq(tmp_path, fake_redis, monkeypatch):
    """Test that a non-retryable corrupted document is immediately quarantined to the Dead-Letter Queue."""
    store = DocumentStore(upload_dir=tmp_path)
    queue = RedisJobQueue(store=store, redis_url="redis://fake-redis:6379/0", concurrency=1)

    async def mock_get_client():
        return fake_redis

    monkeypatch.setattr(queue, "get_client", mock_get_client)

    # Create an invalid corrupted file pretending to be a PDF
    corrupt_path = tmp_path / "corrupted.pdf"
    corrupt_path.write_bytes(b"CORRUPTED_GARBAGE_NOT_A_REAL_PDF_DOCUMENT_123456789")

    job_id = "job-corrupted-dlq-001"
    job = JobStatusResponse(
        job_id=job_id,
        batch_id="batch-corrupt-001",
        file_name="corrupted.pdf",
        file_type="PDF",
        stage=JobStage.QUEUED,
        progress_percent=0,
        pages_processed=0,
        total_pages=0,
    )
    options = {
        "force_ocr": False,
        "provider_override": "heuristic",
        "original_filename": "corrupted.pdf",
    }
    store.save_job(job=job, file_path=corrupt_path, options=options)

    await queue.enqueue(job_id=job_id, file_path=corrupt_path, options=options)

    # Run WorkerDaemon
    daemon = WorkerDaemon(queue=queue, store=store, concurrency=1)
    await daemon.start()

    await queue.wait_idle(timeout=10.0)
    await daemon.stop()

    # Verify job stage is FAILED
    failed_job = store.get_job(job_id)
    assert failed_job is not None
    assert failed_job.stage == JobStage.FAILED
    assert failed_job.error_message is not None

    # Verify that the job was sent to the Dead-Letter Queue
    dlq_count = await fake_redis.llen(REDIS_KEY_DEAD_LETTER)
    assert dlq_count == 1
    dlq_raw = await fake_redis.rpop(REDIS_KEY_DEAD_LETTER)
    assert job_id in dlq_raw


@pytest.mark.asyncio
async def test_redis_idempotency_duplicate_protection(tmp_path, fake_redis, monkeypatch):
    """Test that duplicate jobs with identical idempotency keys are prevented."""
    store = DocumentStore(upload_dir=tmp_path)
    queue = RedisJobQueue(store=store, redis_url="redis://fake-redis:6379/0", concurrency=1)

    async def mock_get_client():
        return fake_redis

    monkeypatch.setattr(queue, "get_client", mock_get_client)

    invoice_path = tmp_path / "invoice.pdf"
    create_sample_invoice_pdf(invoice_path, invoice_number="INV-IDEMP-001")

    options = {
        "force_ocr": False,
        "provider_override": "heuristic",
        "original_filename": "invoice.pdf",
        "idempotency_key": "IDEMP-KEY-UNIQUE-ABC",
    }

    # 1. First enqueue succeeds
    success_first = await queue.enqueue(
        job_id="job-first",
        file_path=invoice_path,
        options=options,
        idempotency_key="IDEMP-KEY-UNIQUE-ABC",
    )
    assert success_first is True

    # 2. Second enqueue with same key is rejected
    success_second = await queue.enqueue(
        job_id="job-second",
        file_path=invoice_path,
        options=options,
        idempotency_key="IDEMP-KEY-UNIQUE-ABC",
    )
    assert success_second is False

    # Verify only one job sits in the queue
    q_len = await fake_redis.llen(REDIS_KEY_QUEUE)
    assert q_len == 1


def test_api_upload_unreachable_redis_returns_503(monkeypatch):
    """Test that when Redis is unreachable, uploading returns HTTP 503 instead of falling back silently."""
    reset_global_queue()
    monkeypatch.setenv("USE_REDIS_QUEUE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:65530/0")

    with TestClient(app) as client:
        # Generate dummy PDF upload
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 70), "INVOICE\nTotal: $100.00", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        response = client.post(
            "/api/v1/documents/upload",
            files={"files": ("test.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 503
        data = response.json()
        assert "Queue backend is unavailable" in data["detail"]

    reset_global_queue()
