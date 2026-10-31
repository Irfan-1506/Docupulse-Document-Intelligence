"""Unit tests for JobQueue abstractions, Redis persistence, retry backoff, and WorkerDaemon."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import fakeredis.aioredis as fake_aioredis

from src.api.queue import (
    AsyncWorkerQueue,
    JobPayload,
    NonRetryableJobError,
    QueueError,
    QueueUnavailableError,
    REDIS_KEY_DEAD_LETTER,
    REDIS_KEY_DELAYED,
    REDIS_KEY_JOB_PREFIX,
    REDIS_KEY_PROCESSING_PREFIX,
    REDIS_KEY_QUEUE,
    RedisJobQueue,
)
from src.api.schemas import JobStage, JobStatusResponse
from src.api.store import DocumentStore
from src.api.worker import WorkerDaemon


@pytest.fixture
def dummy_store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(upload_dir=tmp_path / "uploads")


@pytest.fixture
def fake_redis() -> fake_aioredis.FakeRedis:
    return fake_aioredis.FakeRedis(decode_responses=True)


class TestJobPayloadSerialization:
    """Verify job payload serialization, deserialization, and schema integrity."""

    def test_payload_roundtrip_json(self, tmp_path: Path):
        file_path = tmp_path / "invoice_test.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        payload = JobPayload(
            job_id="job_test_001",
            file_path=str(file_path),
            options={"provider": "heuristic_fallback", "force_ocr": True},
            enqueued_at=1700000000.0,
            attempt=1,
            max_attempts=3,
            idempotency_key="idem_key_001",
        )

        json_str = payload.to_json()
        restored = JobPayload.from_json(json_str)

        assert restored.job_id == payload.job_id
        assert restored.file_path == str(file_path)
        assert restored.options == payload.options
        assert restored.attempt == 1
        assert restored.max_attempts == 3
        assert restored.idempotency_key == "idem_key_001"


class TestAsyncWorkerQueue:
    """Verify local development in-memory queue behavior."""

    @pytest.mark.asyncio
    async def test_enqueue_and_process_job(self, dummy_store: DocumentStore, tmp_path: Path):
        file_path = tmp_path / "sample.pdf"
        file_path.write_bytes(b"%PDF-1.4 test file")

        dummy_store.save_job(
            JobStatusResponse(
                job_id="job_async_1",
                file_name="sample.pdf",
                file_type="pdf",
                stage=JobStage.QUEUED,
                progress_percent=0,
            ),
            file_path=file_path,
        )

        queue = AsyncWorkerQueue(store=dummy_store, concurrency=2)
        await queue.start()

        try:
            enqueued = await queue.enqueue(
                job_id="job_async_1",
                file_path=file_path,
                options={"provider": "heuristic_fallback"},
            )
            assert enqueued is True

            # Wait for queue to drain
            idle = await queue.wait_idle(timeout=5.0)
            assert idle is True

            job = dummy_store.get_job("job_async_1")
            assert job is not None
            assert job.stage in (JobStage.COMPLETED, JobStage.PARTIAL_SUCCESS, JobStage.FAILED)
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_idempotency_deduplication(self, dummy_store: DocumentStore, tmp_path: Path):
        file_path = tmp_path / "sample.pdf"
        file_path.write_bytes(b"%PDF-1.4 test file")

        queue = AsyncWorkerQueue(store=dummy_store, concurrency=1)
        await queue.start()

        try:
            first = await queue.enqueue(
                job_id="job_idem_1",
                file_path=file_path,
                idempotency_key="unique_hash_123",
            )
            assert first is True

            # Attempt duplicate enqueue with same idempotency key
            second = await queue.enqueue(
                job_id="job_idem_2",
                file_path=file_path,
                idempotency_key="unique_hash_123",
            )
            assert second is False
        finally:
            await queue.stop()


class TestRedisJobQueue:
    """Verify distributed Redis task queue, persistence, retry backoff, and DLQ."""

    @pytest.mark.asyncio
    async def test_enqueue_persists_in_redis(
        self,
        dummy_store: DocumentStore,
        fake_redis: fake_aioredis.FakeRedis,
        tmp_path: Path,
    ):
        file_path = tmp_path / "invoice.pdf"
        file_path.write_bytes(b"%PDF-1.4 sample")

        queue = RedisJobQueue(
            store=dummy_store,
            redis_url="redis://localhost:6379/0",
            concurrency=2,
            custom_client=fake_redis,
        )

        enqueued = await queue.enqueue(
            job_id="job_redis_101",
            file_path=file_path,
            options={"provider": "heuristic_fallback"},
            idempotency_key="idem_101",
            max_attempts=3,
        )
        assert enqueued is True

        # Check queue length in Redis
        q_len = await fake_redis.llen(REDIS_KEY_QUEUE)
        assert q_len == 1

        # Check job hash state in Redis
        meta = await fake_redis.hgetall(f"{REDIS_KEY_JOB_PREFIX}job_redis_101")
        assert meta["job_id"] == "job_redis_101"
        assert meta["stage"] == JobStage.QUEUED.value
        assert meta["attempt"] == "1"

        # Duplicate submission with same idempotency key is rejected
        dup = await queue.enqueue(
            job_id="job_redis_dup",
            file_path=file_path,
            idempotency_key="idem_101",
        )
        assert dup is False
        assert await fake_redis.llen(REDIS_KEY_QUEUE) == 1

    @pytest.mark.asyncio
    async def test_redis_consumer_execution_and_acknowledgement(
        self,
        dummy_store: DocumentStore,
        fake_redis: fake_aioredis.FakeRedis,
        tmp_path: Path,
    ):
        import fitz
        file_path = tmp_path / "invoice.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "INVOICE #INV-999\nDate: 2026-10-31\nSupplier: Acme Corp\nTotal: $500.00")
        doc.save(str(file_path))
        doc.close()

        dummy_store.save_job(
            JobStatusResponse(
                job_id="job_redis_ack",
                file_name="invoice.pdf",
                file_type="pdf",
                stage=JobStage.QUEUED,
                progress_percent=0,
            ),
            file_path=file_path,
        )

        queue = RedisJobQueue(
            store=dummy_store,
            redis_url="redis://localhost:6379/0",
            concurrency=1,
            custom_client=fake_redis,
            worker_id="test-worker-1",
        )

        # Enqueue job
        await queue.enqueue(
            job_id="job_redis_ack",
            file_path=file_path,
            options={"provider": "heuristic_fallback"},
        )

        # Start queue consumers
        await queue.start()

        try:
            # Wait for job to process and acknowledge
            idle = await queue.wait_idle(timeout=5.0)
            assert idle is True

            # In-flight queue must be cleared
            proc_key = f"{REDIS_KEY_PROCESSING_PREFIX}test-worker-1"
            assert await fake_redis.llen(proc_key) == 0

            # Job status in Redis must be COMPLETED or PARTIAL_SUCCESS
            meta = await fake_redis.hgetall(f"{REDIS_KEY_JOB_PREFIX}job_redis_ack")
            assert meta["stage"] in (JobStage.COMPLETED.value, JobStage.PARTIAL_SUCCESS.value)
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_redis_retry_with_exponential_backoff(
        self,
        dummy_store: DocumentStore,
        fake_redis: fake_aioredis.FakeRedis,
        tmp_path: Path,
    ):
        file_path = tmp_path / "invoice.pdf"
        file_path.write_bytes(b"%PDF-1.4 content")

        dummy_store.save_job(
            JobStatusResponse(
                job_id="job_retry_1",
                file_name="invoice.pdf",
                file_type="pdf",
                stage=JobStage.QUEUED,
                progress_percent=0,
            ),
            file_path=file_path,
        )

        queue = RedisJobQueue(
            store=dummy_store,
            redis_url="redis://localhost:6379/0",
            concurrency=1,
            max_retries=2,
            initial_retry_delay=0.1,
            backoff_factor=2.0,
            custom_client=fake_redis,
            worker_id="test-retry-worker",
        )

        # Mock process_document_job to fail once then succeed
        call_count = 0

        async def mock_process(job_id, file_path, options, store):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Transient network failure")
            dummy_store.update_job_stage(
                job_id=job_id,
                stage=JobStage.COMPLETED,
                progress_percent=100,
            )
            return JobStatusResponse(
                job_id=job_id,
                file_name="invoice.pdf",
                file_type="pdf",
                stage=JobStage.COMPLETED,
                progress_percent=100,
            )

        with patch("src.api.queue.process_document_job", side_effect=mock_process):
            await queue.enqueue(
                job_id="job_retry_1",
                file_path=file_path,
                max_attempts=2,
            )
            await queue.start()

            try:
                # Wait for retry to execute and settle
                await queue.wait_idle(timeout=5.0)
                assert call_count >= 2

                # Verify final job state in store
                job = dummy_store.get_job("job_retry_1")
                assert job is not None
                assert job.stage == JobStage.COMPLETED
            finally:
                await queue.stop()

    @pytest.mark.asyncio
    async def test_redis_dead_letter_queue_on_exhausted_retries(
        self,
        dummy_store: DocumentStore,
        fake_redis: fake_aioredis.FakeRedis,
        tmp_path: Path,
    ):
        file_path = tmp_path / "corrupted.pdf"
        file_path.write_bytes(b"invalid data")

        dummy_store.save_job(
            JobStatusResponse(
                job_id="job_dlq_1",
                file_name="corrupted.pdf",
                file_type="pdf",
                stage=JobStage.QUEUED,
                progress_percent=0,
            ),
            file_path=file_path,
        )

        queue = RedisJobQueue(
            store=dummy_store,
            redis_url="redis://localhost:6379/0",
            concurrency=1,
            max_retries=1,  # Fails immediately into DLQ
            custom_client=fake_redis,
            worker_id="test-dlq-worker",
        )

        async def mock_fatal_process(job_id, file_path, options, store):
            raise QueueError("Unrecoverable data error")

        with patch("src.api.queue.process_document_job", side_effect=mock_fatal_process):
            await queue.enqueue(
                job_id="job_dlq_1",
                file_path=file_path,
                max_attempts=1,
            )
            await queue.start()

            try:
                await queue.wait_idle(timeout=5.0)

                # Verify Dead-Letter Queue contains the failed job
                dlq_len = await fake_redis.llen(REDIS_KEY_DEAD_LETTER)
                assert dlq_len == 1

                dlq_payload_raw = await fake_redis.lindex(REDIS_KEY_DEAD_LETTER, 0)
                dlq_payload = json.loads(dlq_payload_raw)
                assert dlq_payload["job_id"] == "job_dlq_1"

                # Verify store state is marked FAILED
                job = dummy_store.get_job("job_dlq_1")
                assert job is not None
                assert job.stage == JobStage.FAILED
            finally:
                await queue.stop()

    @pytest.mark.asyncio
    async def test_redis_unreachable_raises_error(self, dummy_store: DocumentStore):
        queue = RedisJobQueue(
            store=dummy_store,
            redis_url="redis://nonexistent-host:9999/0",
        )

        with pytest.raises(QueueUnavailableError):
            await queue.start()


class TestWorkerDaemon:
    """Verify standalone worker process lifecycle and clean signal shutdown."""

    @pytest.mark.asyncio
    async def test_worker_daemon_start_and_shutdown(
        self,
        dummy_store: DocumentStore,
        fake_redis: fake_aioredis.FakeRedis,
    ):
        daemon = WorkerDaemon(
            store=dummy_store,
            redis_url="redis://localhost:6379/0",
            concurrency=2,
            worker_id="test-daemon-worker",
        )

        # Inject fake client to avoid live socket
        daemon.queue._client = fake_redis  # type: ignore

        daemon_task = asyncio.create_task(daemon.run())
        await asyncio.sleep(0.1)
        assert daemon._running is True

        # Signal shutdown
        daemon.trigger_shutdown()
        await asyncio.wait_for(daemon_task, timeout=3.0)
        assert daemon._running is False
