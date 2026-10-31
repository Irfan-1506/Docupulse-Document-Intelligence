# PHASE 3 INITIAL CODEBASE AUDIT: AI EXTRACTION RELIABILITY ENGINEERING
**System:** DocuPulse Enterprise v4.3  
**Date:** October 31, 2026  
**Auditors:** Senior AI Engineer (Google GenAI SDK), Financial Data Validation Engineer, ML Evaluation Engineer  
**Status:** Approved for Implementation  

---

## 1. Executive Summary & Audit Objectives

Phase 3 transitions DocuPulse Enterprise v4.3 from deterministic heuristic baselines and prototype LLM interfaces into an **independently measured, enterprise-grade AI extraction system powered by Google Gemini (via the official `google-genai` SDK v2.33+)**. 

This audit evaluates the codebase across 10 critical reliability dimensions:
1. Existing Gemini provider implementation.
2. Existing heuristic extraction engine.
3. Provider selection and fallback mechanics.
4. Extraction schema expressiveness and null-safety.
5. Prompt engineering templates and anti-hallucination controls.
6. Financial consistency and ambiguous field handling.
7. Confidence calibration and uncertainty scoring.
8. Benchmark infrastructure and dataset provenance.
9. Automated test suites and regression risks.
10. End-to-end API and background worker integration.

---

## 2. Component-by-Component Architectural Audit

### 2.1 Gemini Integration (`src/extraction/engine.py`, `tests/unit/test_gemini_provider.py`)
- **Current State:** A basic integration using `google.genai.Client` exists in `AIExtractionEngine._extract_segment_with_gemini`.
- **Identified Deficiencies & Risks:**
  - *Hardcoded Timeout:* A hardcoded 30.0s timeout inside `asyncio.wait_for` ignores environment-specific latency requirements.
  - *Missing Exponential Backoff & Retries:* No rate-limit (`429 RESOURCE_EXHAUSTED`) or transient server error (`503 UNAVAILABLE`) retry logic is implemented.
  - *Zero Telemetry Capture:* Generation metadata (`response.usage_metadata.prompt_token_count`, `candidates_token_count`, `total_token_count`), server latency, and exact model version are unrecorded.
  - *Silent Heuristic Masquerading Risk:* Under error conditions, `_extract_segment_with_gemini` catches `Exception` and silently invokes `HeuristicExtractor.extract_from_segment`. While a warning is logged, naive benchmark loops could inadvertently evaluate heuristic results as Gemini successes.
  - *Environment Variable Deficiencies:* Only `GEMINI_API_KEY` and `GEMINI_MODEL` are read; `GEMINI_TEMPERATURE`, `GEMINI_MAX_RETRIES`, `GEMINI_TIMEOUT_SECONDS`, and `GEMINI_MAX_OUTPUT_TOKENS` are not supported.

### 2.2 Heuristic Extraction Engine (`src/extraction/engine.py`)
- **Current State:** `HeuristicExtractor` provides regex and rule-based parsing of currency, dates, invoice numbers, supplier/customer entities, and basic table rows.
- **Identified Deficiencies:**
  - Highly brittle against non-standard layouts, multi-column tables, rotated scans, and unstructured text blocks.
  - Does not support field extraction statuses (`extracted`, `uncertain`, `missing`, `conflicting`, `not_applicable`).
  - Lacks awareness of extended financial fields (tax IDs, purchase orders, shipping charges, payment terms, line-item discounts).

### 2.3 Provider Selection Logic (`src/extraction/engine.py`, `src/api/worker.py`)
- **Current State:** Uses `ExtractionProvider` enum (`gemini`, `openai`, `ollama`, `heuristic_fallback`).
- **Identified Deficiencies:**
  - Provenance contract does not strictly differentiate between requested provider, actual provider, fallback status, failure reason, and AI vs. heuristic generation flags.
  - A formal benchmark must isolate execution so that a Gemini run fails cleanly if unconfigured rather than falling back to heuristics.

