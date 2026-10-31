# DocuPulse Enterprise AP v4.3 — Phase 1 Implementation Plan & Strategy
**Phase:** Phase 1: Functional Correctness & Runtime Reliability  
**Date:** October 31, 2026  
**Auditor:** Principal Backend Engineer & Systems Architect  

---

## 1. Executive Implementation Strategy

Phase 1 focuses exclusively on making the existing architecture actually work in distributed, containerized, and local environments without performing gratuitous rewrites or introducing unnecessary new frameworks.

Our phased execution sequence:

```
[Step 1: Baseline Audit & Registration] ──> [Step 2: Dependency Declarations]
                                                        │
                                                        ▼
[Step 4: Standalone Worker Daemon] <─────── [Step 3: Real Redis Queue Engine]
       │
       ▼
[Step 5: Docker Containerization Fixes] ───> [Step 6: AI Provider Provenance & Gemini]
                                                        │
                                                        ▼
[Step 8: End-to-End Test Suite] <─────────── [Step 7: Database Idempotency & Lifecycle]
       │
       ▼
[Step 9: Final Phase 1 Reports & Verification]
```

---

## 2. Objective-by-Objective Implementation Breakdown

### Objective A: Implement a Real Redis Job Queue (`src/api/queue.py`)
- Implement an authentic `redis.asyncio` queue backend in `RedisJobQueue`.
- Queue protocol:
  - Reliable queue pattern using Redis Lists: `LPUSH docupulse:queue:jobs` and blocking consumer `BRPOPLPUSH docupulse:queue:jobs docupulse:queue:processing` (or atomic Lua script).
  - Job payload serialization with JSON encoding: `job_id`, `file_path`, `options`, `enqueued_at`, `attempt`, `idempotency_key`.
  - Redis connection pooling with configurable retry and timeout.
  - Heartbeat and job status persistence in Redis hash `docupulse:job:{job_id}`.
  - Exponential backoff retry mechanism with dead-letter queue `docupulse:queue:dead_letter`.
  - Idempotency locking via `SET docupulse:lock:{idempotency_key} NX EX 3600`.
  - Preserved fallback: If `REDIS_URL` is omitted (local offline testing), seamlessly utilize the reliable in-memory `AsyncWorkerQueue`.

### Objective B: Fix Standalone Worker Process (`src/api/worker.py`)
- Implement a production-grade worker daemon:
  - Class `WorkerDaemon` with signal handlers for graceful shutdown (`SIGINT`, `SIGTERM`).
  - Worker consumer loop continuously fetching tasks from `RedisJobQueue`.
  - Execution of `process_document_job` with structured correlation logging (`job_id`, `worker_id`, `attempt`).
  - Error isolation: Catching unhandled exceptions, updating database and Redis state, and triggering retry or dead-lettering.
  - CLI entrypoint: `if __name__ == "__main__": asyncio.run(main())`.

### Objective C: Repair Docker Compose Orchestration
- Update `Dockerfile` to ensure `google-genai` and `redis` are installed.
- Update `docker-compose.yml`:
  - Ensure worker service stays running via `python -m src.api.worker`.
  - Fix healthchecks and service dependency chains (`depends_on: postgres: condition: service_healthy, redis: condition: service_healthy`).
  - Ensure shared volumes for `/app/data/uploads` between API and worker containers.
- Fix `docker-entrypoint.sh` to fail fast in production if Alembic migrations fail, rather than silently masking schema mismatches.

### Objective D: Fix AI Provider Integration (`src/extraction/engine.py`)
- Update `AIExtractionEngine`:
  - Ensure clean initialization of `genai.Client(api_key=...)`.
  - Eliminate masquerading: When heuristic fallback executes, explicitly record `provider="heuristic_fallback"` and `fallback_invoked=True` in telemetry and result metadata.
  - Support structured error handling: classify timeout, quota limit, and invalid JSON responses.

### Objective E: Database & Transaction Correctness
- Audit session management in `src/db/repository.py` and `src/api/worker.py`.
- Ensure atomic commits: Document processing status and extraction records are committed in an explicit transaction.
- Prevent duplicate extraction records during retries by utilizing upsert or deleting previous partial extraction rows before re-persisting.
- Verify Alembic migrations cleanly run from empty SQLite and PostgreSQL databases.

### Objective F: Dependency Management
- Update `pyproject.toml` and `requirements.txt` to include `google-genai>=2.0.0`, `redis>=5.0.0`, `numpy>=1.26.0`, and `pytesseract>=0.3.10`.
- Verify clean installation capability.

### Objective G: Observability & Error Handling
- Implement structured logging adapter attaching `document_id`, `job_id`, and `worker_id` to log records.
- Standardize error classifications (`FILE_VALIDATION_ERROR`, `OCR_ERROR`, `AI_PROVIDER_ERROR`, `DATABASE_ERROR`, `REDIS_ERROR`).

### Objectives H & I: Automated Testing & End-to-End Verification
- Develop dedicated test suites:
  - `tests/unit/test_redis_queue.py` (queue serialization, lifecycle, retry, idempotency).
  - `tests/integration/test_worker_process.py` (worker daemon startup, job consumption, graceful shutdown).
  - `tests/integration/test_gemini_provider.py` (live/mocked Gemini structured output and fallback provenance).
  - `tests/integration/test_e2e_pipeline.py` (end-to-end upload -> Redis -> worker -> database -> API).
- Verify zero regressions against the 41 baseline tests.
