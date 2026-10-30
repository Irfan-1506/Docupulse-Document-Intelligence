"""Comprehensive unit tests for Financial Data Validation & Cross-Verification Engine (Task 3)."""

from __future__ import annotations

from typing import List
import pytest

from src.extraction.schemas import InvoiceExtractionRecord, LineItem
from src.ingestion.schemas import SheetContent
from src.validation.engine import ValidationEngine
from src.validation.schemas import (
    BatchValidationReport,
    RecordValidationResult,
    ValidationConfig,
    ValidationRuleCode,
    ValidationSeverity,
)


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def create_valid_record(
    record_id: str = "rec-valid-01",
    invoice_number: str = "INV-2026-001",
    total: float = 4400.0,
    subtotal: Optional[float] = None,
    tax: Optional[float] = None,
    supplier_name: str = "Global Supplies Corp",
    customer_name: str = "Acme Logistics Inc",
    invoice_date: str = "2026-10-15",
    currency: str = "USD",
) -> InvoiceExtractionRecord:
    """Helper to build a mathematically sound, complete invoice extraction record."""
    if total is not None:
        if subtotal is None and tax is None:
            # Default consistent math: 90% subtotal, 10% tax
            tax_val = round(total * 0.1, 2)
            subtotal_val = round(total - tax_val, 2)
        elif subtotal is None:
            subtotal_val = round(total - (tax or 0.0), 2)
            tax_val = tax
        elif tax is None:
            tax_val = round(total - subtotal, 2)
            subtotal_val = subtotal
        else:
            subtotal_val = subtotal
            tax_val = tax

        # Create matching line items
        half_sub = round(subtotal_val / 2.0, 2)
        rem_sub = round(subtotal_val - half_sub, 2)
        line_items = [
            LineItem(
                description="High-Performance Server Unit",
                quantity=1.0,
                unit_price=half_sub,
                line_total=half_sub,
                confidence=0.95,
            ),
            LineItem(
                description="Optical Network Switch",
                quantity=1.0,
                unit_price=rem_sub,
                line_total=rem_sub,
                confidence=0.95,
            ),
        ]
    else:
        subtotal_val = subtotal
        tax_val = tax
        line_items = []

    return InvoiceExtractionRecord(
        record_id=record_id,
        source_document_id="doc-source-01",
        page_range_or_sheet="page 1",
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        supplier_name=supplier_name,
        customer_name=customer_name,
        subtotal=subtotal_val,
        tax=tax_val,
        total=total,
        currency=currency,
        line_items=line_items,
        field_confidences={
            "invoice_number": 0.95,
            "invoice_date": 0.95,
            "supplier_name": 0.90,
            "customer_name": 0.90,
            "subtotal": 0.95,
            "tax": 0.95,
            "total": 0.95,
            "currency": 0.95,
        },
        raw_evidence_snippets={
            "invoice_number": f"Invoice No: {invoice_number}" if invoice_number else "",
            "total": f"Total Due: ${total:,.2f}" if total is not None else "",
        },
        uncertain_fields=[],
        missing_required_fields=[],
        extraction_warnings=[],
    )


# ---------------------------------------------------------------------------
# Test Scenarios
# ---------------------------------------------------------------------------