### 2.4 Extraction Schemas (`src/extraction/schemas.py`)
- **Current State:** `InvoiceExtractionRecord` contains 8 top-level financial fields (`invoice_number`, `invoice_date`, `supplier_name`, `customer_name`, `subtotal`, `tax`, `total`, `currency`) and basic `LineItem` rows.
- **Identified Deficiencies:**
  - Lacks enterprise fields specified in Phase 3: `due_date`, `supplier_address`, `supplier_tax_id`, `customer_address`, `customer_tax_id`, `discount`, `tax_amount`, `shipping_amount`, `other_charges`, `total_amount`, `payment_terms`, `purchase_order_number`.
  - Line items lack `SKU`, `unit`, `discount`, `tax_rate`, and `tax_amount`.
  - Missing field-level metadata envelopes: Each field must preserve `extracted_value`, `normalized_value`, `source_evidence`, `source_page`, `source_location` (bounding box when available), `confidence`, and `extraction_status`.
  - Inconsistent null vs. zero representation: Several methods default numbers to `0.0` instead of `None`, which violates the Zero-Fabrication requirement.

### 2.5 Prompt Templates (`src/extraction/prompts.py`)
- **Current State:** Single monolithic prompt pair (`SYSTEM_EXTRACTION_PROMPT` and `DOCUMENT_SEGMENT_USER_PROMPT`).
- **Identified Deficiencies:**
  - No prompt versioning or SHA-256 integrity hashing.
  - Lacks experimental prompt strategies (Basic, Evidence-First, Structured, Evidence-First Structured) required for systematic comparative evaluation.
  - No explicit few-shot framing or negative constraints for adversarial document defense.

### 2.6 Source Evidence & Grounding Verifier (`src/extraction/grounding.py`)
- **Current State:** `GroundingVerifier` checks whether extracted floats or string tokens appear in raw text.
- **Identified Deficiencies:**
  - Grounding does not verify page number or bounding box locations.
  - Grounding operates as an all-or-nothing confidence penalty (-0.30) rather than a multi-factor groundedness score.
  - Does not distinguish between exact verbatim grounding and semantic/fuzzy entity matches.

### 2.7 Financial Validation & Ambiguous Fields (`src/validation/rules.py`)
- **Current State:** `FinancialRulesEngine` checks `subtotal + tax == total` using Python `Decimal`.
- **Identified Deficiencies:**
  - No explicit handling or dedicated test cases for ambiguous financial patterns: tax-inclusive totals, multiple tax rates, pre/post-tax discounts, negative amounts (credit notes), shared currency symbols (`$`, `¥`), comma vs. decimal notation (`1.250,00` vs `1,250.00`), shipping charges, or contradictory page totals.
  - The model currently guesses rather than returning `conflicting` or `uncertain` when genuine ambiguity exists.

### 2.8 Confidence Assessment & Calibration
- **Current State:** Confidences are hardcoded constants (e.g., `0.95` from regex matches, dropped to `0.65` on ungrounded checks).
- **Identified Deficiencies:**
  - No statistical confidence calibration.
  - Missing Expected Calibration Error (ECE), Brier score, and reliability diagrams.
  - Model self-reported confidence is conflated with independent empirical certainty.

### 2.9 Benchmark Implementation & Dataset Provenance (`src/evaluation/`, `tests/fixtures/golden_set/`)
- **Current State:** 54 synthetic documents in `tests/fixtures/golden_set/` evaluated with `src/evaluation/evaluator.py`.
- **Critical Audit Finding:** The previously reported **99.16% Macro F1** in Phase 1 was measured **entirely on synthetic documents using the deterministic HeuristicExtractor**; it was **not** a measurement of Gemini performance!
- **Identified Deficiencies:**
  - No provenance documentation separating real-world permissioned invoices from synthetic fixtures.
  - Lacks coverage across the 12 required document conditions (poor scans, photographic distortion, multi-page boundary splits, ambiguous tax, credit notes, etc.).
  - Benchmark does not measure hallucination rates, evidence-grounding rates, or uncertainty calibration.

---

## 3. Defect & Architectural Risk Register

