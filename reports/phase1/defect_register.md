# DocuPulse Enterprise AP v4.3 — Defect Register
**Phase:** Phase 1: Functional Correctness & Runtime Reliability  
**Date:** October 31, 2026  
**Auditor:** Principal Backend Engineer & QA Automation Engineer  

---

## Defect Summary Table

| Defect ID | Component | Severity | Description | Status |
| :--- | :--- | :--- | :--- | :--- |
| **DEF-01** | `src/api/queue.py` | **CRITICAL** | `RedisJobQueue` is a non-functioning stub that delegates to in-memory fallback. | Identified |
| **DEF-02** | `src/api/worker.py` | **CRITICAL** | Standalone worker CLI entrypoint is missing; `python -m src.api.worker` terminates immediately. | Identified |
| **DEF-03** | `pyproject.toml` | **HIGH** | `google-genai` SDK is omitted from runtime project dependencies. | Identified |
| **DEF-04** | `pyproject.toml` | **MEDIUM** | `redis` package is omitted from `pyproject.toml` dependencies. | Identified |
| **DEF-05** | `src/extraction/engine.py` | **HIGH** | Extraction fallback masquerades as `gemini:model_name` when heuristic extraction runs. | Identified |
| **DEF-06** | `docker-compose.yml` | **HIGH** | Worker container crashes/exits on startup due to missing worker loop. | Identified |
| **DEF-07** | `docker-entrypoint.sh` | **MEDIUM** | Silently falls back to `init_db()` upon Alembic migration error, hiding schema drifts. | Identified |
| **DEF-08** | `src/api/queue.py` | **HIGH** | Lack of exponential backoff retry mechanism and dead-letter queue handling for transient failures. | Identified |
| **DEF-09** | `src/api/queue.py` | **MEDIUM** | Missing distributed idempotency locks for duplicate job submissions. | Identified |
| **DEF-10** | `src/api/worker.py` | **MEDIUM** | Processing logs lack unified contextual correlation IDs (`job_id`, `worker_id`, `attempt`). | Identified |

---

## Detailed Defect Specifications

### DEF-01: RedisJobQueue Stub Implementation
- **File & Function:** `src/api/queue.py::RedisJobQueue`
- **Root Cause:** The class contains a placeholder `start()` method with an empty `try: pass` block. It instantiates `self._fallback_queue = AsyncWorkerQueue()` and routes all `enqueue()` calls to the in-memory fallback queue.
- **Expected Behavior:** `RedisJobQueue` connects to a Redis instance via `redis.asyncio`, serializes jobs into a durable Redis queue/stream, supports distributed consumers across multiple containers, and tracks worker state.
- **Actual Behavior:** Jobs are enqueued into a local in-memory `asyncio.Queue` belonging to the current process, breaking multi-process and multi-container Docker deployments.
- **Proposed Fix:** Implement a production-grade async Redis queue using Redis lists/streams (`LPUSH`/`BRPOPLPUSH` or Streams `XADD`/`XREADGROUP`), including connection pooling, JSON serialization, heartbeat/worker registration, and graceful drain.
- **Regression Test:** `tests/integration/test_redis_queue.py` simulating real Redis job enqueue, cross-process consumption, and queue persistence.

---

### DEF-02: Missing Standalone Worker CLI Entrypoint
- **File & Function:** `src/api/worker.py::__main__`
- **Root Cause:** `src/api/worker.py` contains only the coroutine `process_document_job`. It does not contain an executable loop or `if __name__ == "__main__":` block.
- **Expected Behavior:** Running `python -m src.api.worker` starts a long-running worker daemon that registers signal handlers (`SIGINT`, `SIGTERM`), connects to Redis, consumes jobs continuously, and handles graceful shutdown.
- **Actual Behavior:** The command executes in ~50ms, does nothing, and exits with code 0.
- **Proposed Fix:** Implement a robust `WorkerDaemon` class and entrypoint in `src/api/worker.py` that listens on the Redis queue, handles concurrency, logs telemetry, and terminates cleanly on shutdown signals.
- **Regression Test:** `tests/integration/test_worker_cli.py` testing subprocess invocation, SIGTERM termination, and job processing.

---

### DEF-03: Undeclared `google-genai` Dependency
- **File & Function:** `pyproject.toml`
- **Root Cause:** `src/extraction/engine.py` imports `from google import genai`, but `google-genai>=2.0.0` was not included in `pyproject.toml` dependencies or `requirements.txt`.
- **Expected Behavior:** A clean environment installing via `pip install -e .` or `pip install -r requirements.txt` can immediately import and utilize the Gemini client.
- **Actual Behavior:** In clean environments without global packages, importing `src.extraction.engine` fails when Gemini is invoked.
- **Proposed Fix:** Declare `google-genai>=2.0.0` in `pyproject.toml` and `requirements.txt`.
- **Regression Test:** `tests/unit/test_dependencies.py` verifying all imports in `src/` are declared in packaging specifications.

