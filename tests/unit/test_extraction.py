"""Comprehensive unit test suite for AI Information Extraction Engine (Task 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import openpyxl
import pymupdf
import pytest

from src.ingestion.router import DocumentIngestionEngine
from src.extraction.chunker import (
    DocumentBoundarySplitter,
    InvoiceSegment,
    SpreadsheetInvoiceMapper,
)
from src.extraction.engine import AIExtractionEngine, HeuristicExtractor
from src.extraction.grounding import GroundingVerifier
from src.extraction.schemas import (
    DocumentExtractionResponse,
    ExtractionConfig,
    ExtractionProvider,
    InvoiceExtractionRecord,
    LineItem,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def complete_digital_invoice_pdf(tmp_path: Path) -> Path:
    """Generate a single complete invoice PDF with supplier, customer, dates, totals, and tables."""
    pdf_path = tmp_path / "complete_invoice_8801.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    # Header / Vendor
    page.insert_text(pymupdf.Point(50, 50), "GLOBAL ENTERPRISE SUPPLIES CORP", fontsize=14)
    page.insert_text(pymupdf.Point(50, 70), "100 Financial Way, New York, NY 10001", fontsize=9)
    page.insert_text(pymupdf.Point(50, 85), "Vendor ID: VEND-992", fontsize=9)

    # Title & Invoice details
    page.insert_text(pymupdf.Point(400, 50), "TAX INVOICE", fontsize=14)
    page.insert_text(pymupdf.Point(400, 70), "Invoice No: INV-2026-8801", fontsize=10)
    page.insert_text(pymupdf.Point(400, 85), "Invoice Date: 2026-10-15", fontsize=10)
    page.insert_text(pymupdf.Point(400, 100), "Currency: USD", fontsize=10)

    # Customer
    page.insert_text(pymupdf.Point(50, 130), "Bill To: ACME LOGISTICS INC", fontsize=11)
    page.insert_text(pymupdf.Point(50, 145), "742 Evergreen Terrace, Springfield", fontsize=9)

    # Structured Table Grid
    table_rect = pymupdf.Rect(50, 180, 545, 300)
    page.draw_rect(table_rect, color=(0.2, 0.2, 0.2), width=1)
    page.draw_line(pymupdf.Point(50, 210), pymupdf.Point(545, 210), color=(0.2, 0.2, 0.2), width=1)
    page.draw_line(pymupdf.Point(50, 240), pymupdf.Point(545, 240), color=(0.7, 0.7, 0.7), width=0.5)
    page.draw_line(pymupdf.Point(50, 270), pymupdf.Point(545, 270), color=(0.7, 0.7, 0.7), width=0.5)

    # Vertical separators
    page.draw_line(pymupdf.Point(260, 180), pymupdf.Point(260, 300), color=(0.2, 0.2, 0.2), width=1)
    page.draw_line(pymupdf.Point(340, 180), pymupdf.Point(340, 300), color=(0.2, 0.2, 0.2), width=1)
    page.draw_line(pymupdf.Point(440, 180), pymupdf.Point(440, 300), color=(0.2, 0.2, 0.2), width=1)

    # Table Text
    page.insert_text(pymupdf.Point(60, 200), "Item Description", fontsize=10)
    page.insert_text(pymupdf.Point(270, 200), "Quantity", fontsize=10)
    page.insert_text(pymupdf.Point(350, 200), "Unit Price", fontsize=10)
    page.insert_text(pymupdf.Point(450, 200), "Line Total", fontsize=10)

    page.insert_text(pymupdf.Point(60, 230), "High-Density Server Blades", fontsize=9)
    page.insert_text(pymupdf.Point(270, 230), "10", fontsize=9)
    page.insert_text(pymupdf.Point(350, 230), "2,500.00", fontsize=9)
    page.insert_text(pymupdf.Point(450, 230), "25,000.00", fontsize=9)

    page.insert_text(pymupdf.Point(60, 260), "Optic Transceiver Modules", fontsize=9)
    page.insert_text(pymupdf.Point(270, 260), "50", fontsize=9)
    page.insert_text(pymupdf.Point(350, 260), "200.00", fontsize=9)
    page.insert_text(pymupdf.Point(450, 260), "10,000.00", fontsize=9)

    page.insert_text(pymupdf.Point(60, 290), "Enterprise Rack Enclosures", fontsize=9)
    page.insert_text(pymupdf.Point(270, 290), "2", fontsize=9)
    page.insert_text(pymupdf.Point(350, 290), "2,500.00", fontsize=9)
    page.insert_text(pymupdf.Point(450, 290), "5,000.00", fontsize=9)

    # Totals block
    page.insert_text(pymupdf.Point(350, 330), "Subtotal: $40,000.00", fontsize=10)
    page.insert_text(pymupdf.Point(350, 350), "Tax Amount: $5,250.00", fontsize=10)
    page.insert_text(pymupdf.Point(350, 375), "Total Due: $45,250.00", fontsize=12)

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def missing_and_garbled_invoice_pdf(tmp_path: Path) -> Path:
    """Generate an invoice with missing tax, missing customer, and garbled characters."""
    pdf_path = tmp_path / "missing_and_garbled.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    # Vendor & title
    page.insert_text(pymupdf.Point(50, 60), "TECH HARDWARE CORP", fontsize=14)
    page.insert_text(pymupdf.Point(50, 90), "Invoice No: INV-2026-9912", fontsize=10)
    page.insert_text(pymupdf.Point(50, 110), "Invoice Date: 2026-09-20", fontsize=10)
    page.insert_text(pymupdf.Point(50, 130), "Currency: USD", fontsize=10)

    # NOTICE: No Customer Name, No Tax!
    # Garbled OCR total amount
    page.insert_text(pymupdf.Point(50, 180), "Subtotal: $12,000.00", fontsize=11)
    page.insert_text(pymupdf.Point(50, 210), "Total Due: $1?,000.00", fontsize=11)

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def multi_invoice_multi_page_pdf(tmp_path: Path) -> Path:
    """Generate a 5-page PDF containing 2 distinct multi-page invoices."""
    pdf_path = tmp_path / "multi_invoice_5_pages.pdf"
    doc = pymupdf.open()

    # --- Invoice 1: Page 1 ---
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text(pymupdf.Point(50, 50), "ALPHA CLOUD SYSTEMS LLC", fontsize=14)
    p1.insert_text(pymupdf.Point(400, 50), "TAX INVOICE", fontsize=12)
    p1.insert_text(pymupdf.Point(400, 70), "Invoice No: INV-101", fontsize=10)
    p1.insert_text(pymupdf.Point(400, 85), "Date: 2026-08-01", fontsize=10)
    p1.insert_text(pymupdf.Point(50, 100), "Bill To: Delta Logistics", fontsize=10)
    p1.insert_text(pymupdf.Point(50, 130), "Page 1 of 2", fontsize=10)
    p1.insert_text(pymupdf.Point(50, 200), "Cloud Hosting Tier 3 Provisioning...", fontsize=10)

    # --- Invoice 1: Page 2 ---
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text(pymupdf.Point(50, 50), "ALPHA CLOUD SYSTEMS LLC - Continued", fontsize=11)
    p2.insert_text(pymupdf.Point(400, 50), "Invoice No: INV-101", fontsize=10)
    p2.insert_text(pymupdf.Point(50, 80), "Page 2 of 2", fontsize=10)
    p2.insert_text(pymupdf.Point(350, 200), "Subtotal: $1,000.00", fontsize=10)
    p2.insert_text(pymupdf.Point(350, 220), "Tax Amount: $200.00", fontsize=10)
    p2.insert_text(pymupdf.Point(350, 250), "Total Due: $1,200.00", fontsize=12)

    # --- Invoice 2: Page 1 ---
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text(pymupdf.Point(50, 50), "BETA CONSULTING GROUP INC", fontsize=14)
    p3.insert_text(pymupdf.Point(400, 50), "TAX INVOICE", fontsize=12)
    p3.insert_text(pymupdf.Point(400, 70), "Invoice No: INV-102", fontsize=10)
    p3.insert_text(pymupdf.Point(400, 85), "Date: 2026-08-15", fontsize=10)
    p3.insert_text(pymupdf.Point(50, 100), "Bill To: Omega Retail Corp", fontsize=10)
    p3.insert_text(pymupdf.Point(50, 130), "Page 1 of 3", fontsize=10)
    p3.insert_text(pymupdf.Point(50, 200), "Professional Risk Assessment Services...", fontsize=10)

    # --- Invoice 2: Page 2 ---
    p4 = doc.new_page(width=595, height=842)
    p4.insert_text(pymupdf.Point(50, 50), "BETA CONSULTING GROUP INC - Analysis Details", fontsize=11)
    p4.insert_text(pymupdf.Point(400, 50), "Invoice No: INV-102", fontsize=10)
    p4.insert_text(pymupdf.Point(50, 80), "Page 2 of 3", fontsize=10)

    # --- Invoice 2: Page 3 ---
    p5 = doc.new_page(width=595, height=842)
    p5.insert_text(pymupdf.Point(50, 50), "BETA CONSULTING GROUP INC - Final Summary", fontsize=11)
    p5.insert_text(pymupdf.Point(400, 50), "Invoice No: INV-102", fontsize=10)
    p5.insert_text(pymupdf.Point(50, 80), "Page 3 of 3", fontsize=10)
    p5.insert_text(pymupdf.Point(350, 200), "Subtotal: $3,000.00", fontsize=10)
    p5.insert_text(pymupdf.Point(350, 220), "Tax Amount: $450.00", fontsize=10)
    p5.insert_text(pymupdf.Point(350, 250), "Total Due: $3,450.00", fontsize=12)

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestAIExtractionEngine:
    """Test suite executing the core requirements of Task 2."""

    def test_complete_digital_invoice_extraction(self, complete_digital_invoice_pdf: Path) -> None:
        """Scenario 1: Full digital invoice extraction with structured line items and verified citations."""
        ingestion_engine = DocumentIngestionEngine()
        ingest_res = ingestion_engine.process_file(complete_digital_invoice_pdf)

        extraction_engine = AIExtractionEngine()
        response = extraction_engine.extract_document(ingest_res)

        assert response.total_invoices_detected == 1
        assert len(response.records) == 1

        rec = response.records[0]
        assert rec.invoice_number == "INV-2026-8801"
        assert rec.invoice_date == "2026-10-15"
        assert rec.currency == "USD"
        assert rec.subtotal == 40000.0
        assert rec.tax == 5250.0
        assert rec.total == 45250.0

        # Entity assertions
        assert "GLOBAL ENTERPRISE" in str(rec.supplier_name)
        assert "ACME LOGISTICS" in str(rec.customer_name)

        # Line items assertions
        assert len(rec.line_items) == 3
        descriptions = [item.description for item in rec.line_items]
        assert any("Server Blades" in d for d in descriptions if d)
        assert any("Transceiver" in d for d in descriptions if d)

        # Confidence & Grounding
        assert rec.field_confidences["total"] >= 0.85
        assert rec.field_confidences["invoice_number"] >= 0.85
        assert len(rec.missing_required_fields) == 0
        assert len(rec.uncertain_fields) == 0

    def test_missing_and_uncertain_fields_zero_fabrication(
        self, missing_and_garbled_invoice_pdf: Path
    ) -> None:
        """Scenario 2: Zero fabrication rule (missing tax & customer -> null, garbled number -> null/uncertain)."""
        ingestion_engine = DocumentIngestionEngine()
        ingest_res = ingestion_engine.process_file(missing_and_garbled_invoice_pdf)

        extraction_engine = AIExtractionEngine()
        response = extraction_engine.extract_document(ingest_res)

        assert response.total_invoices_detected == 1
        rec = response.records[0]

        # 1. Zero Fabrication: Tax was NOT on document -> MUST be None!
        assert rec.tax is None
        # 2. Customer Name was NOT on document -> MUST be None!
        assert rec.customer_name is None

        # 3. Garbled OCR amount ($1?,000.00) -> MUST NOT be hallucinated!
        assert rec.total is None or "total" in rec.uncertain_fields

        # 4. Warnings and missing fields
        assert any("Garbled" in w or "ambiguous" in w for w in rec.extraction_warnings)
        assert "customer_name" in rec.missing_required_fields or "total" in rec.missing_required_fields

    def test_multi_invoice_multi_page_pdf_segmentation(
        self, multi_invoice_multi_page_pdf: Path
    ) -> None:
        """Scenario 3: Boundary detection across multi-page document yielding distinct invoice records."""
        ingestion_engine = DocumentIngestionEngine()
        ingest_res = ingestion_engine.process_file(multi_invoice_multi_page_pdf)

        extraction_engine = AIExtractionEngine()
        response = extraction_engine.extract_document(ingest_res)

        assert response.total_invoices_detected == 2
        assert len(response.records) == 2

        # Invoice 1 (pages 1-2)
        inv1 = response.records[0]
        assert inv1.invoice_number == "INV-101"
        assert "pages 1-2" in inv1.page_range_or_sheet
        assert "ALPHA CLOUD" in str(inv1.supplier_name)
        assert inv1.total == 1200.0
        assert inv1.tax == 200.0

        # Invoice 2 (pages 3-5)
        inv2 = response.records[1]
        assert inv2.invoice_number == "INV-102"
        assert "pages 3-5" in inv2.page_range_or_sheet
        assert "BETA CONSULTING" in str(inv2.supplier_name)
        assert inv2.total == 3450.0
        assert inv2.tax == 450.0

    def test_spreadsheet_and_csv_multi_invoice_extraction(self, tmp_path: Path) -> None:
        """Scenario 4: Direct structured spreadsheet multi-invoice extraction."""
        csv_file = tmp_path / "disbursements.csv"
        csv_content = (
            "Invoice #;Vendor;Cleared Date;Disbursement Amount;Currency\n"
            "INV-9001;Zürich Reinsurance;2026-08-14;45000.50;EUR\n"
            "INV-9002;Montréal Logistics Inc;2026-08-15;12400.00;CAD\n"
            "INV-9003;São Paulo IT Consulting;2026-08-16;8750.25;USD\n"
        )
        csv_file.write_text(csv_content, encoding="utf-8")

        ingestion_engine = DocumentIngestionEngine()
        ingest_res = ingestion_engine.process_file(csv_file)

        extraction_engine = AIExtractionEngine()
        response = extraction_engine.extract_document(ingest_res)

        assert response.total_invoices_detected == 3
        assert len(response.records) == 3

        r1 = response.records[0]
        assert r1.invoice_number == "INV-9001"
        assert "Zürich" in str(r1.supplier_name) or "Reinsurance" in str(r1.supplier_name)
        assert r1.total == 45000.50
        assert r1.currency == "EUR"
        assert r1.field_confidences["total"] == 1.0

        r2 = response.records[1]
        assert r2.invoice_number == "INV-9002"
        assert r2.total == 12400.00
        assert r2.currency == "CAD"

        r3 = response.records[2]
        assert r3.invoice_number == "INV-9003"
        assert r3.total == 8750.25
        assert r3.currency == "USD"

    def test_grounding_verifier_rejects_hallucinated_total(self) -> None:
        """Scenario 5: Grounding verifier rejects fabricated number that is absent from source text."""
        source_text = "Vendor: Acme Corp\nInvoice No: INV-123\nSubtotal: $500.00\nTotal: $500.00"

        # Construct a record with a hallucinated total
        record = InvoiceExtractionRecord(
            record_id="test-rec",
            source_document_id="test-doc",
            page_range_or_sheet="page 1",
            invoice_number="INV-123",
            total=999999.99,  # Hallucinated!
            supplier_name="Acme Corp",
            field_confidences={"total": 0.95, "invoice_number": 0.95},
        )

        verifier = GroundingVerifier()
        verified = verifier.verify_and_harden_record(record, source_text=source_text)

        # Total MUST be purged to None because 999999.99 is not in source_text!
        assert verified.total is None
        assert verified.field_confidences["total"] == 0.0
        assert "total" in verified.uncertain_fields
        assert any("Hallucination rejected" in w for w in verified.extraction_warnings)

    def test_ocr_degradation_warning_propagation(self) -> None:
        """Verify low Task 1 OCR quality (<0.60) is propagated to field confidences and warnings."""
        source_text = "Invoice No: INV-99\nTotal: 100.00\nSupplier: Acme"
        record = InvoiceExtractionRecord(
            record_id="test-rec",
            source_document_id="test-doc",
            page_range_or_sheet="page 1",
            invoice_number="INV-99",
            total=100.00,
            supplier_name="Acme",
            field_confidences={"total": 0.95, "invoice_number": 0.90, "supplier_name": 0.90},
        )

        verifier = GroundingVerifier()
        # Pass OCR confidence = 0.45
        verified = verifier.verify_and_harden_record(
            record,
            source_text=source_text,
            ocr_confidence=0.45,
            ocr_warnings=["Low OCR confidence score: 0.45 (< 0.60)."],
        )

        # Field confidences must be capped by the low OCR confidence
        assert verified.field_confidences["total"] <= 0.45
        assert "total" in verified.uncertain_fields
        assert any("OCR quality" in w for w in verified.extraction_warnings)
