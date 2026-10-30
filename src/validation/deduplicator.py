"""Exact and fuzzy duplicate invoice detector with OCR-typo resilience."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

try:
    from rapidfuzz import fuzz
except ImportError:
    import difflib

    class _FuzzFallback:
        @staticmethod
        def token_sort_ratio(s1: str, s2: str) -> float:
            return difflib.SequenceMatcher(None, s1, s2).ratio() * 100.0

        @staticmethod
        def ratio(s1: str, s2: str) -> float:
            return difflib.SequenceMatcher(None, s1, s2).ratio() * 100.0

    fuzz = _FuzzFallback()

from src.extraction.schemas import InvoiceExtractionRecord
from src.validation.rules import to_decimal
from src.validation.schemas import (
    ValidationConfig,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)


def normalize_entity_name(name: Optional[str]) -> str:
    """Normalize vendor/supplier strings by stripping legal suffixes and punctuation."""
    if not name:
        return ""
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    suffixes = {"inc", "corp", "corporation", "llc", "ltd", "gmbh", "co", "the", "group", "holdings"}
    tokens = [t for t in clean.split() if t not in suffixes]
    return " ".join(tokens)


def normalize_invoice_number(inv: Optional[str]) -> str:
    """Strip spaces, dashes, and slashes for canonical alphanumeric comparison."""
    if not inv:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", inv.upper())


def ocr_soft_normalize_invoice_number(inv: Optional[str]) -> str:
    """Normalize invoice number by mapping common OCR character misidentifications (O->0, I/L->1, S->5)."""
    clean = normalize_invoice_number(inv)
    trans = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"})
    return clean.translate(trans)


class InvoiceDeduplicator:
    """Detects both exact and fuzzy/near duplicates across batch records and historical registries."""

    def __init__(
        self,
        config: Optional[ValidationConfig] = None,
        historical_records: Optional[List[InvoiceExtractionRecord]] = None,
    ) -> None:
        self.config = config or ValidationConfig()
        self.registry: List[InvoiceExtractionRecord] = []
        if historical_records:
            self.registry.extend(historical_records)

    def add_to_registry(self, record: InvoiceExtractionRecord) -> None:
        """Add record to deduplication registry."""
        self.registry.append(record)

    def check_duplicate(
        self,
        record: InvoiceExtractionRecord,
        candidates: Optional[List[InvoiceExtractionRecord]] = None,
    ) -> Optional[ValidationIssue]:
        """Check if an invoice record duplicates any candidate in the current batch or registry.

        Returns:
            ValidationIssue if duplicate detected, or None.
        """
        if not record.invoice_number and record.total is None:
            return None

        pool = candidates if candidates is not None else self.registry
        rec_inv_exact = normalize_invoice_number(record.invoice_number)
        rec_inv_ocr = ocr_soft_normalize_invoice_number(record.invoice_number)
        rec_vendor_norm = normalize_entity_name(record.supplier_name)
        rec_total = to_decimal(record.total)

        for other in pool:
            # Skip comparing record against itself
            if other.record_id == record.record_id:
                continue

            other_inv_exact = normalize_invoice_number(other.invoice_number)
            other_inv_ocr = ocr_soft_normalize_invoice_number(other.invoice_number)
            other_vendor_norm = normalize_entity_name(other.supplier_name)
            other_total = to_decimal(other.total)

            vendor_sim = 0.0
            if rec_vendor_norm and other_vendor_norm:
                vendor_sim = fuzz.token_sort_ratio(rec_vendor_norm, other_vendor_norm) / 100.0

            totals_match = (
                rec_total is not None
                and other_total is not None
                and abs(rec_total - other_total) <= Decimal(str(self.config.rounding_tolerance))
            )

            # --- Check 1: EXACT DUPLICATE ---
            # Rule: Same exact invoice number AND same vendor (or same exact invoice number + total + date)
            is_exact_dup = False
            if rec_inv_exact and other_inv_exact and rec_inv_exact == other_inv_exact:
                if vendor_sim >= self.config.fuzzy_supplier_threshold or (rec_vendor_norm == other_vendor_norm and rec_vendor_norm):
                    is_exact_dup = True
                elif totals_match and record.invoice_date and other.invoice_date and record.invoice_date == other.invoice_date:
                    is_exact_dup = True

            if is_exact_dup:
                return ValidationIssue(
                    rule_code=ValidationRuleCode.EXACT_DUPLICATE_INVOICE,
                    severity=ValidationSeverity.ERROR,
                    field_name="invoice_number",
                    message=(
                        f"Exact duplicate invoice detected: matches record '{other.record_id}' "
                        f"(Invoice #{other.invoice_number}, Supplier: '{other.supplier_name}', "
                        f"Total: ${float(other_total):,.2f} on {other.page_range_or_sheet})."
                    ),
                    expected_value="Unique invoice",
                    actual_value=record.invoice_number,
                )

            # --- Check 2: FUZZY / NEAR DUPLICATE (OCR Typos) ---
            # Rule: OCR-normalized invoice number matches (e.g. INV-1001 vs INV-1OO1),
            # vendor similarity >= threshold, total matches, and dates match.
            is_fuzzy_dup = False
            if rec_inv_ocr and other_inv_ocr and rec_inv_ocr == other_inv_ocr:
                if (vendor_sim >= self.config.fuzzy_supplier_threshold or not rec_vendor_norm or not other_vendor_norm) and totals_match:
                    is_fuzzy_dup = True

            # Also check Levenshtein distance on invoice numbers when vendor and total match exactly
            if not is_fuzzy_dup and rec_inv_exact and other_inv_exact and totals_match and vendor_sim >= self.config.fuzzy_supplier_threshold:
                inv_sim = fuzz.ratio(rec_inv_exact, other_inv_exact) / 100.0
                if inv_sim >= 0.80:
                    is_fuzzy_dup = True

            if is_fuzzy_dup:
                return ValidationIssue(
                    rule_code=ValidationRuleCode.FUZZY_DUPLICATE_INVOICE,
                    severity=ValidationSeverity.ERROR,
                    field_name="invoice_number",
                    message=(
                        f"Fuzzy duplicate invoice detected (potential OCR typo): '{record.invoice_number}' "
                        f"closely matches record '{other.record_id}' ('{other.invoice_number}') with identical "
                        f"supplier '{other.supplier_name}' and total ${float(other_total or 0):,.2f}."
                    ),
                    expected_value="Unique invoice",
                    actual_value=record.invoice_number,
                )

        return None

    def evaluate_batch(
        self,
        records: List[InvoiceExtractionRecord],
    ) -> Dict[str, Optional[ValidationIssue]]:
        """Evaluate a full batch of records for intra-batch and registry duplicates."""
        results: Dict[str, Optional[ValidationIssue]] = {}
        seen_so_far: List[InvoiceExtractionRecord] = list(self.registry)

        for rec in records:
            dup_issue = self.check_duplicate(rec, candidates=seen_so_far)
            results[rec.record_id] = dup_issue
            # Append to seen_so_far so subsequent records in the batch are checked against it
            seen_so_far.append(rec)

        return results