| ID | Module / File | Root Cause | Severity | Proposed Remediation |
| :--- | :--- | :--- | :--- | :--- |
| **DEF-AI-01** | `src/extraction/engine.py` | Silent fallback to heuristics during LLM exception | **High** | Introduce explicit `ExecutionProvenance` contract; fail hard in benchmark mode when Gemini is requested. |
| **DEF-AI-02** | `src/extraction/engine.py` | Missing bounded retries with exponential backoff on 429/503 | **High** | Implement retry handler with jittered backoff using official `google-genai` SDK error types. |
| **DEF-AI-03** | `src/extraction/schemas.py` | Schema missing 10 required enterprise fields and line-item details | **Medium** | Upgrade to versioned `EnterpriseInvoiceSchemaV3` with per-field extraction envelope. |
| **DEF-AI-04** | `src/extraction/schemas.py` | Missing field-level extraction status (`extracted`, `uncertain`, etc.) | **Medium** | Implement `FieldEnvelope[T]` tracking status, normalized value, evidence, and confidence. |
| **DEF-AI-05** | `src/extraction/prompts.py` | Only one prompt exists; no prompt experimentation framework | **Medium** | Author 4 distinct prompt strategies (A/B/C/D) with SHA-256 versioning and metadata logging. |
| **DEF-AI-06** | `src/evaluation/metrics.py` | Missing hallucination, grounding, and calibration metric calculators | **High** | Implement ECE, Brier score, evidence grounding rate, and field-level hallucination rate. |
| **DEF-AI-07** | `src/evaluation/generator.py` | Synthetic golden set labeled without provenance or real-world split | **High** | Construct expanded 100+ document benchmark cataloging real-world vs synthetic provenance across 12 conditions. |

---

## 4. Phase 3 Implementation Roadmap

```
Step 1: Codebase Audit & Baseline Verification (COMPLETED)
   │
   ▼
Step 2: Strict Versioned Extraction Schema & Envelope (`src/extraction/schemas.py`)
   │  - 18 invoice-level fields + 9 line-item fields
   │  - Per-field status (extracted, uncertain, missing, conflicting, not_applicable)
   │  - Separate raw vs normalized values, source page & bounding box citations
   │
   ▼
Step 3: Resilient Gemini Provider Engine (`src/extraction/gemini_provider.py` & `engine.py`)
   │  - Official `google-genai` SDK v2.33+ with `Client(api_key=...)`
   │  - Structured Outputs with Pydantic JSON schema
   │  - Bounded retries (3 attempts) with exponential backoff & jitter
   │  - Configurable timeouts (`GEMINI_TIMEOUT_SECONDS`), temperature, max output tokens
   │  - Token usage, latency, and model version telemetry
   │  - Strict non-fallback benchmark mode
   │
   ▼
Step 4: Prompt Engineering Experiments (`src/extraction/prompts.py`)
   │  - Strategy A: Basic Extraction
   │  - Strategy B: Evidence-First Extraction
   │  - Strategy C: Structured Extraction
   │  - Strategy D: Evidence-First Structured Extraction
   │  - SHA-256 prompt hashing and configuration tracking
   │
   ▼
Step 5: Dataset Construction & Provenance Catalog (`evaluation/datasets/`)
   │  - Expand to 12 document categories (100+ independently labeled documents)
   │  - Ground truth records with dual-human annotation and adjudication
   │  - Explicit dataset manifest documenting provenance (Real vs Synthetic)
   │
   ▼
Step 6: Comprehensive Benchmark & Reliability Engine (`src/evaluation/`)
   │  - 3 configurations: Heuristic Baseline, Gemini Raw, Gemini Grounded
   │  - 16 formal metrics: Exact Match, Normalized Match, Numeric Accuracy, F1, Macro F1,
   │    Evidence Grounding Rate, Hallucination Rate, Latency, ECE, Brier Score
   │  - Ambiguous financial field suite & Adversarial robustness testing
   │
   ▼
Step 7: Automated Test Suite & Live API Integration (`tests/`)
   │  - Mocked unit tests for all error codes, retries, and schema mappings
   │  - Separate integration test suite for live Gemini execution
   │  - Full regression pass across Phase 1, Phase 2, and Phase 3 suites
   │
   ▼
Step 8: Final Engineering Report & Visualizations
   │  - Generate `reports/PHASE3_AI_RELIABILITY_REPORT.md` and `.pdf`
   │  - Empirical charts: Field F1, Provider Comparison, Grounding, Calibration, Latency
```

---

## 5. Certification of Audit Findings

The existing application architecture is sound and well-modularized. Phase 3 will not disrupt existing Phase 1 background workers or Phase 2 security controls, but will cleanly replace the prototype Gemini implementation with a hardened, telemetry-rich provider and an independently evaluated reliability framework.
