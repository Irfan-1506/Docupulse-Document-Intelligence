# Enterprise Document Processing, AI Extraction, Validation, REST API & Hybrid Vector Search Engine

Production-grade, asynchronous financial document intelligence system in Python engineered for high-volume, heterogeneous financial workflows (`PDF`, `JPG`, `PNG`, `TIFF`, `XLSX`, `XLS`, `CSV`).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              FASTAPI BACKEND REST API                                  │
│  - Multi-file uploads (PDF, XLSX, CSV, JPG, PNG) -> 202 Accepted                      │
│  - Real-time job telemetry, batch polling, and background progress streaming           │
│  - Hybrid keyword + semantic vector search (RRF reranking + faceted filters)           │
│  - Human-in-the-Loop (HITL) corrections with field audit trail & live re-validation   │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                       ASYNC BACKGROUND WORKER ORCHESTRATION                            │
│  - Dual-mode queue (Distributed Redis / Arq + Zero-Config AsyncWorkerQueue fallback)  │
│  - Stage 1: INGESTING (10%)       -> Document validation & multi-sheet/PDF parsing    │
│  - Stage 2: OCR_PROCESSING (35%)  -> OpenCV image restoration & rasterization         │
│  - Stage 3: AI_EXTRACTING (65%)   -> Gemini Structured Outputs / Heuristic Fallback   │
│  - Stage 4: VALIDATING (90%)      -> Decimal rules, deduplication, cross-verification │
│  - Stage 5: COMPLETED (100%)      -> Results persisted & telemetry recorded           │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   POSTGRESQL 16+ WITH PGVECTOR & FULL-TEXT SEARCH                      │
│  - Normalized schema: documents, pages, sheets, extracted_records, validation_results  │
│  - Vector embeddings (1536-dim) with pgvector HNSW cosine distance (<=>)               │
│  - Reciprocal Rank Fusion (RRF) combining keyword BM25 ranks with vector similarity    │
│  - Alembic async migrations for zero-downtime database evolution                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Technical Stack & Modules

