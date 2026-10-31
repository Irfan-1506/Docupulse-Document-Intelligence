"""Cross-verification engine for PDF-to-Excel and PDF-to-PDF reconciliation."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from rapidfuzz import fuzz
except ImportError:
    import difflib

    class _FuzzFallback:
        @staticmethod
        def token_sort_ratio(s1: str, s2: str) -> float:
            return difflib.SequenceMatcher(None, s1, s2).ratio() * 100.0

    fuzz = _FuzzFallback()

from src.extraction.schemas import InvoiceExtractionRecord, LineItem
from src.ingestion.schemas import SheetContent
from src.validation.deduplicator import normalize_entity_name, normalize_invoice_number
from src.validation.rules import to_decimal
from src.validation.schemas import (
    CrossVerificationMatch,
    ValidationConfig,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)


class CrossVerificationEngine:
    """Reconciles extracted PDF records against spreadsheet ledgers and secondary reference documents."""

    def __init__(self, config: Optional[ValidationConfig] = None) -> None:
        self.config = config or ValidationConfig()
        self.tolerance = Decimal(str(self.config.rounding_tolerance))

    # -----------------------------------------------------------------------
    # PDF-to-Excel / CSV Reconciliation
    # -----------------------------------------------------------------------

    def reconcile_with_spreadsheet(
        self,
        pdf_record: InvoiceExtractionRecord,
        ledger: Union[SheetContent, List[Dict[str, Any]], List[InvoiceExtractionRecord]],
    ) -> Tuple[CrossVerificationMatch, Optional[ValidationIssue]]:
        """Reconcile a single PDF invoice against a spreadsheet ledger (SheetContent or list of dicts).

        Returns:
            Tuple of (CrossVerificationMatch, Optional[ValidationIssue]).
        """
        # Normalize ledger into list of standardized dicts
        ledger_rows = self._normalize_ledger_data(ledger)

        pdf_inv_norm = normalize_invoice_number(pdf_record.invoice_number)
        pdf_vendor_norm = normalize_entity_name(pdf_record.supplier_name)
        pdf_total = to_decimal(pdf_record.total)

        matched_row: Optional[Dict[str, Any]] = None
        matched_keys: List[str] = []
        best_match_id = "unknown"

        # Pass 1: Match by Invoice Number
        if pdf_inv_norm:
            for idx, row in enumerate(ledger_rows):
                row_inv = normalize_invoice_number(str(row.get("invoice_number", "")))
                if row_inv and row_inv == pdf_inv_norm:
                    matched_row = row
                    matched_keys.append("invoice_number")
                    best_match_id = str(row.get("row_id", f"Row_{idx+1}"))
                    break

        # Pass 2: Fallback composite match by (vendor, total)
        if not matched_row and pdf_total is not None and pdf_vendor_norm:
            for idx, row in enumerate(ledger_rows):
                row_total = to_decimal(row.get("total"))
                row_vendor = normalize_entity_name(str(row.get("supplier_name", "")))

                if row_total is not None and abs(row_total - pdf_total) <= self.tolerance:
                    if row_vendor and fuzz.token_sort_ratio(pdf_vendor_norm, row_vendor) >= (self.config.fuzzy_supplier_threshold * 100):
                        matched_row = row
                        matched_keys.extend(["supplier_name", "total"])
                        best_match_id = str(row.get("row_id", f"Row_{idx+1}"))
                        break

        # If completely unmatched -> UNMATCHED_ORPHAN
        if not matched_row:
            match = CrossVerificationMatch(
                source_record_id=pdf_record.record_id,
                target_reference_id="None",
                verification_type="PDF_TO_EXCEL",
                match_status="UNMATCHED_ORPHAN",
                matched_on_keys=[],
                field_deltas={},
                confidence_score=0.0,
            )
            issue = ValidationIssue(
                rule_code=ValidationRuleCode.CROSS_VERIFY_MISSING_IN_LEDGER,
                severity=ValidationSeverity.ERROR,
                field_name="invoice_number",
                message=(
                    f"Orphan invoice: Invoice '{pdf_record.invoice_number}' (Supplier: '{pdf_record.supplier_name}', "
                    f"Total: ${float(pdf_total or 0):,.2f}) not found in master spreadsheet ledger."
                ),
                expected_value="Entry in ledger",
                actual_value="Missing",
            )
            return match, issue

        # Compare fields and compute deltas
        field_deltas: Dict[str, Dict[str, Any]] = {}
        has_variance = False
        variance_details: List[str] = []

        # 1. Total comparison
        row_total_dec = to_decimal(matched_row.get("total"))
        if pdf_total is not None and row_total_dec is not None:
            delta = float(pdf_total - row_total_dec)
            if abs(pdf_total - row_total_dec) > self.tolerance:
                has_variance = True
                variance_details.append(f"Total: PDF=${float(pdf_total):,.2f} vs Ledger=${float(row_total_dec):,.2f} (Delta: ${delta:+,.2f})")
                field_deltas["total"] = {
                    "source": float(pdf_total),
                    "target": float(row_total_dec),
                    "delta": round(delta, 2),
                }
            else:
                field_deltas["total"] = {
                    "source": float(pdf_total),
                    "target": float(row_total_dec),
                    "delta": 0.0,
                }

        # 2. Subtotal comparison (if both present)
        pdf_sub = to_decimal(pdf_record.subtotal)
        row_sub = to_decimal(matched_row.get("subtotal"))
        if pdf_sub is not None and row_sub is not None:
            delta = float(pdf_sub - row_sub)
            if abs(pdf_sub - row_sub) > self.tolerance:
                has_variance = True
                variance_details.append(f"Subtotal: PDF=${float(pdf_sub):,.2f} vs Ledger=${float(row_sub):,.2f}")
                field_deltas["subtotal"] = {
                    "source": float(pdf_sub),
                    "target": float(row_sub),
                    "delta": round(delta, 2),
                }
            else:
                field_deltas["subtotal"] = {
                    "source": float(pdf_sub),
                    "target": float(row_sub),
                    "delta": 0.0,
                }

        # 3. Tax comparison
        pdf_tax = to_decimal(pdf_record.tax)
        row_tax = to_decimal(matched_row.get("tax"))
        if pdf_tax is not None and row_tax is not None:
            delta = float(pdf_tax - row_tax)
            field_deltas["tax"] = {
                "source": float(pdf_tax),
                "target": float(row_tax),
                "delta": round(delta, 2) if abs(delta) > self.tolerance else 0.0,
            }

        # 4. Supplier comparison
        row_supplier = str(matched_row.get("supplier_name", "")).strip()
        if pdf_record.supplier_name and row_supplier:
            supp_match = fuzz.token_sort_ratio(pdf_vendor_norm, normalize_entity_name(row_supplier)) >= (self.config.fuzzy_supplier_threshold * 100)
            field_deltas["supplier_name"] = {
                "source": pdf_record.supplier_name,
                "target": row_supplier,
                "delta": 0.0 if supp_match else "MISMATCH",
            }
            if not supp_match:
                has_variance = True
                variance_details.append(f"Supplier mismatch: PDF='{pdf_record.supplier_name}' vs Ledger='{row_supplier}'")

        # 5. Invoice Number comparison
        row_inv = str(matched_row.get("invoice_number", "")).strip()
        if pdf_record.invoice_number and row_inv:
            inv_match = normalize_invoice_number(pdf_record.invoice_number) == normalize_invoice_number(row_inv)
            field_deltas["invoice_number"] = {
                "source": pdf_record.invoice_number,
                "target": row_inv,
                "delta": 0.0 if inv_match else "MISMATCH",
            }

        # 6. Invoice Date comparison
        row_date = str(matched_row.get("invoice_date", "")).strip()
        if pdf_record.invoice_date and row_date:
            date_match = str(pdf_record.invoice_date).split("T")[0] == row_date.split("T")[0]
            field_deltas["invoice_date"] = {
                "source": str(pdf_record.invoice_date),
                "target": row_date,
                "delta": 0.0 if date_match else "MISMATCH",
            }

        # 7. Currency comparison
        row_curr = str(matched_row.get("currency", "")).strip().upper()
        if pdf_record.currency and row_curr:
            curr_match = pdf_record.currency.upper() == row_curr
            field_deltas["currency"] = {
                "source": pdf_record.currency,
                "target": row_curr,
                "delta": 0.0 if curr_match else "MISMATCH",
            }
            if not curr_match:
                has_variance = True
                variance_details.append(f"Currency mismatch: PDF={pdf_record.currency} vs Ledger={row_curr}")

        if has_variance:
            match = CrossVerificationMatch(
                source_record_id=pdf_record.record_id,
                target_reference_id=best_match_id,
                verification_type="PDF_TO_EXCEL",
                match_status="VARIANCE_DETECTED",
                matched_on_keys=matched_keys,
                field_deltas=field_deltas,
                confidence_score=0.90,
            )
            issue = ValidationIssue(
                rule_code=ValidationRuleCode.CROSS_VERIFY_AMOUNT_VARIANCE,
                severity=ValidationSeverity.ERROR,
                field_name="total",
                message=f"Cross-verification variance detected against ledger {best_match_id}: {'; '.join(variance_details)}.",
                expected_value=float(row_total_dec) if row_total_dec is not None else None,
                actual_value=float(pdf_total) if pdf_total is not None else None,
                variance=field_deltas.get("total", {}).get("delta"),
            )
            return match, issue

        # Clean exact match
        match = CrossVerificationMatch(
            source_record_id=pdf_record.record_id,
            target_reference_id=best_match_id,
            verification_type="PDF_TO_EXCEL",
            match_status="EXACT_MATCH",
            matched_on_keys=matched_keys,
            field_deltas=field_deltas,
            confidence_score=1.0,
        )
        return match, None

    # -----------------------------------------------------------------------
    # PDF-to-PDF Reconciliation (e.g. Invoice vs Purchase Order)
    # -----------------------------------------------------------------------

    def reconcile_pdf_to_pdf(
        self,
        invoice_record: InvoiceExtractionRecord,
        po_record: InvoiceExtractionRecord,
    ) -> Tuple[CrossVerificationMatch, Optional[ValidationIssue]]:
        """Cross-verify two PDF extraction records (e.g. Vendor Invoice vs Purchase Order).

        Compares totals, subtotals, vendor entities, and line-item unit prices/quantities.
        """
        field_deltas: Dict[str, Dict[str, Any]] = {}
        matched_keys: List[str] = []
        has_variance = False
        variance_details: List[str] = []

        # Check vendor
        inv_vendor = normalize_entity_name(invoice_record.supplier_name)
        po_vendor = normalize_entity_name(po_record.supplier_name)
        if inv_vendor and po_vendor:
            matched_keys.append("supplier_name")
            if fuzz.token_sort_ratio(inv_vendor, po_vendor) < (self.config.fuzzy_supplier_threshold * 100):
                has_variance = True
                variance_details.append(f"Supplier mismatch: '{invoice_record.supplier_name}' vs '{po_record.supplier_name}'")

        # Compare Totals
        inv_total = to_decimal(invoice_record.total)
        po_total = to_decimal(po_record.total)
        if inv_total is not None and po_total is not None:
            delta = float(inv_total - po_total)
            if abs(inv_total - po_total) > self.tolerance:
                has_variance = True
                variance_details.append(f"Total variance: Invoice=${float(inv_total):,.2f} vs PO=${float(po_total):,.2f} (Delta: ${delta:+,.2f})")
                field_deltas["total"] = {
                    "source": float(inv_total),
                    "target": float(po_total),
                    "delta": round(delta, 2),
                }
            else:
                matched_keys.append("total")
                field_deltas["total"] = {
                    "source": float(inv_total),
                    "target": float(po_total),
                    "delta": 0.0,
                }

        # Compare Line Items (SKU / Description match)
        if invoice_record.line_items and po_record.line_items:
            for idx, inv_item in enumerate(invoice_record.line_items):
                inv_desc = (inv_item.description or "").lower()
                # Find best matching item in PO
                best_po_item: Optional[LineItem] = None
                best_sim = 0.0

                for po_item in po_record.line_items:
                    po_desc = (po_item.description or "").lower()
                    if inv_item.sku_or_code and po_item.sku_or_code and inv_item.sku_or_code == po_item.sku_or_code:
                        best_po_item = po_item
                        break
                    sim = fuzz.token_sort_ratio(inv_desc, po_desc) / 100.0
                    if sim > best_sim and sim >= 0.65:
                        best_sim = sim
                        best_po_item = po_item

                if best_po_item:
                    # Compare unit prices
                    inv_price = to_decimal(inv_item.unit_price)
                    po_price = to_decimal(best_po_item.unit_price)

                    if inv_price is not None and po_price is not None:
                        if abs(inv_price - po_price) > self.tolerance:
                            has_variance = True
                            p_delta = float(inv_price - po_price)
                            variance_details.append(
                                f"Item '{inv_item.description}' Unit Price mismatch: "
                                f"Invoice=${float(inv_price):,.2f} vs PO=${float(po_price):,.2f}"
                            )
                            field_deltas[f"line_items[{idx}].unit_price"] = {
                                "source": float(inv_price),
                                "target": float(po_price),
                                "delta": round(p_delta, 2),
                            }

                    # Compare quantities
                    if inv_item.quantity is not None and best_po_item.quantity is not None:
                        if abs(inv_item.quantity - best_po_item.quantity) > 0.001:
                            has_variance = True
                            q_delta = float(inv_item.quantity - best_po_item.quantity)
                            variance_details.append(
                                f"Item '{inv_item.description}' Quantity mismatch: "
                                f"Invoice={inv_item.quantity} vs PO={best_po_item.quantity}"
                            )
                            field_deltas[f"line_items[{idx}].quantity"] = {
                                "source": inv_item.quantity,
                                "target": best_po_item.quantity,
                                "delta": round(q_delta, 2),
                            }

        if has_variance:
            match = CrossVerificationMatch(
                source_record_id=invoice_record.record_id,
                target_reference_id=po_record.record_id,
                verification_type="PDF_TO_PDF",
                match_status="VARIANCE_DETECTED",
                matched_on_keys=matched_keys,
                field_deltas=field_deltas,
                confidence_score=0.85,
            )
            issue = ValidationIssue(
                rule_code=ValidationRuleCode.CROSS_VERIFY_AMOUNT_VARIANCE,
                severity=ValidationSeverity.ERROR,
                field_name="total",
                message=f"PDF-to-PDF cross-verification variance detected against PO '{po_record.record_id}': {'; '.join(variance_details)}.",
                expected_value=float(po_total) if po_total is not None else None,
                actual_value=float(inv_total) if inv_total is not None else None,
                variance=field_deltas.get("total", {}).get("delta"),
            )
            return match, issue

        match = CrossVerificationMatch(
            source_record_id=invoice_record.record_id,
            target_reference_id=po_record.record_id,
            verification_type="PDF_TO_PDF",
            match_status="EXACT_MATCH",
            matched_on_keys=matched_keys,
            field_deltas=field_deltas,
            confidence_score=1.0,
        )
        return match, None

    # -----------------------------------------------------------------------
    # Helper Normalizer
    # -----------------------------------------------------------------------

    def _normalize_ledger_data(
        self,
        ledger: Union[SheetContent, List[Dict[str, Any]], List[InvoiceExtractionRecord]],
    ) -> List[Dict[str, Any]]:
        """Standardize various spreadsheet input formats into canonical row dicts."""
        standardized_rows: List[Dict[str, Any]] = []

        if isinstance(ledger, SheetContent):
            from src.extraction.chunker import SpreadsheetInvoiceMapper
            role_map = SpreadsheetInvoiceMapper.match_column_roles(ledger.column_headers)

            for idx, r in enumerate(ledger.records):
                row_dict: Dict[str, Any] = {"row_id": f"Row_{idx+1}"}
                for k, v in r.items():
                    role = role_map.get(k)
                    if role:
                        row_dict[role] = v
                standardized_rows.append(row_dict)

        elif isinstance(ledger, list) and ledger and isinstance(ledger[0], InvoiceExtractionRecord):
            for rec in ledger:
                standardized_rows.append({
                    "row_id": rec.record_id,
                    "invoice_number": rec.invoice_number,
                    "supplier_name": rec.supplier_name,
                    "total": rec.total,
                    "subtotal": rec.subtotal,
                    "tax": rec.tax,
                    "currency": rec.currency,
                    "invoice_date": rec.invoice_date,
                })

        elif isinstance(ledger, list):
            for idx, r in enumerate(ledger):
                if isinstance(r, dict):
                    row_dict = dict(r)
                    if "row_id" not in row_dict:
                        row_dict["row_id"] = f"Row_{idx+1}"
                    standardized_rows.append(row_dict)

        return standardized_rows
