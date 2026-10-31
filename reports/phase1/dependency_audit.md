# DocuPulse Enterprise AP v4.3 — Dependency Audit & Packaging Analysis
**Phase:** Phase 1: Functional Correctness & Runtime Reliability  
**Date:** October 31, 2026  
**Auditor:** Principal Backend Engineer & DevOps Architect  

---

## 1. Overview of Project Packaging

DocuPulse specifies dependencies across two files:
- `pyproject.toml` (PEP 517 / PEP 621 compliant build metadata)
- `requirements.txt` (Traditional pip requirements file used by `Dockerfile`)

An audit was conducted by comparing declared specifications against actual static imports across all Python source files in `src/`.

---

## 2. Static Import Analysis Across Codebase

A scan of all `import` and `from ... import` statements in `src/` revealed the following external third-party dependencies:

| Imported Package | Source Modules | Declared in `pyproject.toml`? | Declared in `requirements.txt`? | Status / Action Needed |
| :--- | :--- | :--- | :--- | :--- |
| **`pydantic`** | `src/extraction/schemas.py`, `src/ingestion/schemas.py`, `src/api/schemas.py` | Yes (`>=2.7.0`) | Yes (`>=2.7.0`) | Valid |
| **`fitz` (`pymupdf`)** | `src/ingestion/pdf_processor.py`, `src/evaluation/generator.py` | Yes (`>=1.24.0`) | Yes (`>=1.24.0`) | Valid |
| **`cv2` (`opencv-python-headless`)** | `src/ingestion/ocr_processor.py` | Yes (`>=4.9.0`) | Yes (`>=4.9.0`) | Valid |
| **`PIL` (`pillow`)** | `src/ingestion/ocr_processor.py` | Yes (`>=10.0.0`) | Yes (`>=10.0.0`) | Valid |
| **`rapidocr_onnxruntime`** | `src/ingestion/ocr_processor.py` | Yes (`>=1.3.0`) | Yes (`>=1.3.0`) | Valid |
| **`pytesseract`** | `src/ingestion/ocr_processor.py` | In `[optional-dependencies].dev` | Yes (`>=0.3.10`) | Promote to core in `pyproject.toml` |
| **`pandas`** | `src/ingestion/spreadsheet_processor.py` | Yes (`>=2.2.0`) | Yes (`>=2.2.0`) | Valid |
| **`openpyxl`** | `src/ingestion/spreadsheet_processor.py` | Yes (`>=3.1.2`) | Yes (`>=3.1.2`) | Valid |
| **`xlrd`** | `src/ingestion/spreadsheet_processor.py` | Yes (`>=2.0.1`) | Yes (`>=2.0.1`) | Valid |
| **`charset_normalizer`** | `src/ingestion/validators.py` | Yes (`>=3.3.0`) | Yes (`>=3.3.0`) | Valid |
| **`rapidfuzz`** | `src/validation/deduplicator.py`, `src/extraction/grounding.py` | Yes (`>=3.0.0`) | Yes (`>=3.0.0`) | Valid |
| **`fastapi`** | `src/api/main.py`, `src/api/routes/*.py` | Yes (`>=0.110.0`) | Yes (`>=0.110.0`) | Valid |
| **`uvicorn`** | `src/api/main.py`, `Dockerfile` | Yes (`>=0.29.0`) | Yes (`>=0.29.0`) | Valid |
| **`sqlalchemy`** | `src/db/session.py`, `src/db/models.py`, `src/db/repository.py` | Yes (`>=2.0.0`) | Yes (`>=2.0.0`) | Valid |
| **`asyncpg`** | `src/db/session.py` (PostgreSQL async driver) | Yes (`>=0.29.0`) | Yes (`>=0.29.0`) | Valid |
| **`aiosqlite`** | `src/db/session.py` (SQLite async driver) | Yes (`>=0.20.0`) | Yes (`>=0.20.0`) | Valid |
| **`alembic`** | `alembic/env.py`, `alembic.ini` | Yes (`>=1.13.0`) | Yes (`>=1.13.0`) | Valid |
| **`pgvector`** | `src/db/models.py`, `src/db/search.py` | Yes (`>=0.2.5`) | Yes (`>=0.2.5`) | Valid |
| **`redis`** | `src/api/queue.py`, `src/api/worker.py` | **NO (MISSING)** | Yes (`>=5.0.0`) | **DEFECT (DEF-04): Add to `pyproject.toml`** |
| **`google.genai`** | `src/extraction/engine.py` | **NO (MISSING)** | **NO (MISSING)** | **DEFECT (DEF-03): Add `google-genai>=2.0.0` to both** |
| **`numpy`** | `src/api/main.py` (telemetry percentiles) | Implicit via pandas | Implicit via pandas | Explicitly declare `numpy>=1.26.0` |

---

## 3. Version Compatibility Analysis

- **Python Compatibility:** Python `>=3.11` is specified in `pyproject.toml`. Current execution environment is Python `3.12.10`.
- **Pydantic v2:** `pydantic>=2.7.0` is used across schemas. All schemas utilize Pydantic v2 API (`model_validate`, `model_dump`, `Field`).
- **SQLAlchemy 2.0:** All queries and models utilize SQLAlchemy 2.0 `select()`, async engine, and `Mapped[...]` type annotations.
- **Async Redis:** `redis.asyncio` (`redis>=5.0.0`) supports modern async connection pools, native asyncio coroutines, and connection healthchecks.
- **Google GenAI SDK:** The modern `google-genai` SDK (`>=2.0.0`) provides `genai.Client` with structured JSON output enforcement (`response_schema=...`).

---

## 4. Remediation Steps

1. Update `pyproject.toml` dependencies:
   - Add `google-genai>=2.0.0`
   - Add `redis>=5.0.0`
   - Add `numpy>=1.26.0`
   - Promote `pytesseract>=0.3.10` to runtime dependencies
2. Update `requirements.txt`:
   - Add `google-genai>=2.0.0`
   - Add `numpy>=1.26.0`
3. Validate clean installation in an isolated virtual environment.
