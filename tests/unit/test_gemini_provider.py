"""Unit test suite for Google Gemini extraction provider integration.

Verifies:
1. Client initialization with Google GenAI SDK (google-genai).
2. Structured output parsing into InvoiceExtractionRecord schema.
3. Accurate model provenance tracking without silent heuristic masquerading.
4. Error handling and safe fallback to deterministic heuristic extraction.
5. Live Gemini API execution (conditionally executed if GEMINI_API_KEY is available).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch
import pytest

from src.extraction.chunker import InvoiceSegment
from src.extraction.engine import AIExtractionEngine
from src.extraction.schemas import (
    DocumentExtractionResponse,
    ExtractionConfig,
    ExtractionProvider,
    InvoiceExtractionRecord,
    LineItem,
)
from src.ingestion.schemas import DocumentProcessingResult, DocumentStatus, ExtractionMethod, PageContent


@pytest.fixture
def sample_invoice_segment() -> InvoiceSegment:
    """Create a sample InvoiceSegment representing an extracted PDF page."""
    text = (
        "INVOICE\n"
        "Invoice Number: INV-GEMINI-2026\n"
        "Date: 2026-03-25\n"
        "Vendor: Apex Cloud Systems\n"
        "Bill To: Acme Global Enterprises\n\n"
        "Items:\n"
        "GPU Cluster Compute (100 hrs)  $50.00  $5,000.00\n\n"
        "Subtotal: $5,000.00\n"
        "Tax (10%): $500.00\n"
        "Total Amount: $5,500.00\n"
    )
    return InvoiceSegment(
        segment_id="seg-gemini-001",
        document_id="doc-gemini-001",
        page_numbers=[1],
        source_reference="Page 1",
        combined_text=text,
        tables=[],
    )


@pytest.fixture
def sample_document_result(sample_invoice_segment) -> DocumentProcessingResult:
    """Create a DocumentProcessingResult containing one page."""
    page = PageContent(
        page_number=1,
        raw_text=sample_invoice_segment.combined_text,
        extraction_method=ExtractionMethod.DIGITAL_PDF,
        confidence_score=1.0,
    )
    return DocumentProcessingResult(
        document_id="doc-gemini-001",
        file_name="invoice_gemini.pdf",
        file_type="PDF",
        file_hash="hash_gemini_12345",
        status=DocumentStatus.SUCCESS,
        total_pages_or_sheets=1,
        pages=[page],
        full_merged_text=sample_invoice_segment.combined_text,
        processing_time_ms=120.0,
    )


def test_gemini_client_initialization_with_key(monkeypatch):
    """Test that GenAI client initializes when GEMINI_API_KEY is present."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key-12345")
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    engine = AIExtractionEngine()
    assert engine.config.provider == ExtractionProvider.GEMINI
    assert engine.config.model_name == "gemini-2.5-flash"
    assert engine._gemini_client is not None


def test_gemini_client_absent_key_handling(monkeypatch):
    """Test that engine handles missing GEMINI_API_KEY without crashing."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gemini")

    engine = AIExtractionEngine()
    assert engine.config.provider == ExtractionProvider.GEMINI
    assert engine._gemini_client is None


@pytest.mark.asyncio
async def test_gemini_structured_extraction_success_mock(
    sample_document_result, monkeypatch
):
    """Test successful Gemini structured JSON extraction and provenance reporting."""
    monkeypatch.setenv("GEMINI_API_KEY", "mock-key")
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    engine = AIExtractionEngine()

    mock_invoice_data = {
        "invoice_number": "INV-GEMINI-2026",
        "invoice_date": "2026-03-25",
        "supplier_name": "Apex Cloud Systems",
        "customer_name": "Acme Global Enterprises",
        "currency": "USD",
        "subtotal": 5000.00,
        "tax": 500.00,
        "total": 5500.00,
        "line_items": [
            {
                "description": "GPU Cluster Compute (100 hrs)",
                "quantity": 100.0,
                "unit_price": 50.0,
                "total": 5000.0,
            }
        ],
        "extraction_confidence": 0.98,
        "uncertain_fields": [],
        "missing_required_fields": [],
        "extraction_warnings": [],
    }

    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_invoice_data)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    engine._gemini_client = mock_client

    response = await engine.extract_document_async(sample_document_result)

    assert isinstance(response, DocumentExtractionResponse)
    assert response.total_invoices_detected == 1
    assert response.model_used == "gemini:gemini-2.5-flash"
    assert response.telemetry["requested_provider"] == "gemini"
    assert response.telemetry["fallback_invoked"] is False
    assert "gemini" in response.telemetry["actual_providers"]

    rec = response.records[0]
    assert rec.invoice_number == "INV-GEMINI-2026"
    assert rec.total == 5500.00
    assert rec.subtotal == 5000.00
    assert rec.supplier_name == "Apex Cloud Systems"


@pytest.mark.asyncio
async def test_gemini_api_failure_triggers_honest_fallback_provenance(
    sample_document_result, monkeypatch
):
    """Test that API failure falls back to heuristic extractor with transparent provenance."""
    monkeypatch.setenv("GEMINI_API_KEY", "mock-key")
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gemini")

    engine = AIExtractionEngine()

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("Simulated Gemini Quota/Timeout Error")
    engine._gemini_client = mock_client

    response = await engine.extract_document_async(sample_document_result)

    assert isinstance(response, DocumentExtractionResponse)
    # Provenance MUST NOT claim it was processed by Gemini
    assert "fallback from gemini" in response.model_used
    assert response.telemetry["fallback_invoked"] is True
    assert "heuristic_fallback" in response.telemetry["actual_providers"]

    # Record should still be extracted via heuristic fallback
    assert len(response.records) == 1
    rec = response.records[0]
    assert rec.invoice_number == "INV-GEMINI-2026"
    assert any("fallback invoked" in w.lower() for w in rec.extraction_warnings)


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"),
    reason="Requires active GEMINI_API_KEY environment variable for live execution.",
)
@pytest.mark.asyncio
async def test_gemini_live_api_extraction(sample_document_result):
    """Live integration test against Google Gemini API (run when key is present)."""
    config = ExtractionConfig(
        provider=ExtractionProvider.GEMINI,
        model_name=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    )
    engine = AIExtractionEngine(config=config)
    assert engine._gemini_client is not None

    response = await engine.extract_document_async(sample_document_result)
    assert response.total_invoices_detected >= 1
    assert "gemini" in response.model_used.lower()