| Task | Module | Path | Description |
| :--- | :--- | :--- | :--- |
| **Task 1** | **Schemas** | `src/ingestion/schemas.py` | Pydantic v2 models (`PageContent`, `SheetContent`, `DocumentProcessingResult`, `IngestionConfig`). |
| **Task 1** | **Validators** | `src/ingestion/validators.py` | Magic byte inspection, SHA-256 deduplication, zero-byte/truncated/corrupt file guard. |
| **Task 1** | **PDF Processor** | `src/ingestion/pdf_processor.py` | Hybrid PDF engine (digital extraction + OCR fallback), 1,000+ page bounded chunking. |
| **Task 1** | **OCR Pipeline** | `src/ingestion/ocr_processor.py` | 4-stage OpenCV image restoration (CLAHE, denoise, deskew, adaptive threshold) + modular RapidOCR/Tesseract. |
| **Task 1** | **Spreadsheet Engine** | `src/ingestion/spreadsheet_processor.py` | Multi-sheet Excel & dirty CSV engine with banner stripping, encoding/delimiter detection, and markdown rendering. |
| **Task 1** | **Ingestion Orchestrator** | `src/ingestion/router.py` | `DocumentIngestionEngine` with thread pool batching, order preservation, and CLI runner. |
| **Task 2** | **Extraction Schemas** | `src/extraction/schemas.py` | Strict nullable Pydantic v2 models (`InvoiceExtractionRecord`, `LineItem`, `DocumentExtractionResponse`). |
| **Task 2** | **Prompts** | `src/extraction/prompts.py` | `SYSTEM_EXTRACTION_PROMPT` enforcing Zero-Fabrication, ambiguity nulling, and exact evidence snippets. |
| **Task 2** | **Grounding Verifier** | `src/extraction/grounding.py` | Anti-hallucination verification cross-checking numbers and names against raw text, and propagating OCR warnings. |
| **Task 2** | **Chunker & Mapper** | `src/extraction/chunker.py` | Multi-page boundary splitter for 1,000-page multi-invoice PDFs & semantic spreadsheet row-to-invoice mapper. |
| **Task 2** | **Extraction Engine** | `src/extraction/engine.py` | `AIExtractionEngine` with modular provider strategy (Gemini, OpenAI/Ollama, Heuristic Fallback). |
| **Task 2** | **End-to-End CLI** | `src/extraction/cli.py` | Chained CLI executing Ingestion $\rightarrow$ Extraction and exporting structured JSON. |
| **Task 3** | **Validation Schemas** | `src/validation/schemas.py` | Pydantic v2 validation models (`RecordValidationResult`, `ValidationIssue`, `CrossVerificationMatch`, `BatchValidationReport`). |
| **Task 3** | **Financial Rules** | `src/validation/rules.py` | Strict `decimal.Decimal` math validation (`subtotal + tax == total`, `sum(line_items) == subtotal`, sanity checks). |
| **Task 3** | **Deduplicator** | `src/validation/deduplicator.py` | Exact and fuzzy duplicate detector with vendor normalization and OCR typo translation (`O` $\rightarrow$ `0`, `I` $\rightarrow$ `1`). |
| **Task 3** | **Cross-Verifier** | `src/validation/cross_verifier.py` | PDF-to-Excel ledger reconciliation and PDF-to-PDF (Invoice vs Purchase Order) cross-verification. |
| **Task 3** | **Validation Orchestrator** | `src/validation/engine.py` | `ValidationEngine` with automated HITL review status classification (`AUTO_VERIFIED`, `NEEDS_MANUAL_REVIEW`, `REJECTED_DUPLICATE`). |
| **Task 3** | **Full Pipeline CLI** | `src/validation/cli.py` | End-to-end CLI executing Task 1 $\rightarrow$ Task 2 $\rightarrow$ Task 3 with ledger cross-verification and JSON export. |
| **Task 4** | **API Schemas** | `src/api/schemas.py` | Pydantic v2 contracts (`JobStatusResponse`, `BatchUploadResponse`, `ManualCorrectionRequest`, `SearchQueryParams`, `AuditLogEntry`). |
| **Task 4** | **Document & Job Store** | `src/api/store.py` | Database-backed repository interface connecting FastAPI with SQLAlchemy and pgvector with thread-safe async bridge. |
| **Task 4** | **Dual-Mode Queue** | `src/api/queue.py` | Background queue abstraction supporting Redis distributed workers and zero-config `AsyncWorkerQueue` for CI/offline use. |
| **Task 4** | **Pipeline Worker** | `src/api/worker.py` | End-to-end async worker executing Ingestion $\rightarrow$ OCR $\rightarrow$ AI Extraction $\rightarrow$ Validation with progress telemetry. |
| **Task 4** | **Route Handlers** | `src/api/routes/` | Modular endpoints for documents (`upload`, `search`, `results`, `cross-verify`), jobs (`status`, `batches`, `reprocess`), and records (`correct`). |
| **Task 4** | **FastAPI Entrypoint** | `src/api/main.py` | FastAPI application with CORS, request latency headers, health check, and lifespan queue lifecycle management. |
| **Task 5** | **Relational Models** | `src/db/models.py` | SQLAlchemy 2.0 models for documents, pages, sheets, records, validation, audit logs, vector embeddings, and jobs. |
| **Task 5** | **Session & Lifecycle** | `src/db/session.py` | Async engine/sessionmaker supporting PostgreSQL+asyncpg (`DATABASE_URL`) and SQLite fallback, plus `init_db()`/`drop_db()`. |
| **Task 5** | **Repository** | `src/db/repository.py` | `SQLAlchemyRepository` implementing complete transactional persistence, retrieval, and updates. |
| **Task 5** | **Hybrid Search Engine** | `src/db/search.py` | Full-text keyword search + 1536-dim vector cosine similarity + Reciprocal Rank Fusion (RRF) reranking. |
| **Task 5** | **Alembic Migrations** | `alembic/` | Asynchronous Alembic configuration and migration versions (`alembic upgrade head`). |
| **Task 7** | **Evaluation Schemas** | `src/evaluation/schemas.py` | Pydantic v2 benchmark contracts (`EvaluationBenchmarkReport`, `FieldEvaluationScore`, `LatencyPercentiles`). |
| **Task 7** | **Dataset Generator** | `src/evaluation/generator.py` | Synthetic generator producing 50+ diverse financial files (`PDF`, `XLSX`, `CSV`, Scans, Corrupted) with ground truth. |
| **Task 7** | **Metrics Engine** | `src/evaluation/metrics.py` | Scikit-learn Macro F1, Precision, Recall, and financial tolerance evaluation calculators. |
| **Task 7** | **Benchmark Evaluator** | `src/evaluation/evaluator.py` | End-to-end evaluation orchestrator generating JSON and Markdown benchmark reports. |
| **Task 7** | **CI/CD Pipeline** | `scripts/run_benchmarks.py` | Executable script validating production thresholds (Macro F1 $\ge 95\%$, P95 $< 2.5\text{ s/page}$). |

---

## Database Schema & Hybrid Search Architecture

