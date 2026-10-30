"""Comprehensive unit and integration test suite for Document Ingestion Engine."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Generator

import cv2
import numpy as np
import openpyxl
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
import pymupdf
import pytest

from src.ingestion.ocr_processor import ImagePreprocessor, OCRProcessor, RapidOCREngine
from src.ingestion.pdf_processor import PDFProcessor
from src.ingestion.router import DocumentIngestionEngine
from src.ingestion.schemas import (
    DocumentProcessingResult,
    DocumentStatus,
    ExtractionMethod,
    IngestionConfig,
    PageContent,
    SheetContent,
)
from src.ingestion.spreadsheet_processor import SpreadsheetProcessor
from src.ingestion.validators import (
    compute_sha256,
    validate_file_integrity,
)


# ---------------------------------------------------------------------------
# Fixture Generators
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Provide a clean temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def digital_pdf_path(temp_dir: Path) -> Path:
    """Generate a 3-page native digital PDF with structured financial tables."""
    pdf_path = temp_dir / "financial_statement_digital.pdf"
    doc = pymupdf.open()

    # --- Page 1: Financial Overview & Balance Sheet Table ---
    page1 = doc.new_page(width=595, height=842)  # A4 size
    page1.insert_text(
        pymupdf.Point(50, 60),
        "GLOBAL FINANCIAL ASSETS & HOLDINGS CORP\n"
        "Quarterly Financial Statement - Consolidated Balance Sheet",
        fontsize=14,
    )
    page1.insert_text(
        pymupdf.Point(50, 110),
        "The following table summarizes current asset distributions and liquid cash reserves\n"
        "audited in compliance with International Financial Reporting Standards (IFRS 9).",
        fontsize=10,
    )

    # Draw structured table grid
    table_rect = pymupdf.Rect(50, 140, 545, 260)
    page1.draw_rect(table_rect, color=(0.2, 0.2, 0.2), width=1)
    page1.draw_line(pymupdf.Point(50, 170), pymupdf.Point(545, 170), color=(0.2, 0.2, 0.2), width=1)
    page1.draw_line(pymupdf.Point(50, 200), pymupdf.Point(545, 200), color=(0.7, 0.7, 0.7), width=0.5)
    page1.draw_line(pymupdf.Point(50, 230), pymupdf.Point(545, 230), color=(0.7, 0.7, 0.7), width=0.5)
    page1.draw_line(pymupdf.Point(200, 140), pymupdf.Point(200, 260), color=(0.2, 0.2, 0.2), width=1)
    page1.draw_line(pymupdf.Point(340, 140), pymupdf.Point(340, 260), color=(0.2, 0.2, 0.2), width=1)
    page1.draw_line(pymupdf.Point(440, 140), pymupdf.Point(440, 260), color=(0.2, 0.2, 0.2), width=1)

    # Table Text
    page1.insert_text(pymupdf.Point(60, 160), "Asset Category", fontsize=10)
    page1.insert_text(pymupdf.Point(210, 160), "Liquidity Tier", fontsize=10)
    page1.insert_text(pymupdf.Point(350, 160), "Valuation (USD)", fontsize=10)
    page1.insert_text(pymupdf.Point(450, 160), "Audit Status", fontsize=10)

    page1.insert_text(pymupdf.Point(60, 190), "Cash & Equivalents", fontsize=9)
    page1.insert_text(pymupdf.Point(210, 190), "Tier 1 Liquid", fontsize=9)
    page1.insert_text(pymupdf.Point(350, 190), "45,250,000", fontsize=9)
    page1.insert_text(pymupdf.Point(450, 190), "Verified", fontsize=9)

    page1.insert_text(pymupdf.Point(60, 220), "Treasury Securities", fontsize=9)
    page1.insert_text(pymupdf.Point(210, 220), "Tier 1 Liquid", fontsize=9)
    page1.insert_text(pymupdf.Point(350, 220), "128,400,000", fontsize=9)
    page1.insert_text(pymupdf.Point(450, 220), "Verified", fontsize=9)

    page1.insert_text(pymupdf.Point(60, 250), "Corporate Debt", fontsize=9)
    page1.insert_text(pymupdf.Point(210, 250), "Tier 2 High Yield", fontsize=9)
    page1.insert_text(pymupdf.Point(350, 250), "34,100,000", fontsize=9)
    page1.insert_text(pymupdf.Point(450, 250), "Pending", fontsize=9)

    # --- Page 2: Income Statement Notes ---
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text(
        pymupdf.Point(50, 60),
        "NOTE 4: OPERATING REVENUES AND EXPENSE DISCLOSURES",
        fontsize=12,
    )
    page2.insert_text(
        pymupdf.Point(50, 100),
        "Total group revenue for the trailing twelve months grew by 18.4% to $842 million.\n"
        "Operating margins remained stable at 24.2% despite macroeconomic headwinds.\n"
        "Capital expenditures were primarily directed towards high-performance computing clusters\n"
        "and automated algorithmic risk management systems.",
        fontsize=10,
    )

    # --- Page 3: Auditor Certification ---
    page3 = doc.new_page(width=595, height=842)
    page3.insert_text(
        pymupdf.Point(50, 60),
        "INDEPENDENT AUDITOR'S REPORT AND SIGN-OFF",
        fontsize=12,
    )
    page3.insert_text(
        pymupdf.Point(50, 100),
        "To the Shareholders of Global Financial Assets & Holdings Corp:\n"
        "We have audited the accompanying consolidated financial statements.\n"
        "In our opinion, the statements present fairly, in all material respects, the financial position.",
        fontsize=10,
    )

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def noisy_rotated_image_path(temp_dir: Path) -> Path:
    """Generate a skewed, noisy receipt/invoice image to test deskew and OCR restoration."""
    img_path = temp_dir / "invoice_rotated_noisy.png"

    # 1. Create clean image with crisp invoice text
    w, h = 600, 300
    base = np.ones((h, w, 3), dtype=np.uint8) * 255
    cv2.putText(base, "INVOICE #98421", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
    cv2.putText(base, "TOTAL DUE: $7,850.00", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    cv2.putText(base, "DUE DATE: 2026-11-30", (40, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    cv2.putText(base, "PAYMENT METHOD: WIRE TRANSFER", (40, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)

    # 2. Rotate by 6.5 degrees
    center = (w // 2, h // 2)
    rot_mat = cv2.getRotationMatrix2D(center, 6.5, 1.0)
    rotated = cv2.warpAffine(
        base,
        rot_mat,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )

    # 3. Add Gaussian noise to simulate low-quality scan
    noise = np.random.normal(0, 12, (h, w, 3)).astype(np.int16)
    noisy = np.clip(rotated.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    cv2.imwrite(str(img_path), noisy)
    return img_path


@pytest.fixture
def multi_sheet_excel_path(temp_dir: Path) -> Path:
    """Generate a multi-sheet financial Excel workbook with title banners and empty rows."""
    xlsx_path = temp_dir / "portfolio_holdings.xlsx"
    wb = openpyxl.Workbook()

    # Sheet 1: Equities (with banner on rows 1-2)
    ws1 = wb.active
    ws1.title = "Equities Portfolio"
    ws1.append(["ACME CAPITAL ASSET MANAGEMENT"])  # Title banner 1
    ws1.append(["CONFIDENTIAL - FOR INTERNAL USE ONLY"])  # Title banner 2
    ws1.append([])  # Empty row
    ws1.append(["Ticker", "Asset Class", "Shares", "Avg Cost", "Market Value"])  # Actual header
    ws1.append(["AAPL", "Equities", 1500, 175.50, 263250.00])
    ws1.append(["GOOGL", "Equities", 850, 160.20, 136170.00])
    ws1.append(["MSFT", "Equities", 1200, 410.00, 492000.00])
    ws1.append(["NVDA", "Equities", 2000, 115.00, 230000.00])

    # Sheet 2: Fixed Income (with banner)
    ws2 = wb.create_sheet(title="Fixed Income")
    ws2.append(["FIXED INCOME SECURITIES LEDGER"])
    ws2.append([])
    ws2.append(["Bond Description", "Maturity Date", "Coupon", "Par Value", "Yield to Maturity"])
    ws2.append(["US Treasury 10Y", "2036-05-15", 0.0425, 500000, 0.0431])
    ws2.append(["Corp AAA Bond", "2029-10-01", 0.0510, 250000, 0.0515])
    ws2.append(["Municipal Bond", "2031-03-31", 0.0380, 150000, 0.0390])

    wb.save(str(xlsx_path))
    return xlsx_path


@pytest.fixture
def dirty_csv_path(temp_dir: Path) -> Path:
    """Generate a dirty CSV with non-standard delimiter (;), top banner, and Latin-1 encoding."""
    csv_path = temp_dir / "vendor_disbursements_dirty.csv"
    content = (
        "NORTH AMERICA VENDOR PAYMENTS EXPORT\n"
        "BATCH RUN #2026-991A\n"
        "====================================\n"
        "Transaction ID;Vendor Name;Disbursement Amount;Currency;Status;Cleared Date\n"
        "TXN-1001;Zürich Reinsurance;45000.50;EUR;Completed;2026-08-14\n"
        "TXN-1002;Montréal Logistics Inc;12400.00;CAD;Completed;2026-08-15\n"
        "TXN-1003;São Paulo IT Consulting;8750.25;USD;Pending;\n"
        "TXN-1004;Tokyo Datacenters;31200.00;USD;Completed;2026-08-16\n"
    )
    # Write as Latin-1 with semicolon delimiters
    with open(csv_path, "wb") as f:
        f.write(content.encode("latin-1"))
    return csv_path


@pytest.fixture
def corrupted_files(temp_dir: Path) -> dict[str, Path]:
    """Generate diverse corrupted files to test isolation guard."""
    files: dict[str, Path] = {}

    # Zero-byte PDF
    zero_file = temp_dir / "zero_bytes.pdf"
    zero_file.touch()
    files["zero_bytes"] = zero_file

    # Truncated PDF (broken EOF and missing xref)
    trunc_pdf = temp_dir / "truncated.pdf"
    with open(trunc_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%TRUNCATED")
    files["truncated_pdf"] = trunc_pdf

    # Magic byte mismatch / corrupted XLSX (valid ZIP signature but corrupt payload)
    corrupt_xlsx = temp_dir / "corrupted_archive.xlsx"
    with open(corrupt_xlsx, "wb") as f:
        f.write(b"PK\x03\x04\x14\x00\x00\x00\x08\x00ThisIsNotAValidZipStructureAtAll!!")
    files["corrupt_xlsx"] = corrupt_xlsx

    # Corrupted image (PNG signature followed by garbage)
    corrupt_png = temp_dir / "broken_header.png"
    with open(corrupt_png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRGarbageDataCorruptChunk")
    files["corrupt_png"] = corrupt_png

    # Unsupported format
    unsupported = temp_dir / "malicious_script.exe"
    with open(unsupported, "wb") as f:
        f.write(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00")
    files["unsupported"] = unsupported

    return files


@pytest.fixture
def hybrid_pdf_path(temp_dir: Path) -> Path:
    """Generate a hybrid 2-page PDF: Page 1 is digital text, Page 2 is a full-page raster scan."""
    pdf_path = temp_dir / "hybrid_document.pdf"
    doc = pymupdf.open()

    # Page 1: Native Digital Text
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text(
        pymupdf.Point(50, 80),
        "DIGITAL EXECUTIVE SUMMARY\n\n"
        "This page contains full vector font native digital text representing\n"
        "high density financial metrics. Over forty characters are present.\n"
        "Net Operating Profit After Tax (NOPAT): $12,450,000.\n"
        "Weighted Average Cost of Capital (WACC): 7.85%.\n"
        "Return on Invested Capital (ROIC): 16.20%.",
        fontsize=11,
    )

    # Page 2: Full-Page Scanned Raster Image Overlay (no digital text)
    p2 = doc.new_page(width=595, height=842)
    # Create raster bitmap in memory
    scan_img = np.ones((842, 595, 3), dtype=np.uint8) * 255
    cv2.putText(scan_img, "SCANNED RECEIPT ATTACHMENT", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2)
    cv2.putText(scan_img, "STATIONERY EXPENSE: $425.50", (50, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2)
    cv2.putText(scan_img, "APPROVED BY: FINANCE CONTROLLER", (50, 280), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)

    # Insert raster image into page 2 covering full page
    _, img_bytes = cv2.imencode(".png", scan_img)
    p2.insert_image(p2.rect, stream=img_bytes.tobytes())

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestDocumentIngestionEngine:
    """Test suite executing the core requirements of Task 1."""

    def test_scenario_1_multi_page_digital_pdf(self, digital_pdf_path: Path) -> None:
        """Scenario 1: Digital PDF extraction with table detection and page metadata."""
        engine = DocumentIngestionEngine()
        result = engine.process_file(digital_pdf_path)

        assert result.status == DocumentStatus.SUCCESS
        assert result.file_type == "pdf"
        assert result.total_pages_or_sheets == 3
        assert len(result.pages) == 3
        assert result.processing_time_ms > 0

        # Page 1 verification: native digital extraction
        p1 = result.pages[0]
        assert p1.page_number == 1
        assert p1.extraction_method == ExtractionMethod.DIGITAL_PDF
        assert p1.confidence_score == 1.0
        assert "GLOBAL FINANCIAL ASSETS" in p1.raw_text
        assert "Consolidated Balance Sheet" in p1.raw_text

        # Verify structured table extraction
        assert len(p1.tables) >= 1
        table = p1.tables[0]
        assert "headers" in table
        assert "rows" in table
        assert table["row_count"] >= 2

        # Page 2 & 3 verification
        p2 = result.pages[1]
        assert "OPERATING REVENUES" in p2.raw_text
        assert p2.extraction_method == ExtractionMethod.DIGITAL_PDF

        p3 = result.pages[2]
        assert "AUDITOR'S REPORT" in p3.raw_text

        # Verify full merged text contains all pages
        assert "--- Page 1 [digital_pdf] ---" in result.full_merged_text
        assert "--- Page 2 [digital_pdf] ---" in result.full_merged_text
        assert "--- Page 3 [digital_pdf] ---" in result.full_merged_text

    def test_scenario_2_noisy_rotated_ocr_invoice(self, noisy_rotated_image_path: Path) -> None:
        """Scenario 2: Low-quality noisy/rotated image scan restored and extracted via OCR."""
        engine = DocumentIngestionEngine()
        result = engine.process_file(noisy_rotated_image_path)

        assert result.status in (DocumentStatus.SUCCESS, DocumentStatus.PARTIAL_SUCCESS)
        assert result.file_type in ("png", "jpg")
        assert result.total_pages_or_sheets == 1
        assert len(result.pages) == 1

        p1 = result.pages[0]
        assert p1.extraction_method == ExtractionMethod.OCR
        assert p1.confidence_score > 0.0

        # Text recognition verification (RapidOCR handles rotation + noise)
        text_upper = p1.raw_text.upper()
        assert "INVOICE" in text_upper or "98421" in text_upper or "DUE" in text_upper or "TRANSFER" in text_upper

    def test_scenario_3_multi_sheet_excel(self, multi_sheet_excel_path: Path) -> None:
        """Scenario 3: Multi-sheet Excel workbook with title banners and empty row stripping."""
        engine = DocumentIngestionEngine()
        result = engine.process_file(multi_sheet_excel_path)

        assert result.status == DocumentStatus.SUCCESS
        assert result.file_type == "xlsx"
        assert result.total_pages_or_sheets == 2
        assert len(result.sheets) == 2

        # Sheet 1: Equities Portfolio
        sheet1 = result.sheets[0]
        assert sheet1.sheet_name == "Equities Portfolio"
        assert sheet1.row_count == 4
        # Column headers should be auto-detected below banner
        assert "Ticker" in sheet1.column_headers
        assert "Market Value" in sheet1.column_headers
        assert len(sheet1.records) == 4
        assert sheet1.records[0]["Ticker"] == "AAPL"
        assert sheet1.records[3]["Ticker"] == "NVDA"
        # Verify markdown preview has headers
        assert "| Ticker |" in sheet1.markdown_preview
        # Verify summary stats
        assert "Market Value" in sheet1.summary_stats.get("numeric_columns", [])

        # Sheet 2: Fixed Income
        sheet2 = result.sheets[1]
        assert sheet2.sheet_name == "Fixed Income"
        assert sheet2.row_count == 3
        assert "Bond Description" in sheet2.column_headers
        assert "Yield to Maturity" in sheet2.column_headers

    def test_scenario_4_dirty_csv_encoding_and_delimiter(self, dirty_csv_path: Path) -> None:
        """Scenario 4: CSV with semicolon delimiter, Latin-1 encoding, and header banners."""
        engine = DocumentIngestionEngine()
        result = engine.process_file(dirty_csv_path)

        assert result.status == DocumentStatus.SUCCESS
        assert result.file_type == "csv"
        assert result.total_pages_or_sheets == 1
        assert len(result.sheets) == 1

        sheet = result.sheets[0]
        assert sheet.row_count == 4
        # Verify columns detected despite banner
        assert "Transaction ID" in sheet.column_headers
        assert "Vendor Name" in sheet.column_headers
        assert "Disbursement Amount" in sheet.column_headers

        # Verify unicode character handling from Latin-1
        vendors = [r["Vendor Name"] for r in sheet.records]
        assert any("Z\xfcrich" in v or "Reinsurance" in v for v in vendors)
        assert any("Montr\xe9al" in v or "Logistics" in v for v in vendors)
        assert any("S\xe3o Paulo" in v or "Consulting" in v for v in vendors)

    def test_scenario_5_corrupted_file_isolation_guard(
        self, corrupted_files: dict[str, Path]
    ) -> None:
        """Scenario 5: Robust isolation of corrupted, truncated, and unsupported files."""
        engine = DocumentIngestionEngine()

        # 1. Zero byte file
        res_zero = engine.process_file(corrupted_files["zero_bytes"])
        assert res_zero.status == DocumentStatus.FAILED_CORRUPTED
        assert "empty (0 bytes)" in str(res_zero.error_details).lower()

        # 2. Truncated PDF
        res_trunc = engine.process_file(corrupted_files["truncated_pdf"])
        assert res_trunc.status == DocumentStatus.FAILED_CORRUPTED
        assert res_trunc.error_details is not None

        # 3. Corrupt XLSX archive
        res_xlsx = engine.process_file(corrupted_files["corrupt_xlsx"])
        assert res_xlsx.status == DocumentStatus.FAILED_CORRUPTED
        assert "xlsx" in str(res_xlsx.error_details).lower()

        # 4. Broken image
        res_png = engine.process_file(corrupted_files["corrupt_png"])
        assert res_png.status == DocumentStatus.FAILED_CORRUPTED
        assert res_png.error_details is not None

        # 5. Unsupported executable
        res_unsup = engine.process_file(corrupted_files["unsupported"])
        assert res_unsup.status == DocumentStatus.UNSUPPORTED_FORMAT
        assert "unsupported" in str(res_unsup.error_details).lower()

    def test_hybrid_pdf_classification(self, hybrid_pdf_path: Path) -> None:
        """Verify hybrid routing: Page 1 native digital, Page 2 falls back to OCR."""
        engine = DocumentIngestionEngine()
        result = engine.process_file(hybrid_pdf_path)

        assert result.status in (DocumentStatus.SUCCESS, DocumentStatus.PARTIAL_SUCCESS)
        assert len(result.pages) == 2

        # Page 1: Digital
        p1 = result.pages[0]
        assert p1.page_number == 1
        assert p1.extraction_method == ExtractionMethod.DIGITAL_PDF
        assert "DIGITAL EXECUTIVE SUMMARY" in p1.raw_text
        assert p1.confidence_score == 1.0

        # Page 2: OCR Fallback
        p2 = result.pages[1]
        assert p2.page_number == 2
        assert p2.extraction_method == ExtractionMethod.OCR
        assert p2.confidence_score > 0.0
        assert "SCANNED" in p2.raw_text.upper() or "RECEIPT" in p2.raw_text.upper() or "EXPENSE" in p2.raw_text.upper()

    def test_parallel_batch_processing(
        self,
        digital_pdf_path: Path,
        multi_sheet_excel_path: Path,
        dirty_csv_path: Path,
        corrupted_files: dict[str, Path],
    ) -> None:
        """Verify process_batch with heterogeneous valid and corrupted documents."""
        engine = DocumentIngestionEngine()
        batch_paths = [
            digital_pdf_path,
            multi_sheet_excel_path,
            dirty_csv_path,
            corrupted_files["zero_bytes"],
            corrupted_files["truncated_pdf"],
        ]

        results = engine.process_batch(batch_paths, max_workers=3)

        assert len(results) == len(batch_paths)
        # Check order preservation
        assert results[0].file_name == digital_pdf_path.name
        assert results[0].status == DocumentStatus.SUCCESS

        assert results[1].file_name == multi_sheet_excel_path.name
        assert results[1].status == DocumentStatus.SUCCESS

        assert results[2].file_name == dirty_csv_path.name
        assert results[2].status == DocumentStatus.SUCCESS

        assert results[3].file_name == corrupted_files["zero_bytes"].name
        assert results[3].status == DocumentStatus.FAILED_CORRUPTED

        assert results[4].file_name == corrupted_files["truncated_pdf"].name
        assert results[4].status == DocumentStatus.FAILED_CORRUPTED

    def test_image_preprocessor_deskew_and_clahe(self) -> None:
        """Unit test direct image preprocessor operations."""
        preproc = ImagePreprocessor()

        # Test grayscale conversion
        test_rgb = np.ones((100, 100, 3), dtype=np.uint8) * 180
        gray = preproc.to_grayscale(test_rgb)
        assert len(gray.shape) == 2
        assert gray.shape == (100, 100)

        # Test CLAHE
        clahe_img = preproc.apply_clahe(gray)
        assert clahe_img.shape == (100, 100)

        # Test deskew
        slanted = np.ones((200, 400), dtype=np.uint8) * 255
        cv2.putText(slanted, "SAMPLE DOCUMENT LINE", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)
        center = (200, 100)
        M = cv2.getRotationMatrix2D(center, 5.0, 1.0)
        rotated = cv2.warpAffine(slanted, M, (400, 200), borderValue=255)

        deskewed, angle = preproc.deskew(rotated)
        assert deskewed.shape == rotated.shape
        # Angle should be detected close to -5 degrees
        assert abs(angle) > 2.0

    def test_1000_page_pdf_streaming(self, temp_dir: Path) -> None:
        """Verify memory-safe chunked streaming over a 1,000-page PDF document."""
        pdf_1000 = temp_dir / "high_volume_1000_pages.pdf"
        doc = pymupdf.open()
        for i in range(1000):
            p = doc.new_page()
            p.insert_text(
                pymupdf.Point(50, 50),
                f"Page {i+1}: Financial Settlement Ledger #{50000+i}. Liquid reserves verified and cleared.",
            )
        doc.save(str(pdf_1000))
        doc.close()

        config = IngestionConfig(pdf_chunk_size=50, max_parallel_workers=4)
        engine = DocumentIngestionEngine(config=config)
        result = engine.process_file(pdf_1000)

        assert result.status == DocumentStatus.SUCCESS
        assert result.total_pages_or_sheets == 1000
        assert len(result.pages) == 1000
        assert result.pages[0].page_number == 1
        assert result.pages[999].page_number == 1000
        assert result.pages[0].extraction_method == ExtractionMethod.DIGITAL_PDF
        assert "Page 1000" in result.pages[999].raw_text
        # High volume processing benchmark: should easily complete in under 15 seconds
        assert result.processing_time_ms < 15000.0