---

### DEF-04: Undeclared `redis` in `pyproject.toml`
- **File & Function:** `pyproject.toml`
- **Root Cause:** `redis>=5.0.0` was added to `requirements.txt` but omitted from `pyproject.toml` under `dependencies`.
- **Expected Behavior:** Installing package via modern PEP 517/518 build (`pyproject.toml`) installs `redis`.
- **Actual Behavior:** Package installed solely via `pyproject.toml` lacks Redis driver.
- **Proposed Fix:** Add `redis>=5.0.0` to `dependencies` in `pyproject.toml`.
- **Regression Test:** Clean virtual environment installation test.

---

### DEF-05: Fallback Extraction Masquerading as LLM
- **File & Function:** `src/extraction/engine.py::AIExtractionEngine.extract_document_async`
- **Root Cause:** When `_gemini_client` is unavailable, the engine falls back to `HeuristicExtractor` but sets `model_used=f"{self.config.provider.value}:{self.config.model_name}"` (`gemini:gemini-2.5-flash`), hiding the fact that heuristic extraction occurred.
- **Expected Behavior:** The extraction response metadata and telemetry must explicitly indicate `heuristic_fallback` as the model used and set `provider="heuristic_fallback"` when fallback occurs.
- **Actual Behavior:** The response falsely claims that Gemini was used.
- **Proposed Fix:** Update `AIExtractionEngine` to explicitly record the true provider executed, including telemetry flag `fallback_invoked=True`.
- **Regression Test:** `tests/unit/test_extraction.py` asserting model provenance metadata on fallback.

---

### DEF-06: Docker Worker Service Premature Termination
- **File & Function:** `docker-compose.yml::services.worker`
- **Root Cause:** Due to DEF-02, the worker container immediately exits.
- **Expected Behavior:** The worker container remains running in the background, continuously polling Redis for document jobs.
- **Actual Behavior:** Container transitions to status `Exited (0)`.
- **Proposed Fix:** Fix DEF-02 and configure worker healthcheck in `docker-compose.yml`.
- **Regression Test:** Automated Docker integration verification script.

---

### DEF-07: Unchecked Database Migration Fallback
- **File & Function:** `docker-entrypoint.sh`
- **Root Cause:** `alembic upgrade head || { echo "WARNING..."; python -c "init_db()"; }` catches any migration failure and blindly executes `Base.metadata.create_all()`.
- **Expected Behavior:** Migrations should execute deterministically. In production mode, migration failures must be treated as critical deployment blockers.
- **Actual Behavior:** Schema drift is masked by unversioned table creation.
- **Proposed Fix:** Ensure Alembic migrations are robust and separate production migration enforcement from development initialization.
- **Regression Test:** Migration upgrade/downgrade test against clean SQLite and PostgreSQL.

---

### DEF-08: Missing Retry Policy & Backoff for Redis Jobs
- **File & Function:** `src/api/queue.py`
- **Root Cause:** Failed jobs are immediately transitioned to `FAILED` without retries for transient network, database, or rate-limit exceptions.
- **Expected Behavior:** Configurable maximum retries (e.g. 3 attempts) with exponential backoff (`delay = initial * (backoff_factor ** attempt)`), filtering permanent non-retryable errors (e.g. corrupted file, invalid format).
- **Actual Behavior:** Single-shot failure marks job as permanently failed.
- **Proposed Fix:** Implement retry semantics with `JobStage.RETRYING`, tracking `attempt_count` in Redis metadata.
- **Regression Test:** `tests/unit/test_queue_retry.py`.

---

### DEF-09: Missing Idempotency Protection on Job Submission
- **File & Function:** `src/api/routes/documents.py`, `src/api/queue.py`
- **Root Cause:** Submitting the same document or job ID multiple times can spawn redundant duplicate processing tasks.
- **Expected Behavior:** Check whether a job for the same document or idempotency key is already `QUEUED` or `PROCESSING` and return existing job status without duplicate execution.
- **Actual Behavior:** New jobs are redundantly queued.
- **Proposed Fix:** Implement Redis-backed idempotency lock (`SET NX EX`) keyed by file hash or document ID.
- **Regression Test:** Duplicate submission unit test.

---

### DEF-10: Lack of Unified Structured Logging Context
- **File & Function:** `src/api/worker.py`, `src/api/main.py`
- **Root Cause:** Log records use raw strings without structured context fields.
- **Expected Behavior:** Every processing event includes `document_id`, `job_id`, `worker_id`, `attempt`, and `stage` for end-to-end traceability.
- **Actual Behavior:** Traceability is fragmented across unstructured log lines.
- **Proposed Fix:** Implement contextual logger adapter / structured logging utility.
- **Regression Test:** Logging audit verification test.
