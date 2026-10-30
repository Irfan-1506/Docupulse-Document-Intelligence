"""Deterministic financial validation rules with exact Decimal arithmetic."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Optional, Tuple

from src.extraction.schemas import InvoiceExtractionRecord, LineItem
from src.validation.schemas import (
    ValidationConfig,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)

CURRENCY_CENT = Decimal("0.01")


def to_decimal(val: Optional[Any]) -> Optional[Decimal]:
    """Safely convert float, int, or string to Decimal, rounding half up."""
    if val is None:
        return None
    try:
        if isinstance(val, (int, str)):
            return Decimal(str(val)).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
        if isinstance(val, float):
            return Decimal(str(round(val, 4))).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
        return Decimal(str(val)).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return None


class FinancialRulesValidator:
    """Core validator implementing deterministic arithmetic and financial integrity rules."""

    def __init__(self, config: Optional[ValidationConfig] = None) -> None:
        self.config = config or ValidationConfig()
        self.tolerance = Decimal(str(self.config.rounding_tolerance))

    def evaluate(self, record: InvoiceExtractionRecord) -> Tuple[List[ValidationIssue], dict[str, Optional[float]]]:
        """Evaluate an invoice record across all financial rules.

        Returns:
            Tuple of (issues_list, computed_metrics_dict).
        """
        issues: List[ValidationIssue] = []
        metrics: dict[str, Optional[float]] = {
            "computed_line_sum": None,
            "computed_total": None,
            "total_variance": None,
        }

        # 1. Missing Required and Recommended Fields
        self._check_missing_fields(record, issues)

        # 2. Numeric Sanity & Format Validation
        self._check_numeric_sanity_and_formats(record, issues)

        # 3. Line-Item Internal Math (Quantity * Unit Price == Line Total)
        self._check_line_items_internal_math(record, issues)

        # 4. Line Items Sum vs Subtotal
        self._check_line_items_sum_vs_subtotal(record, issues, metrics)

        # 5. Subtotal + Tax vs Total
        self._check_subtotal_tax_vs_total(record, issues, metrics)

        # 6. Uncertainty & Upstream OCR Warnings
        self._check_uncertainties(record, issues)

        return issues, metrics

    def _check_missing_fields(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
    ) -> None:
        """Verify presence of required and recommended invoice fields."""
        for field in self.config.required_fields:
            val = getattr(record, field, None)
            if val is None or (isinstance(val, str) and not val.strip()):
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.MISSING_REQUIRED_FIELD,
                        severity=ValidationSeverity.ERROR,
                        field_name=field,
                        message=f"Mandatory financial field '{field}' is missing or null.",
                        expected_value="Non-null value",
                        actual_value="null",
                    )
                )

        for field in self.config.recommended_fields:
            val = getattr(record, field, None)
            if val is None or (isinstance(val, str) and not val.strip()):
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.MISSING_REQUIRED_FIELD,
                        severity=ValidationSeverity.WARNING,
                        field_name=field,
                        message=f"Recommended field '{field}' is unstated on the source document.",
                        expected_value="Recommended",
                        actual_value="null",
                    )
                )

    def _check_numeric_sanity_and_formats(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
    ) -> None:
        """Ensure numerical amounts are non-negative and codes conform to standards."""
        # Total must be strictly positive
        if record.total is not None and record.total <= 0:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                    severity=ValidationSeverity.ERROR,
                    field_name="total",
                    message=f"Invoice total must be strictly positive (> 0), got: {record.total}.",
                    expected_value="> 0.00",
                    actual_value=record.total,
                )
            )

        # Subtotal cannot be negative
        if record.subtotal is not None and record.subtotal < 0:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                    severity=ValidationSeverity.ERROR,
                    field_name="subtotal",
                    message=f"Subtotal cannot be negative, got: {record.subtotal}.",
                    expected_value=">= 0.00",
                    actual_value=record.subtotal,
                )
            )

        # Tax cannot be negative
        if record.tax is not None and record.tax < 0:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                    severity=ValidationSeverity.ERROR,
                    field_name="tax",
                    message=f"Tax amount cannot be negative, got: {record.tax}.",
                    expected_value=">= 0.00",
                    actual_value=record.tax,
                )
            )

        # Currency code validation (ISO 4217: 3 uppercase letters)
        if record.currency is not None:
            if not re.match(r"^[A-Z]{3}$", record.currency.strip()):
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                        severity=ValidationSeverity.WARNING,
                        field_name="currency",
                        message=f"Currency '{record.currency}' is not a valid 3-letter ISO-4217 code.",
                        expected_value="3-letter uppercase (e.g., USD)",
                        actual_value=record.currency,
                    )
                )

        # Date format validation
        if record.invoice_date is not None:
            try:
                datetime.strptime(record.invoice_date.strip(), "%Y-%m-%d")
            except ValueError:
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                        severity=ValidationSeverity.WARNING,
                        field_name="invoice_date",
                        message=f"Invoice date '{record.invoice_date}' does not conform to standardized YYYY-MM-DD format.",
                        expected_value="YYYY-MM-DD",
                        actual_value=record.invoice_date,
                    )
                )

        # Line item sanity checks
        for idx, item in enumerate(record.line_items):
            desc = (item.description or "").lower()
            is_discount = any(k in desc for k in ("discount", "rebate", "credit", "coupon"))

            if item.quantity is not None and item.quantity <= 0:
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                        severity=ValidationSeverity.ERROR,
                        field_name=f"line_items[{idx}].quantity",
                        message=f"Line item {idx+1} has invalid quantity ({item.quantity} <= 0).",
                        expected_value="> 0",
                        actual_value=item.quantity,
                    )
                )

            if item.unit_price is not None and item.unit_price < 0 and not is_discount:
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.INVALID_NUMERIC_VALUE,
                        severity=ValidationSeverity.ERROR,
                        field_name=f"line_items[{idx}].unit_price",
                        message=f"Line item {idx+1} has negative unit price (${item.unit_price:.2f}) without discount indicator.",
                        expected_value=">= 0.00",
                        actual_value=item.unit_price,
                    )
                )

    def _check_line_items_internal_math(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
    ) -> None:
        """Verify quantity * unit_price == line_total for each itemized row."""
        for idx, item in enumerate(record.line_items):
            if item.quantity is None or item.unit_price is None or item.line_total is None:
                continue

            qty = Decimal(str(item.quantity))
            price = Decimal(str(item.unit_price))
            actual_total = Decimal(str(item.line_total))

            expected_sub = (qty * price).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
            tax_add = Decimal(str(item.tax_amount or 0)).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
            expected_with_tax = (expected_sub + tax_add).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)

            # Match against either pretax line total or tax-inclusive line total
            diff_pretax = abs(actual_total - expected_sub)
            diff_with_tax = abs(actual_total - expected_with_tax)

            if diff_pretax > self.tolerance and diff_with_tax > self.tolerance:
                variance = float(actual_total - expected_sub)
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.LINE_ITEM_MATH_ERROR,
                        severity=ValidationSeverity.ERROR,
                        field_name=f"line_items[{idx}].line_total",
                        message=(
                            f"Line item {idx+1} ({item.description or 'Item'}) arithmetic error: "
                            f"{qty} * ${price:.2f} = ${expected_sub:.2f}, but extracted line total is ${actual_total:.2f}. "
                            f"Variance: ${variance:+.2f}"
                        ),
                        expected_value=float(expected_sub),
                        actual_value=float(actual_total),
                        variance=round(variance, 2),
                    )
                )

    def _check_line_items_sum_vs_subtotal(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
        metrics: dict[str, Optional[float]],
    ) -> None:
        """Verify sum(line_items.line_total) == subtotal."""
        if not record.line_items or record.subtotal is None:
            return

        valid_totals = [
            Decimal(str(item.line_total))
            for item in record.line_items
            if item.line_total is not None
        ]
        if not valid_totals:
            return

        computed_sum = sum(valid_totals).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
        metrics["computed_line_sum"] = float(computed_sum)

        subtotal_dec = to_decimal(record.subtotal)
        if subtotal_dec is not None:
            diff = abs(computed_sum - subtotal_dec)
            if diff > self.tolerance:
                variance = float(computed_sum - subtotal_dec)
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.LINE_ITEMS_SUM_MISMATCH,
                        severity=ValidationSeverity.ERROR,
                        field_name="subtotal",
                        message=(
                            f"Sum of line items (${computed_sum:,.2f}) does not match extracted subtotal "
                            f"(${subtotal_dec:,.2f}). Variance: ${variance:+,.2f}"
                        ),
                        expected_value=float(subtotal_dec),
                        actual_value=float(computed_sum),
                        variance=round(variance, 2),
                    )
                )

    def _check_subtotal_tax_vs_total(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
        metrics: dict[str, Optional[float]],
    ) -> None:
        """Verify subtotal + tax == total (or handle unstated tax)."""
        if record.total is None:
            return

        total_dec = to_decimal(record.total)
        subtotal_dec = to_decimal(record.subtotal)
        tax_dec = to_decimal(record.tax)

        if total_dec is None:
            return

        # Case A: Both subtotal and tax are explicitly present
        if subtotal_dec is not None and tax_dec is not None:
            computed_total = (subtotal_dec + tax_dec).quantize(CURRENCY_CENT, rounding=ROUND_HALF_UP)
            metrics["computed_total"] = float(computed_total)
            diff = abs(computed_total - total_dec)
            metrics["total_variance"] = float(computed_total - total_dec)

            if diff > self.tolerance:
                variance = float(computed_total - total_dec)
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.SUBTOTAL_TAX_TOTAL_MISMATCH,
                        severity=ValidationSeverity.ERROR,
                        field_name="total",
                        message=(
                            f"Mathematical discrepancy: Subtotal (${subtotal_dec:,.2f}) + Tax (${tax_dec:,.2f}) "
                            f"= ${computed_total:,.2f}, which does not match extracted Total (${total_dec:,.2f}). "
                            f"Variance: ${variance:+,.2f}"
                        ),
                        expected_value=float(computed_total),
                        actual_value=float(total_dec),
                        variance=round(variance, 2),
                    )
                )

        # Case B: Tax is missing/None, but subtotal is present
        elif subtotal_dec is not None and tax_dec is None:
            diff = total_dec - subtotal_dec
            if diff > self.tolerance:
                # Subtotal is less than total; unstated tax or shipping likely accounts for difference
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.SUBTOTAL_TAX_TOTAL_MISMATCH,
                        severity=ValidationSeverity.WARNING,
                        field_name="tax",
                        message=(
                            f"Subtotal (${subtotal_dec:,.2f}) is less than Total (${total_dec:,.2f}), but tax is unstated. "
                            f"Unextracted tax, shipping, or surcharges (${float(diff):,.2f}) may account for the difference."
                        ),
                        expected_value=float(total_dec),
                        actual_value=float(subtotal_dec),
                        variance=round(float(diff), 2),
                    )
                )
            elif (subtotal_dec - total_dec) > self.tolerance:
                # Subtotal exceeds total: error
                issues.append(
                    ValidationIssue(
                        rule_code=ValidationRuleCode.SUBTOTAL_TAX_TOTAL_MISMATCH,
                        severity=ValidationSeverity.ERROR,
                        field_name="total",
                        message=(
                            f"Subtotal (${subtotal_dec:,.2f}) exceeds Total (${total_dec:,.2f}). "
                            "Invoices cannot have subtotal greater than gross payable total."
                        ),
                        expected_value=float(subtotal_dec),
                        actual_value=float(total_dec),
                        variance=round(float(total_dec - subtotal_dec), 2),
                    )
                )

    def _check_uncertainties(
        self,
        record: InvoiceExtractionRecord,
        issues: List[ValidationIssue],
    ) -> None:
        """Flag low confidence extractions or OCR degradation."""
        # 1. Propagate uncertain_fields
        if record.uncertain_fields:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.LOW_EXTRACTION_CONFIDENCE,
                    severity=ValidationSeverity.WARNING,
                    field_name=record.uncertain_fields[0],
                    message=f"Fields flagged with low confidence or ambiguity: {', '.join(record.uncertain_fields)}.",
                )
            )

        # 2. Check field confidences below threshold
        low_conf_fields = [
            f for f, c in record.field_confidences.items()
            if c < self.config.auto_verify_confidence_threshold and f not in record.uncertain_fields
        ]
        if low_conf_fields:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.LOW_EXTRACTION_CONFIDENCE,
                    severity=ValidationSeverity.WARNING,
                    field_name=low_conf_fields[0],
                    message=f"Field confidences below {self.config.auto_verify_confidence_threshold:.2f} threshold: {', '.join(low_conf_fields)}.",
                )
            )

        # 3. Propagate extraction warnings
        for warn in record.extraction_warnings:
            issues.append(
                ValidationIssue(
                    rule_code=ValidationRuleCode.LOW_EXTRACTION_CONFIDENCE,
                    severity=ValidationSeverity.WARNING,
                    message=f"Upstream Extraction Warning: {warn}",
                )
            )
