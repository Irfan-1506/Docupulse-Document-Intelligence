# DocuPulse Enterprise AP v4.3 — Baseline Test Report
**Phase:** Phase 1: Functional Correctness & Runtime Reliability  
**Date:** October 31, 2026  
**Environment:** Windows 11, Python 3.12.10, Pytest 9.1.1, Pluggy 1.7.0  
**Test Command:** `pytest -v --tb=short tests/`

---

## 1. Executive Summary

A comprehensive baseline test run was executed on the existing unmodified codebase to assess the operational status of all registered unit and integration tests before Phase 1 remediation.

| Metric | Result |
| :--- | :--- |
| **Total Tests Collected** | **41** |
| **Tests Passed** | **41** (100.0%) |
| **Tests Failed** | **0** (0.0%) |
| **Tests with Errors** | **0** (0.0%) |
| **Tests Skipped** | **0** (0.0%) |
| **Total Test Execution Time** | **68.58 seconds** |
| **Warnings** | 1 (PyMuPDF deprecation warning on `fitz`) |

---

## 2. Test Execution Breakdown by Module

### A. Integration Tests (`tests/integration/`)
- `tests/integration/test_api.py`:
  - `test_health_check` — **PASSED**
  - `test_batch_upload_and_pipeline_execution` — **PASSED**
  - `test_document_results_retrieval` — **PASSED**
  - `test_search_documents` — **PASSED**
  - `test_manual_correction_and_audit_trail` — **PASSED**
  - `test_corrupted_file_handling_and_reprocessing` — **PASSED**
  - `test_jobs_dashboard_listing` — **PASSED**
  - `test_serve_dashboard` — **PASSED**
- `tests/integration/test_api_db.py`:
  - `test_api_db_full_lifecycle_and_search` — **PASSED**
  - `test_api_db_corrupted_file_handling` — **PASSED**
- `tests/integration/test_db.py`:
  - `test_db_document_and_record_persistence` — **PASSED**
  - `test_db_record_update_and_audit_log` — **PASSED**
  - `test_hybrid_search_keyword_and_faceted_filters` — **PASSED**
  - `test_semantic_vector_search_and_rrf` — **PASSED**

### B. Unit Tests (`tests/unit/`)
- `tests/unit/test_evaluation.py`:
  - `test_string_normalization_and_number_matching` — **PASSED**
  - `test_benchmark_metrics_calculator_perfect_match` — **PASSED**
  - `test_golden_dataset_generation` — **PASSED**
  - `test_benchmark_evaluator_end_to_end` — **PASSED**
  - `test_cli_runners` — **PASSED**
- `tests/unit/test_extraction.py`:
  - `TestAIExtractionEngine::test_complete_digital_invoice_extraction` — **PASSED**
  - `TestAIExtractionEngine::test_missing_and_uncertain_fields_zero_fabrication` — **PASSED**
  - `TestAIExtractionEngine::test_multi_invoice_multi_page_pdf_segmentation` — **PASSED**
  - `TestAIExtractionEngine::test_spreadsheet_and_csv_multi_invoice_extraction` — **PASSED**
  - `TestAIExtractionEngine::test_grounding_verifier_rejects_hallucinated_total` — **PASSED**
  - `TestAIExtractionEngine::test_ocr_degradation_warning_propagation` — **PASSED**
- `tests/unit/test_ingestion.py`:
  - `TestDocumentIngestionEngine::test_scenario_1_multi_page_digital_pdf` — **PASSED**
  - `TestDocumentIngestionEngine::test_scenario_2_noisy_rotated_ocr_invoice` — **PASSED**
  - `TestDocumentIngestionEngine::test_scenario_3_multi_sheet_excel` — **PASSED**
  - `TestDocumentIngestionEngine::test_scenario_4_dirty_csv_encoding_and_delimiter` — **PASSED**
  - `TestDocumentIngestionEngine::test_scenario_5_corrupted_file_isolation_guard` — **PASSED**
  - `TestDocumentIngestionEngine::test_hybrid_pdf_classification` — **PASSED**
  - `TestDocumentIngestionEngine::test_parallel_batch_processing` — **PASSED**
  - `TestDocumentIngestionEngine::test_image_preprocessor_deskew_and_clahe` — **PASSED**
  - `TestDocumentIngestionEngine::test_1000_page_pdf_streaming` — **PASSED**
- `tests/unit/test_validation.py`:
  - `TestValidationEngine::test_scenario_1_clean_auto_verified_record` — **PASSED**
  - `TestValidationEngine::test_scenario_2_missing_required_fields` — **PASSED**
  - `TestValidationEngine::test_scenario_3_negative_and_invalid_numeric_values` — **PASSED**
  - `TestValidationEngine::test_scenario_4_mathematical_inconsistency_and_line_item_math` — **PASSED**
  - `TestValidationEngine::test_scenario_5_exact_and_fuzzy_duplicate_detection` — **PASSED**
  - `TestValidationEngine::test_scenario_6_pdf_to_excel_cross_verification` — **PASSED**
  - `TestValidationEngine::test_scenario_7_pdf_to_pdf_cross_verification_po_variance` — **PASSED**

---

## 3. Critical Architectural Gap Analysis: The Illusion of 100% Pass Rate

While the existing test suite reports 100% passing tests, a rigorous architectural inspection reveals why this green status masks severe distributed system defects:

1. **Redis Queue Is Untested:** All integration tests (`test_api.py`, `test_api_db.py`) run with `USE_REDIS_QUEUE=false` or default in-memory `AsyncWorkerQueue`. `RedisJobQueue` has zero test coverage for actual Redis persistence, connection pooling, serialization, or worker consumption.
2. **Worker Process Is Untested:** There are no tests verifying `python -m src.api.worker` as an independent CLI entrypoint. In the current codebase, running that command does not start a consumer loop and exits immediately.
3. **AI LLM Extraction Is Untested in Live Execution:** `test_extraction.py` executes entirely against the local deterministic `HeuristicExtractor`. The Gemini integration (`google.genai`) is completely bypassed, and its dependency was undeclared in `pyproject.toml`.
4. **Failure & Recovery Paths Are Untested:** There are no tests covering Redis broker disconnection, worker crashes during active processing, idempotency under duplicate delivery, or database transaction rollbacks during worker failure.

---

## 4. Baseline Verdict & Next Actions

The baseline unit tests for ingestion, validation, heuristics, and basic REST endpoints are functional and must be preserved without regressions. However, the distributed runtime layers (Redis, Worker process, Docker containerization, Gemini integration) require immediate repair and end-to-end integration testing.