### 1. Relational Tables
- **`documents`**: Metadata, file SHA-256 hash (unique index for deduplication), status, and full merged text.
- **`document_pages`**: Page-level OCR/digital text, extraction method, confidence score, and extracted table matrices.
- **`document_sheets`**: Multi-sheet workbook metadata, column headers, structured row records, and markdown previews.
- **`extracted_records`**: Strict schema-validated financial fields (`invoice_number`, `invoice_date`, `supplier_name`, `customer_name`, `subtotal`, `tax`, `total`, `currency`, itemized `line_items`).
- **`validation_results`**: Mathematical consistency diagnostics, automated status (`AUTO_VERIFIED`, `NEEDS_MANUAL_REVIEW`, `REJECTED_DUPLICATE`, `MANUALLY_VERIFIED`), and cross-verification matches.
- **`audit_logs`**: Reviewer mutation trail recording actor, modified field, old value, new value, timestamp, and notes.
- **`document_embeddings`**: Text chunk embeddings stored in `pgvector` (`Vector(1536)`) or cross-dialect vector types.
- **`processing_jobs`**: Persistent background queue telemetry, progress percentages, and retry counts.

### 2. Hybrid Search & Reciprocal Rank Fusion (RRF)
Search combines keyword and semantic vector signals:
$$\text{RRF Score}(d) = \frac{w_{\text{keyword}}}{k + \text{rank}_{\text{keyword}}(d)} + \frac{w_{\text{vector}}}{k + \text{rank}_{\text{vector}}(d)}$$
where $k = 60$ and $w_{\text{keyword}} = w_{\text{vector}} = 0.5$.

Results are strictly filtered by structured relational facets (supplier name, total amount bounds, date ranges, review requirement flags).

---

## Database Migrations & Initialization

Initialize the database schema:

```bash
# Run migrations using Alembic
alembic upgrade head

# Or directly initialize via Python CLI
python -m src.db.session
```

---

## Starting the REST API & Web Dashboard

Start the FastAPI application with Uvicorn:

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

- **DocuPulse Enterprise AP v4.2 Web Dashboard:** Open `http://localhost:8000/` in any browser.
- **Interactive Swagger Documentation:** Open `http://localhost:8000/docs`.
- **OpenAPI 3.1 JSON Specification:** Open `http://localhost:8000/openapi.json`.

---

## Web Dashboard: DocuPulse Enterprise AP v4.2

The system includes a single-page web application served directly from `/` (via `static/index.html`), implementing all 5 enterprise operational screens:

1. **Ingestion Pipeline & Queue Monitor (`#ingestion-pipeline`):**
   - Drag-and-drop batch upload area for `PDF`, `JPG`, `PNG`, `TIFF`, `XLSX`, `XLS`, and `CSV`.
   - Toggle flags for `Force OCR Pipeline` and LLM `Extraction Provider Override`.
   - Real-time job telemetry cards showing processing rate, active batch items, OCR queue, and error counts.
   - Interactive batch progress ledger with live stage badges (`INGESTING`, `OCR_PROCESSING`, `AI_EXTRACTING`, `VALIDATING`, `COMPLETED`, `FAILED`), page throughput, and one-click manual retry buttons.

2. **HITL Review Workbench (`#hitl-workbench`):**
   - Side-by-side interactive document inspection view with zoom controls (`-`, `+`, `100%`) and visual highlight overlays for extracted bounding boxes.
   - Live editable financial form for `Supplier Name`, `Invoice #`, `Invoice Date`, `Subtotal`, `Tax`, and `Total`.
   - Dynamic math reconciliation calculator: changes to subtotal or tax automatically recalculate expected totals and trigger live discrepancy warnings.
   - "One-Click Math Rebalance" button that automatically adjusts the total to match `Subtotal + Tax`.
   - Itemized line items table with editable quantities, unit prices, line totals, and dynamic add/remove row operations.
   - Audit trail tracking capturing field changes, before/after values, and reviewer identity.
   - Action controls: "Reject Document", "Escalate to Senior Auditor", and "Approve & Mark Verified" (which syncs changes to `PATCH /api/v1/records/{id}/correct`).

3. **Cross-Verification & Multi-Ledger Reconciliation Matrix (`#cross-verification`):**
   - Multi-ledger discrepancy triage matrix with status filters (`All Exceptions`, `Variance Detected`, `Exact Match`, `Missing Record`).
   - Split reconciliation comparison cards (Source Document vs. ERP Ledger Entry) displaying PO numbers, vendor tax IDs, item-by-item cost breakdowns, and highlighted delta badges.
   - Resolution panel allowing reviewers to accept variances, re-route to vendor dispute workflows, or sync corrected values back to the primary ERP.

