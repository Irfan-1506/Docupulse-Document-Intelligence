"""Dual-mode background job queue: Production Redis distributed queue + AsyncWorkerQueue fallback.

Supports:
- Asynchronous Redis Streams / Reliable Lists pattern (LPUSH + BRPOPLPUSH)
- JSON job payload serialization & persistence in Redis hashes
- Worker heartbeat and orphaned job recovery
- Exponential backoff retries with dead-letter queue (DLQ)
- Distributed idempotency locks
- Clean development in-memory queue fallback when Redis is unconfigured
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnError, RedisError

from src.api.schemas import JobStage, JobStatusResponse
from src.api.store import DocumentStore, get_global_store
from src.api.worker import process_document_job

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions & Constants
# ---------------------------------------------------------------------------

class QueueError(Exception):
    """Base exception for job queue operations."""
    pass


class QueueUnavailableError(QueueError):
    """Raised when the configured queue backend cannot be reached."""
    pass


class NonRetryableJobError(QueueError):
    """Raised when an unrecoverable error occurs (e.g. corrupted file, invalid format)."""
    pass


REDIS_KEY_QUEUE = "docupulse:queue:jobs"
REDIS_KEY_PROCESSING_PREFIX = "docupulse:queue:processing:"
REDIS_KEY_DELAYED = "docupulse:queue:delayed"
REDIS_KEY_DEAD_LETTER = "docupulse:queue:dead_letter"
REDIS_KEY_JOB_PREFIX = "docupulse:job:"
REDIS_KEY_IDEMPOTENCY_PREFIX = "docupulse:idempotency:"
REDIS_KEY_WORKER_HEARTBEAT_PREFIX = "docupulse:worker:heartbeat:"


@dataclass
class JobPayload:
    """Serializable payload for background document jobs."""
    job_id: str
    file_path: str
    options: Dict[str, Any]
    enqueued_at: float
    attempt: int = 1
    max_attempts: int = 3
    idempotency_key: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data_str: str) -> JobPayload:
        data = json.loads(data_str)
        return cls(**data)


# ---------------------------------------------------------------------------
# Abstract Queue Interface
# ---------------------------------------------------------------------------

class BaseJobQueue(ABC):
    """Abstract interface for background document processing queues."""

    @abstractmethod
    async def start(self) -> None:
        """Initialize queue resources and start worker consumers."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully drain or cancel active workers."""
        pass

    @abstractmethod
    async def enqueue(
        self,
        job_id: str,
        file_path: Path,
        options: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> bool:
        """Submit a job to the background processing queue.

        Returns True if enqueued, or False if skipped due to idempotency.
        """
        pass

    @abstractmethod
    async def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until all queued jobs complete (primarily for test synchronization)."""
        pass

    @abstractmethod
    async def get_queue_depth(self) -> int:
        """Return number of pending jobs in queue."""
        pass


# ---------------------------------------------------------------------------
# In-Memory Queue (Development & Local Test Fallback)
# ---------------------------------------------------------------------------

class AsyncWorkerQueue(BaseJobQueue):
    """Zero-config, in-process async worker queue.

    Enables local testing, automated CI/CD pipelines, and standalone deployment
    without requiring an external Redis daemon.
    """

    def __init__(self, store: DocumentStore, concurrency: int = 4) -> None:
        self.store = store
        self.concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[JobPayload] = asyncio.Queue()
        self._workers: List[asyncio.Task[None]] = []
        self._running = False
        self._seen_idempotency: Dict[str, str] = {}

    async def start(self) -> None:
        """Start worker coroutines."""
        if self._running:
            return
        loop = asyncio.get_running_loop()
        if getattr(self._queue, "_loop", None) is not loop:
            self._queue = asyncio.Queue()

        self._running = True
        self._workers = [
            asyncio.create_task(self._worker_loop(idx), name=f"doc-worker-{idx}")
            for idx in range(self.concurrency)
        ]
        logger.info(f"Started AsyncWorkerQueue with {self.concurrency} concurrent workers.")

    async def stop(self) -> None:
        """Stop worker coroutines and wait for cancellation."""
        if not self._running:
            return
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Stopped AsyncWorkerQueue.")

    async def enqueue(
        self,
        job_id: str,
        file_path: Path,
        options: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> bool:
        """Add job to the local queue with idempotency protection."""
        if idempotency_key and idempotency_key in self._seen_idempotency:
            logger.info(f"Job {job_id} deduplicated via idempotency key: {idempotency_key}")
            return False

        if idempotency_key:
            self._seen_idempotency[idempotency_key] = job_id

        payload = JobPayload(
            job_id=job_id,
            file_path=str(file_path.resolve()),
            options=options or {},
            enqueued_at=time.time(),
            attempt=1,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
        )
        await self._queue.put(payload)
        logger.info(f"Enqueued job {job_id} into AsyncWorkerQueue.")
        return True

    async def wait_idle(self, timeout: Optional[float] = 30.0) -> bool:
        """Wait until all current queue items are processed."""
        try:
            if timeout:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            else:
                await self._queue.join()
            return True
        except asyncio.TimeoutError:
            logger.warning("wait_idle timed out waiting for queue to drain.")
            return False

    async def get_queue_depth(self) -> int:
        return self._queue.qsize()

    async def _worker_loop(self, worker_id: int) -> None:
        """Continuous consumer loop for an individual worker task."""
        while self._running:
            try:
                payload = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                logger.debug(f"[Worker-{worker_id}] Processing job {payload.job_id} (attempt {payload.attempt})")
                await process_document_job(
                    job_id=payload.job_id,
                    file_path=Path(payload.file_path),
                    options=payload.options,
                    store=self.store,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(f"[Worker-{worker_id}] Error processing job {payload.job_id}: {exc}")
                if payload.attempt < payload.max_attempts and not isinstance(exc, NonRetryableJobError):
                    payload.attempt += 1
                    logger.info(f"Retrying job {payload.job_id} (attempt {payload.attempt}/{payload.max_attempts})")
                    self.store.update_job_stage(
                        job_id=payload.job_id,
                        stage=JobStage.RETRYING,
                        progress_percent=0,
                        error_message=f"Attempt {payload.attempt - 1} failed: {exc}",
                    )
                    await asyncio.sleep(1.0)
                    await self._queue.put(payload)
                else:
                    self.store.update_job_stage(
                        job_id=payload.job_id,
                        stage=JobStage.FAILED,
                        progress_percent=100,
                        error_message=f"Worker exception after {payload.attempt} attempts: {exc}",
                    )
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Production Distributed Redis Job Queue
# ---------------------------------------------------------------------------

class RedisJobQueue(BaseJobQueue):
    """Production Redis distributed task queue.

    Implements:
    - LPUSH / BRPOPLPUSH reliable queue pattern
    - Persistent hash state per job
    - Worker heartbeats and orphan recovery
    - Exponential backoff retry loop via Redis sorted sets
    - Dead-letter queue (DLQ) for unrecoverable errors
    - Distributed idempotency locks
    """

    def __init__(
        self,
        store: DocumentStore,
        redis_url: str,
        concurrency: int = 4,
        max_retries: int = 3,
        initial_retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        worker_id: Optional[str] = None,
        custom_client: Optional[aioredis.Redis] = None,
    ) -> None:
        self.store = store
        self.redis_url = redis_url
        self.concurrency = max(1, concurrency)
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.backoff_factor = backoff_factor
        self.worker_id = worker_id or f"worker-{os.getpid()}-{int(time.time() * 1000) % 100000}"

        self._client: Optional[aioredis.Redis] = custom_client
        self._pool: Optional[ConnectionPool] = None
        self._running = False
        self._consumer_tasks: List[asyncio.Task[None]] = []
        self._refeeder_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._processing_key = f"{REDIS_KEY_PROCESSING_PREFIX}{self.worker_id}"

    async def get_client(self) -> aioredis.Redis:
        """Retrieve or initialize the active Redis async client."""
        if self._client is None:
            self._pool = ConnectionPool.from_url(
                self.redis_url,
                decode_responses=True,
                max_connections=max(10, self.concurrency * 2 + 5),
            )
            self._client = aioredis.Redis(connection_pool=self._pool)
        return self._client

    async def start(self) -> None:
        """Connect to Redis, verify health, recover orphaned jobs, and launch consumers."""
        if self._running:
            return

        client = await self.get_client()
        try:
            await client.ping()
            logger.info(f"Connected to Redis at {self.redis_url}. Worker ID: {self.worker_id}")
        except (RedisConnError, RedisError, OSError) as exc:
            err_msg = f"Failed to connect to Redis at {self.redis_url}: {exc}"
            logger.error(err_msg)
            raise QueueUnavailableError(err_msg) from exc

        self._running = True

        # 1. Recover any unacknowledged jobs left from previous crashes of this worker ID
        await self._recover_orphaned_jobs(client)

        # 2. Launch background delayed job refeeder (for retries)
        self._refeeder_task = asyncio.create_task(
            self._delayed_refeeder_loop(),
            name=f"{self.worker_id}-refeeder",
        )

        # 3. Launch worker heartbeat
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"{self.worker_id}-heartbeat",
        )

        # 4. Launch consumer worker coroutines
        self._consumer_tasks = [
            asyncio.create_task(
                self._consumer_loop(idx),
                name=f"{self.worker_id}-consumer-{idx}",
            )
            for idx in range(self.concurrency)
        ]
        logger.info(f"RedisJobQueue started with {self.concurrency} concurrent consumers.")

    async def stop(self) -> None:
        """Stop consumers, cancel background monitors, and disconnect Redis."""
        if not self._running:
            return
        self._running = False

        # Cancel consumers and refeeder
        tasks_to_cancel = list(self._consumer_tasks)
        if self._refeeder_task:
            tasks_to_cancel.append(self._refeeder_task)
        if self._heartbeat_task:
            tasks_to_cancel.append(self._heartbeat_task)

        for t in tasks_to_cancel:
            t.cancel()

        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        self._consumer_tasks.clear()
        self._refeeder_task = None
        self._heartbeat_task = None

        # Clean up Redis client
        if self._client:
            try:
                # Remove heartbeat key
                await self._client.delete(f"{REDIS_KEY_WORKER_HEARTBEAT_PREFIX}{self.worker_id}")
                await self._client.aclose()
            except Exception as e:
                logger.debug(f"Error closing Redis client: {e}")
            self._client = None

        if self._pool:
            try:
                await self._pool.disconnect()
            except Exception as e:
                logger.debug(f"Error disconnecting Redis pool: {e}")
            self._pool = None

        logger.info(f"RedisJobQueue stopped for worker {self.worker_id}.")

    async def enqueue(
        self,
        job_id: str,
        file_path: Path,
        options: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> bool:
        """Submit a job to Redis with idempotency locking and durable state tracking."""
        try:
            client = await self.get_client()
            abs_path = str(file_path.resolve())

            # 1. Distributed idempotency check
            idem_key = idempotency_key or f"doc:{file_path.name}:{os.path.getsize(abs_path) if file_path.exists() else 0}"
            redis_idem_key = f"{REDIS_KEY_IDEMPOTENCY_PREFIX}{idem_key}"

            # Try to acquire idempotency lock for 1 hour
            acquired = await client.set(redis_idem_key, job_id, nx=True, ex=3600)
            if not acquired:
                existing_job_id = await client.get(redis_idem_key)
                logger.info(f"Deduplicated submission for job {job_id} -> existing {existing_job_id}")
                return False

            # 2. Build payload and persist state in Redis hash
            payload = JobPayload(
                job_id=job_id,
                file_path=abs_path,
                options=options or {},
                enqueued_at=time.time(),
                attempt=1,
                max_attempts=max_attempts,
                idempotency_key=idem_key,
            )

            job_meta_key = f"{REDIS_KEY_JOB_PREFIX}{job_id}"
            await client.hset(
                job_meta_key,
                mapping={
                    "job_id": job_id,
                    "file_path": abs_path,
                    "stage": JobStage.QUEUED.value,
                    "progress_percent": "0",
                    "attempt": "1",
                    "max_attempts": str(max_attempts),
                    "enqueued_at": str(payload.enqueued_at),
                    "payload": payload.to_json(),
                },
            )
            await client.expire(job_meta_key, 86400 * 7)  # 7 days retention

            # 3. Push to Redis queue
            await client.lpush(REDIS_KEY_QUEUE, payload.to_json())
            logger.info(f"[RedisJobQueue] Enqueued job {job_id} to {REDIS_KEY_QUEUE}")
            return True
        except (RedisConnError, RedisError, OSError) as exc:
            logger.error(f"[RedisJobQueue] Failed to enqueue job {job_id}: {exc}")
            raise QueueUnavailableError(f"Cannot connect to Redis queue: {exc}") from exc

    async def get_queue_depth(self) -> int:
        """Return number of pending jobs in Redis."""
        client = await self.get_client()
        try:
            return await client.llen(REDIS_KEY_QUEUE)
        except Exception:
            return 0

    async def wait_idle(self, timeout: Optional[float] = 30.0) -> bool:
        """Wait until main queue and in-flight processing lists are empty."""
        client = await self.get_client()
        deadline = time.time() + (timeout or 30.0)

        while time.time() < deadline:
            q_len = await client.llen(REDIS_KEY_QUEUE)
            proc_len = await client.llen(self._processing_key)
            delayed_len = await client.zcard(REDIS_KEY_DELAYED)

            if q_len == 0 and proc_len == 0 and delayed_len == 0:
                return True
            await asyncio.sleep(0.1)

        logger.warning(f"wait_idle timed out for RedisJobQueue ({self.worker_id}).")
        return False

    async def _consumer_loop(self, consumer_idx: int) -> None:
        """Continuous consumer loop popping jobs via BRPOPLPUSH."""
        client = await self.get_client()

        while self._running:
            try:
                # Atomically pop from main queue and push to worker's in-flight list
                # Uses timeout of 2 seconds so the loop can react to shutdown signals
                raw_payload = await client.brpoplpush(
                    REDIS_KEY_QUEUE,
                    self._processing_key,
                    timeout=2,
                )
                if not raw_payload:
                    continue

                payload = JobPayload.from_json(raw_payload)
                logger.info(
                    f"[Consumer-{consumer_idx}] Dequeued job {payload.job_id} "
                    f"(attempt {payload.attempt}/{payload.max_attempts})"
                )

                # Process the job through pipeline
                await self._execute_and_settle_job(client, payload, raw_payload)

            except asyncio.CancelledError:
                break
            except (RedisConnError, RedisError) as r_err:
                logger.warning(f"[Consumer-{consumer_idx}] Redis error: {r_err}. Retrying in 1s...")
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.exception(f"[Consumer-{consumer_idx}] Unexpected loop error: {exc}")
                await asyncio.sleep(0.5)

    async def _execute_and_settle_job(
        self,
        client: aioredis.Redis,
        payload: JobPayload,
        raw_payload: str,
    ) -> None:
        """Execute a document processing job and handle success/retry/failure settlements."""
        job_id = payload.job_id
        file_path = Path(payload.file_path)

        # Update Redis hash state
        job_meta_key = f"{REDIS_KEY_JOB_PREFIX}{job_id}"
        await client.hset(
            job_meta_key,
            mapping={
                "stage": JobStage.INGESTING.value,
                "attempt": str(payload.attempt),
                "started_at": str(time.time()),
            },
        )

        try:
            # Execute pipeline
            result = await process_document_job(
                job_id=job_id,
                file_path=file_path,
                options=payload.options,
                store=self.store,
            )

            # Check if job completed or failed within the pipeline
            if result.stage == JobStage.FAILED:
                err_msg = result.error_message or "Pipeline reported failure status"
                if any(k in err_msg.lower() for k in ("corrupt", "unsupported", "format", "not found", "malformed", "failed to open")):
                    raise NonRetryableJobError(err_msg)
                raise QueueError(err_msg)

            # SUCCESS SETTLEMENT
            await client.lrem(self._processing_key, 1, raw_payload)
            await client.hset(
                job_meta_key,
                mapping={
                    "stage": result.stage.value,
                    "progress_percent": "100",
                    "completed_at": str(time.time()),
                },
            )
            self.store.update_job_stage(
                job_id=job_id,
                stage=result.stage,
                progress_percent=100,
            )
            logger.info(f"[RedisJobQueue] Job {job_id} successfully settled with stage {result.stage.value}.")

        except Exception as exc:
            logger.exception(f"[RedisJobQueue] Job {job_id} execution raised exception: {exc}")
            await self._handle_job_failure(client, payload, raw_payload, exc)

    async def _handle_job_failure(
        self,
        client: aioredis.Redis,
        payload: JobPayload,
        raw_payload: str,
        exc: Exception,
    ) -> None:
        """Apply exponential backoff retry or route to Dead-Letter Queue."""
        job_id = payload.job_id
        job_meta_key = f"{REDIS_KEY_JOB_PREFIX}{job_id}"
        is_retryable = not isinstance(exc, NonRetryableJobError)
        err_lower = str(exc).lower()

        # Check if file is missing or corrupted beyond retry
        if (
            isinstance(exc, (FileNotFoundError, ValueError))
            or any(k in err_lower for k in ("corrupt", "unsupported", "format", "not found", "malformed", "failed to open", "cannot open", "broken document"))
        ):
            is_retryable = False

        if payload.attempt < payload.max_attempts and is_retryable:
            # 1. Schedule Retry with Exponential Backoff
            payload.attempt += 1
            delay = self.initial_retry_delay * (self.backoff_factor ** (payload.attempt - 2))
            scheduled_time = time.time() + delay
            new_raw_payload = payload.to_json()

            # Atomically remove from processing and insert into delayed sorted set
            pipe = client.pipeline()
            pipe.lrem(self._processing_key, 1, raw_payload)
            pipe.zadd(REDIS_KEY_DELAYED, {new_raw_payload: scheduled_time})
            pipe.hset(
                job_meta_key,
                mapping={
                    "stage": JobStage.RETRYING.value,
                    "attempt": str(payload.attempt),
                    "retry_scheduled_at": str(scheduled_time),
                    "error_message": str(exc),
                },
            )
            await pipe.execute()

            self.store.update_job_stage(
                job_id=job_id,
                stage=JobStage.RETRYING,
                progress_percent=0,
                error_message=f"Attempt {payload.attempt - 1} failed: {exc}. Retrying in {delay:.1f}s.",
            )
            logger.warning(
                f"[RedisJobQueue] Job {job_id} failed: {exc}. "
                f"Scheduled retry {payload.attempt}/{payload.max_attempts} in {delay:.1f}s."
            )

        else:
            # 2. Dead-Letter Queue (Permanent Failure)
            pipe = client.pipeline()
            pipe.lrem(self._processing_key, 1, raw_payload)
            pipe.lpush(REDIS_KEY_DEAD_LETTER, payload.to_json())
            pipe.hset(
                job_meta_key,
                mapping={
                    "stage": JobStage.FAILED.value,
                    "progress_percent": "100",
                    "failed_at": str(time.time()),
                    "error_message": f"Exhausted {payload.attempt} attempts: {str(exc)}",
                },
            )
            await pipe.execute()

            self.store.update_job_stage(
                job_id=job_id,
                stage=JobStage.FAILED,
                progress_percent=100,
                error_message=f"Job failed after {payload.attempt} attempts: {str(exc)}",
            )
            logger.error(
                f"[RedisJobQueue] Job {job_id} permanently failed and moved to dead-letter queue. "
                f"Error: {exc}"
            )

    async def _delayed_refeeder_loop(self) -> None:
        """Periodically checks the delayed sorted set and re-enqueues mature jobs."""
        client = await self.get_client()

        while self._running:
            try:
                now = time.time()
                # Find all jobs whose scheduled_time <= now
                mature_jobs = await client.zrangebyscore(REDIS_KEY_DELAYED, 0, now, start=0, num=20)
                if mature_jobs:
                    for raw_job in mature_jobs:
                        pipe = client.pipeline()
                        pipe.zrem(REDIS_KEY_DELAYED, raw_job)
                        pipe.lpush(REDIS_KEY_QUEUE, raw_job)
                        await pipe.execute()
                        logger.info(f"[RedisJobQueue] Re-enqueued delayed retry job to {REDIS_KEY_QUEUE}")

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Refeeder] Error in delayed loop: {e}")
                await asyncio.sleep(1.0)

    async def _heartbeat_loop(self) -> None:
        """Maintain active worker heartbeat in Redis."""
        client = await self.get_client()
        hb_key = f"{REDIS_KEY_WORKER_HEARTBEAT_PREFIX}{self.worker_id}"

        while self._running:
            try:
                await client.set(hb_key, time.time(), ex=15)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Heartbeat] Worker heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _recover_orphaned_jobs(self, client: aioredis.Redis) -> None:
        """Check for stranded jobs in worker's in-flight list and return them to main queue."""
        try:
            in_flight_count = await client.llen(self._processing_key)
            if in_flight_count > 0:
                logger.warning(
                    f"[Recovery] Found {in_flight_count} stranded jobs in {self._processing_key}. "
                    "Recovering to main queue..."
                )
                for _ in range(in_flight_count):
                    raw = await client.rpoplpush(self._processing_key, REDIS_KEY_QUEUE)
                    if raw:
                        logger.info("[Recovery] Re-queued orphaned job back to main queue.")
        except Exception as e:
            logger.error(f"[Recovery] Failed to recover orphaned jobs: {e}")


# ---------------------------------------------------------------------------
# Global Queue Factory & Singleton Management
# ---------------------------------------------------------------------------

_global_queue: Optional[BaseJobQueue] = None


def reset_global_queue() -> None:
    """Reset the global background job queue singleton."""
    global _global_queue
    _global_queue = None


def get_job_queue(store: Optional[DocumentStore] = None) -> BaseJobQueue:
    """Retrieve or initialize the global background job queue.

    Selection Logic:
    - If USE_REDIS_QUEUE is 'true' or REDIS_URL is provided, initializes RedisJobQueue.
    - If Redis is unreachable, raises QueueUnavailableError in production mode,
      or falls back to AsyncWorkerQueue only if explicitly configured for local dev.
    """
    global _global_queue
    if _global_queue is None:
        target_store = store or get_global_store()
        redis_url = os.getenv("REDIS_URL")
        use_redis = os.getenv("USE_REDIS_QUEUE", "false").lower() in ("true", "1", "yes")

        if use_redis or (redis_url and "redis://" in redis_url):
            resolved_url = redis_url or "redis://localhost:6379/0"
            concurrency = int(os.getenv("QUEUE_CONCURRENCY", "4"))
            _global_queue = RedisJobQueue(
                store=target_store,
                redis_url=resolved_url,
                concurrency=concurrency,
            )
            logger.info(f"Initialized global RedisJobQueue targeting {resolved_url}")
        else:
            concurrency = int(os.getenv("QUEUE_CONCURRENCY", "4"))
            _global_queue = AsyncWorkerQueue(store=target_store, concurrency=concurrency)
            logger.info("Initialized global AsyncWorkerQueue (development in-memory mode).")

    return _global_queue
