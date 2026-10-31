# DocuPulse Enterprise AP v4.3 — Architecture Audit & Runtime Dependency Graph
**Phase:** Phase 1: Functional Correctness & Runtime Reliability  
**Date:** October 31, 2026  
**Auditor:** Principal Backend Engineer & Distributed Systems Architect  

---

## 1. System Overview & Component Inventory

DocuPulse Enterprise AP is an asynchronous document intelligence platform designed to ingest heterogeneous financial documents (`PDF`, `JPG`, `PNG`, `TIFF`, `XLSX`, `XLS`, `CSV`), perform OCR and layout analysis, extract structured invoice entities with anti-hallucination guarantees, execute financial validation rules, persist data in relational and vector storage, and surface records through a reactive review workbench.

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   FASTAPI WEB TIER                                      │
│  - REST API Routes (/api/v1/documents, /api/v1/jobs, /api/v1/records, /api/v1/telemetry) │
│  - Single-Page Workbench Interface (static/index.html)                                 │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ Enqueues Job
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                            BACKGROUND QUEUE ABSTRACTION                                 │
│  - BaseJobQueue (Abstract Interface)                                                    │
│  - AsyncWorkerQueue (In-memory development fallback)                                    │
│  - RedisJobQueue (Distributed production queue via Redis Streams / Task Queue)          │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ Consumes Job
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                            STANDALONE WORKER PROCESS                                    │
│  - CLI Entrypoint: `python -m src.api.worker`                                           │
│  - Worker loop fetching from Redis -> Invoking `process_document_job()`                 │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ Pipeline Execution
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           CORE PROCESSING PIPELINE                                      │
│  1. Ingestion Engine: PyMuPDF classification + OpenCV CLAHE/deskew + RapidOCR           │
│  2. AI Extraction Engine: Boundary chunking + Gemini SDK (Structured Output) / Fallback │
│  3. Anti-Hallucination Guard: GroundingVerifier fuzzy snippet & coordinate verification │
│  4. Financial Validation: Decimal arithmetic + 3-way matching + Deduplication           │
│  5. Storage & Vector Indexing: SQLAlchemy models (PostgreSQL 16 + pgvector / SQLite)    │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Runtime Dependency Graph

The runtime graph maps how services and modules interact in production:

```
[Client / Browser]
       │
       ▼
 [FastAPI REST API] ──(reads/writes)──> [PostgreSQL / SQLite Database]
       │
       ├──(enqueues job)──────────────> [Redis Broker]
       │                                     │
       │                               (delivers task)
       │                                     ▼
 [FastAPI Healthcheck]              [Standalone Worker Process]
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
             [Document Ingestion]                        [AI Extraction Engine]
             - PyMuPDF                                   - Google GenAI SDK (Gemini)
             - OpenCV Headless                           - Heuristic Fallback
             - RapidOCR / Tesseract                                │
                       │                                           ▼
                       └───────────────────> [Financial Validation Engine]
                                                       │
                                                       ▼
                                            [Database Persistence]
                                            - DocumentModel
                                            - ProcessingJobModel
                                            - ExtractedRecordModel
                                            - ValidationResultModel
                                            - DocumentEmbeddingModel
```

---

## 3. Detailed Component Audit

### 3.1 Job Queue Subsystem (`src/api/queue.py`)
- **Abstract Base:** `BaseJobQueue` defines `start()`, `stop()`, `enqueue()`, `wait_idle()`.
- **In-Memory Implementation:** `AsyncWorkerQueue` uses an internal `asyncio.Queue` with a pool of coroutine workers. It works reliably for local dev and single-process unit testing.
- **Distributed Redis Implementation:** `RedisJobQueue` as found in the baseline is a dummy placeholder:
  - `start()` executes a `pass` in a try block without connecting to Redis or launching worker listeners.
  - `enqueue()` delegates directly to `self._fallback_queue.enqueue(...)`.
  - It does not serialize job parameters to Redis, does not use Redis Streams or lists, and does not support multi-worker distribution.

### 3.2 Standalone Worker Process (`src/api/worker.py`)
- Contains `process_document_job()` which orchestrates the 5 pipeline stages.
- **Defect:** Does not implement a `main()` block or CLI runner (`python -m src.api.worker`). When executed as defined in `docker-compose.yml`, the process immediately terminates with return code 0, leaving Docker containers unable to process queued work.

### 3.3 AI Extraction Engine (`src/extraction/engine.py`)
- Defines `AIExtractionEngine` and `HeuristicExtractor`.
- **Gemini Integration:** Implemented using `google.genai` SDK with `GenerateContentConfig(response_schema=InvoiceExtractionRecord, response_mime_type="application/json")`.
- **Defects:**
  - `google-genai` was omitted from `pyproject.toml` and `requirements.txt`.
  - If `GEMINI_API_KEY` is missing or initialization fails, `AIExtractionEngine` silently falls back to `HeuristicExtractor` but marks `model_used` as `gemini:...`, misrepresenting the origin of the extraction.

### 3.4 Database & Migrations (`src/db/` & `alembic/`)
- Relational schema supports both PostgreSQL (with pgvector) and SQLite (development).
- Tables: `documents`, `document_pages`, `document_sheets`, `extracted_records`, `validation_results`, `audit_logs`, `document_embeddings`, `processing_jobs`.
- Alembic configuration uses `alembic/env.py` with async engine support.
- **Defect in Docker Entrypoint:** `docker-entrypoint.sh` executes `alembic upgrade head || python -c "init_db()"`, masking migration failures in production environments.

---

## 4. Phase 1 Architecture Target

To satisfy Phase 1 functional correctness:
1. **Redis Queue:** Implement a robust Redis queue protocol (using Redis Streams or Redis Lists with atomic `BLPOP`/`BRPOPLPUSH` or native async Redis client) supporting job serialization, persistence, status reporting, retry with exponential backoff, and idempotency.
2. **Worker Entrypoint:** Provide a robust, resilient worker loop in `src/api/worker.py` responsive to OS signals (`SIGINT`, `SIGTERM`), consuming from Redis, executing pipeline jobs, and writing updates to the shared database.
3. **Provider Honesty:** Clearly report extraction provenance (`gemini-2.5-flash` vs `heuristic_fallback`) in all extraction telemetry and response schemas.