class TestValidationEngine:
    """Test suite executing the 7 required scenarios of Task 3."""

    def test_scenario_1_clean_auto_verified_record(self) -> None:
        """Scenario 1: Clean, mathematically sound invoice -> AUTO_VERIFIED (requires_manual_review=False)."""
        record = create_valid_record(total=4400.0, subtotal=4000.0, tax=400.0)
        engine = ValidationEngine()

        result = engine.validate_record(record)

        assert result.is_valid is True
        assert result.validation_status == "AUTO_VERIFIED"
        assert result.requires_manual_review is False
        assert len(result.issues) == 0
        assert result.computed_metrics["computed_line_sum"] == 4000.0
        assert result.computed_metrics["computed_total"] == 4400.0
        assert result.computed_metrics["total_variance"] == 0.0

    def test_scenario_2_missing_required_fields(self) -> None:
        """Scenario 2: Missing mandatory fields (invoice_number & total is None) -> MISSING_REQUIRED_FIELD."""
        record = create_valid_record(invoice_number="", total=None)
        # Explicitly set total to None
        record.total = None
        record.invoice_number = None

        engine = ValidationEngine()
        result = engine.validate_record(record)

        assert result.is_valid is False
        assert result.validation_status == "NEEDS_MANUAL_REVIEW"
        assert result.requires_manual_review is True

        missing_fields = [
            i.field_name for i in result.issues
            if i.rule_code == ValidationRuleCode.MISSING_REQUIRED_FIELD and i.severity == ValidationSeverity.ERROR
        ]
        assert "invoice_number" in missing_fields
        assert "total" in missing_fields

    def test_scenario_3_negative_and_invalid_numeric_values(self) -> None:
        """Scenario 3: Negative total, negative unit price, and invalid quantity -> INVALID_NUMERIC_VALUE."""
        record = create_valid_record(total=-500.0)
        # Add invalid line items
        record.line_items = [
            LineItem(
                description="Faulty Widget",
                quantity=-5.0,  # Invalid negative quantity
                unit_price=100.0,
                line_total=500.0,
            ),
            LineItem(
                description="Consulting Labor",
                quantity=2.0,
                unit_price=-250.0,  # Negative unit price without discount label
                line_total=500.0,
            ),
        ]

        engine = ValidationEngine()
        result = engine.validate_record(record)

        assert result.is_valid is False
        assert result.requires_manual_review is True

        invalid_rules = [
            i for i in result.issues
            if i.rule_code == ValidationRuleCode.INVALID_NUMERIC_VALUE
        ]
        assert len(invalid_rules) >= 3  # Negative total, negative quantity, negative unit price
        assert any(i.field_name == "total" for i in invalid_rules)

    def test_scenario_4_mathematical_inconsistency_and_line_item_math(self) -> None:
        """Scenario 4: Discrepancies between line items, subtotal, and total -> exact Decimal variances."""
        # Setup: Line items sum to $4,200, but subtotal is $4,000.
        # Subtotal ($4,000) + Tax ($420) = $4,420, but extracted Total is $4,520 (variance -$100).
        record = create_valid_record(
            subtotal=4000.0,
            tax=420.0,
            total=4520.0,
        )
        # Alter line items so item 1 has internal math error: 10 * $210 = $2,100, but stated line total is $2,200
        record.line_items = [
            LineItem(
                description="Item A",
                quantity=10.0,
                unit_price=210.0,
                line_total=2200.0,  # Math error: 10 * 210 = 2100 != 2200
            ),
            LineItem(
                description="Item B",
                quantity=10.0,
                unit_price=200.0,
                line_total=2000.0,
            ),
        ]  # Sum = 2200 + 2000 = 4200 != subtotal 4000

        engine = ValidationEngine()
        result = engine.validate_record(record)

        assert result.is_valid is False
        assert result.requires_manual_review is True

        # Check line item internal math error
        line_math_issues = [i for i in result.issues if i.rule_code == ValidationRuleCode.LINE_ITEM_MATH_ERROR]
        assert len(line_math_issues) == 1
        assert line_math_issues[0].variance == 100.0  # 2200 - 2100

        # Check line items sum vs subtotal error
        line_sum_issues = [i for i in result.issues if i.rule_code == ValidationRuleCode.LINE_ITEMS_SUM_MISMATCH]
        assert len(line_sum_issues) == 1
        assert line_sum_issues[0].variance == 200.0  # 4200 - 4000

        # Check subtotal + tax vs total error
        total_mismatch_issues = [i for i in result.issues if i.rule_code == ValidationRuleCode.SUBTOTAL_TAX_TOTAL_MISMATCH]
        assert len(total_mismatch_issues) == 1
        assert total_mismatch_issues[0].variance == -100.0  # 4420 - 4520

    def test_scenario_5_exact_and_fuzzy_duplicate_detection(self) -> None:
        """Scenario 5: Exact duplicate and OCR-fuzzy duplicate (INV-1001 vs INV-1OO1) detection."""
        # 1. Original Record
        rec_a = create_valid_record(
            record_id="rec-orig",
            invoice_number="INV-1001",
            supplier_name="Global Logistics Corp",
            total=5000.0,
            invoice_date="2026-09-01",
        )

        # 2. Exact Duplicate: Same invoice #, same vendor, same total
        rec_b = create_valid_record(
            record_id="rec-dup-exact",
            invoice_number="INV-1001",
            supplier_name="Global Logistics",
            total=5000.0,
            invoice_date="2026-09-01",
        )

        # 3. Fuzzy Duplicate (OCR Typo: letter O instead of digit 0)
        rec_c = create_valid_record(
            record_id="rec-dup-fuzzy",
            invoice_number="INV-1OO1",  # Letter O instead of zero
            supplier_name="Global Logistics Corp",
            total=5000.0,
            invoice_date="2026-09-01",
        )

        # 4. Independent Different Record
        rec_d = create_valid_record(
            record_id="rec-distinct",
            invoice_number="INV-9999",
            supplier_name="Different Vendor LLC",
            total=1200.0,
            invoice_date="2026-09-15",
        )

        engine = ValidationEngine()
        report = engine.validate_batch([rec_a, rec_b, rec_c, rec_d])

        assert report.total_records_evaluated == 4
        assert report.duplicate_count == 2

        # Record A: Original -> AUTO_VERIFIED
        assert report.results[0].validation_status == "AUTO_VERIFIED"

        # Record B: Exact Duplicate -> REJECTED_DUPLICATE
        assert report.results[1].validation_status == "REJECTED_DUPLICATE"
        assert report.results[1].requires_manual_review is True
        assert any(i.rule_code == ValidationRuleCode.EXACT_DUPLICATE_INVOICE for i in report.results[1].issues)

        # Record C: Fuzzy Duplicate -> REJECTED_DUPLICATE
        assert report.results[2].validation_status == "REJECTED_DUPLICATE"
        assert report.results[2].requires_manual_review is True
        assert any(i.rule_code == ValidationRuleCode.FUZZY_DUPLICATE_INVOICE for i in report.results[2].issues)

        # Record D: Distinct -> AUTO_VERIFIED
        assert report.results[3].validation_status == "AUTO_VERIFIED"

    def test_scenario_6_pdf_to_excel_cross_verification(self) -> None:
        """Scenario 6: PDF-to-Excel cross-verification (1 exact match, 1 with $50 variance, 1 orphan)."""
        # Master Excel Ledger data
        ledger_sheet = SheetContent(
            sheet_name="Master_Ledger",
            row_count=2,
            column_headers=["Invoice #", "Vendor", "Total", "Currency", "Date"],
            records=[
                {
                    "Invoice #": "INV-201",
                    "Vendor": "Acme Supplies",
                    "Total": 1500.0,
                    "Currency": "USD",
                    "Date": "2026-08-01",
                },
                {
                    "Invoice #": "INV-202",
                    "Vendor": "Beta Services",
                    "Total": 2000.0,
                    "Currency": "USD",
                    "Date": "2026-08-02",
                },
            ],
            markdown_preview="| Invoice # | Vendor | Total | Currency | Date |\n| INV-201 | Acme | 1500 | USD | 2026-08-01 |\n| INV-202 | Beta | 2000 | USD | 2026-08-02 |",
        )

        # PDF Record 1: Exact Match
        pdf_1 = create_valid_record(
            record_id="pdf-inv-201",
            invoice_number="INV-201",
            supplier_name="Acme Supplies",
            total=1500.0,
            subtotal=1500.0,
            tax=0.0,
        )

        # PDF Record 2: Amount Discrepancy ($2,050 vs $2,000 in ledger -> $50 variance)
        pdf_2 = create_valid_record(
            record_id="pdf-inv-202",
            invoice_number="INV-202",
            supplier_name="Beta Services",
            total=2050.0,  # $50 higher than ledger!
            subtotal=2050.0,
            tax=0.0,
        )

        # PDF Record 3: Orphan (INV-203 does not exist in master ledger)
        pdf_3 = create_valid_record(
            record_id="pdf-inv-203",
            invoice_number="INV-203",
            supplier_name="Gamma Consulting",
            total=3000.0,
            subtotal=3000.0,
            tax=0.0,
        )

        engine = ValidationEngine()
        report = engine.validate_batch(
            records=[pdf_1, pdf_2, pdf_3],
            reference_ledger=ledger_sheet,
        )

        assert report.total_records_evaluated == 3
        assert report.cross_verified_exact_matches == 1
        assert report.cross_verified_variances == 1

        # PDF 1: Exact Match
        res1 = report.results[0]
        assert res1.cross_verification is not None
        assert res1.cross_verification.match_status == "EXACT_MATCH"
        assert res1.cross_verification.field_deltas["total"]["delta"] == 0.0

        # PDF 2: Variance Detected ($50 variance)
        res2 = report.results[1]
        assert res2.cross_verification is not None
        assert res2.cross_verification.match_status == "VARIANCE_DETECTED"
        assert res2.cross_verification.field_deltas["total"]["delta"] == 50.0
        assert res2.requires_manual_review is True
        assert any(i.rule_code == ValidationRuleCode.CROSS_VERIFY_AMOUNT_VARIANCE for i in res2.issues)

        # PDF 3: Orphan Missing in Ledger
        res3 = report.results[2]
        assert res3.cross_verification is not None
        assert res3.cross_verification.match_status == "UNMATCHED_ORPHAN"
        assert res3.requires_manual_review is True
        assert any(i.rule_code == ValidationRuleCode.CROSS_VERIFY_MISSING_IN_LEDGER for i in res3.issues)

    def test_scenario_7_pdf_to_pdf_cross_verification_po_variance(self) -> None:
        """Scenario 7: PDF-to-PDF cross-verification comparing Invoice vs Purchase Order with price variance."""
        # Purchase Order PDF record
        po_record = create_valid_record(
            record_id="po-9901",
            invoice_number="PO-9901",
            supplier_name="Nexus Cloud Systems LLC",
            total=1000.0,
            subtotal=1000.0,
            tax=0.0,
        )
        po_record.line_items = [
            LineItem(
                description="Cloud Compute VM Tier 2",
                quantity=5.0,
                unit_price=200.0,  # Approved PO unit price: $200.00
                line_total=1000.0,
            )
        ]

        # Vendor Invoice PDF record: billed at $250.00 per unit instead of $200.00!
        inv_record = create_valid_record(
            record_id="inv-9901",
            invoice_number="INV-9901",
            supplier_name="Nexus Cloud Systems LLC",
            total=1250.0,
            subtotal=1250.0,
            tax=0.0,
        )
        inv_record.line_items = [
            LineItem(
                description="Cloud Compute VM Tier 2",
                quantity=5.0,
                unit_price=250.0,  # Billed unit price: $250.00 (Delta: +$50/unit)
                line_total=1250.0,
            )
        ]

        engine = ValidationEngine()
        result = engine.validate_record(
            record=inv_record,
            reference_po=po_record,
        )

        assert result.cross_verification is not None
        assert result.cross_verification.verification_type == "PDF_TO_PDF"
        assert result.cross_verification.match_status == "VARIANCE_DETECTED"
        assert result.requires_manual_review is True

        deltas = result.cross_verification.field_deltas
        assert deltas["total"]["delta"] == 250.0
        assert deltas["line_items[0].unit_price"]["delta"] == 50.0
        assert any(i.rule_code == ValidationRuleCode.CROSS_VERIFY_AMOUNT_VARIANCE for i in result.issues)