4. **Search Explorer & Hybrid Discovery (`#search-explorer`):**
   - Dual-mode hybrid search bar executing full-text BM25 keyword matching and 1536-dim vector cosine similarity with Reciprocal Rank Fusion (RRF).
   - Faceted relational filtering panel with instant toggles for suppliers, currency (`USD`, `EUR`, `GBP`), date ranges, minimum/maximum amount sliders, and review status flags.
   - Result cards displaying relevance score match percentages, extracted field badges, document thumbnails, and direct links to inspect records in the HITL workbench.

5. **System Telemetry & Architecture Monitor (`#system-telemetry`):**
   - Real-time service uptime, worker thread pool utilization, background queue health, and processing latency stats (`X-Process-Time-Ms`).
   - End-to-end architecture topology diagram illustrating data flow from ingestion through vector indexing.

---

## Running the Automated Test Suite & Code Coverage

Run all 41 unit and integration tests across Tasks 1, 2, 3, 4, 5, and 7 with code coverage:

```bash
pytest --cov=src --cov-report=term-missing -v
```

All 41 tests execute completely offline without requiring live external services, Redis daemons, or live API keys.

---

## Running the Labeled Evaluation Benchmark Engine (Task 7)

Execute the end-to-end benchmark against the labeled golden dataset of 54 diverse financial documents:

```bash
# Direct Evaluation CLI
python -m src.evaluation.evaluator --dataset tests/fixtures/golden_set/

# Or Automated CI/CD Script
python scripts/run_benchmarks.py
```

### Production Thresholds & Benchmark Results

The benchmark evaluates field-level extraction accuracy and operational latency against formal production criteria:

| Metric | Production Threshold | Benchmark Result | Status |
| :--- | :--- | :--- | :--- |
| **Macro F1 Score** | $\ge 95.0\%$ | **99.16%** | **PASS** |
| **Macro Precision** | - | **100.00%** | **PASS** |
| **Macro Recall** | - | **98.35%** | **PASS** |
| **P95 Processing Latency** | $< 2.50\text{ s/page}$ | **0.158 s/page** (158 ms) | **PASS** |
| **P50 (Median) Latency** | - | **71.7 ms/page** | **PASS** |
| **Graceful Rejection Rate** | $100.0\%$ | **100.0%** (4/4 corrupt files) | **PASS** |

Reports are automatically generated and saved to:
- `reports/evaluation_benchmark.json` (Structured machine-readable benchmark metrics)
- `reports/evaluation_benchmark.md` (Executive summary table with field-level breakdowns)


---

## Engineering Report & PDF Export (Deliverables 2–7 & 9)

A comprehensive, enterprise-grade engineering report consolidating all deliverables, architecture diagrams, data schemas, mathematical validation rules, and benchmark results is located at [`docs/ENGINEERING_REPORT.md`](file:///C:/Users/Asus/.gemini/antigravity/scratch/doc_intelligence_engine/docs/ENGINEERING_REPORT.md).

To generate or re-compile the high-fidelity 22-page publication PDF:

```bash
python scripts/export_report_pdf.py
```

The compiled PDF is saved to:
- [`docs/DocuPulse_Enterprise_AP_v4.2_Engineering_Report.pdf`](file:///C:/Users/Asus/.gemini/antigravity/scratch/doc_intelligence_engine/docs/DocuPulse_Enterprise_AP_v4.2_Engineering_Report.pdf)

---

## Docker Containerization & Production Deployment

DocuPulse provides a complete multi-stage, security-hardened `Dockerfile` and `docker-compose.yml` orchestrating:
1. **`postgres`**: PostgreSQL 16 + `pgvector` (`pgvector/pgvector:pg16`) with HNSW cosine distance indexing.
2. **`redis`**: Redis 7 Alpine (`redis:7-alpine`) task broker for distributed job queuing.
3. **`api`**: FastAPI REST API server with health checks and static workbench UI.
4. **`worker`**: Background worker processing multi-stage document ingestion, OCR, LLM extraction, and validation.

### Deployment Commands

```bash
# 1. Configure environment variables
cp .env.example .env

# 2. Build and launch production cluster
docker compose up -d --build

# 3. Check container status & health
docker compose ps

# 4. Stream cluster logs
docker compose logs -f

# 5. Stop cluster
docker compose down
```

For local development with live code reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

Alternatively, use the included lifecycle scripts:
- PowerShell: `.\scripts\manage_containers.ps1 {build|up|dev|down|status|logs|test-health|clean}`
- Bash: `./scripts/manage_containers.sh {build|up|dev|down|status|logs|test-health|clean}`
