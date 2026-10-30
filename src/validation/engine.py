"""Main validation orchestrator combining rules, deduplication, and cross-verification."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from src.extraction.schemas import InvoiceExtractionRecord
from src.ingestion.schemas import SheetContent
from src.validation.cross_verifier import CrossVerificationEngine
from src.validation.deduplicator import InvoiceDeduplicator
from src.validation.rules import FinancialRulesValidator
from src.validation.schemas import (
    BatchValidationReport,
    CrossVerificationMatch,
    RecordValidationResult,
    ValidationConfig,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)


class ValidationEngine:
    """Enterprise financial validation and cross-verification engine.

    Evaluates completeness, strict Decimal arithmetic, duplicate detection,
    and PDF-to-Excel / PDF-to-PDF reconciliation.
    """

    def __init__(self, config: Optional[ValidationConfig] = None) -> None:
        self.config = config or ValidationConfig()
        self.rules_validator = FinancialRulesValidator(config=self.config)
        self.deduplicator = InvoiceDeduplicator(config=self.config)
        self.cross_verifier = CrossVerificationEngine(config=self.config)

    def validate_record(
        self,
        record: InvoiceExtractionRecord,
        candidate_pool: Optional[List[InvoiceExtractionRecord]] = None,
        reference_ledger: Optional[Union[SheetContent, List[Dict[str, Any]], List[InvoiceExtractionRecord]]] = None,
        reference_po: Optional[InvoiceExtractionRecord] = None,
    ) -> RecordValidationResult:
        """Evaluate a single invoice record through rules, deduplication, and cross-verification."""
        all_issues: List[ValidationIssue] = []

        # 1. Evaluate Financial & Completeness Rules
        rule_issues, metrics = self.rules_validator.evaluate(record)
        all_issues.extend(rule_issues)

        # 2. Evaluate Duplicate Detection
        dup_issue = self.deduplicator.check_duplicate(record, candidates=candidate_pool)
        if dup_issue:
            all_issues.append(dup_issue)

        # 3. Cross-Verification (if reference ledger or PO provided)
        cross_match: Optional[CrossVerificationMatch] = None
        if reference_ledger is not None:
            cross_match, cross_issue = self.cross_verifier.reconcile_with_spreadsheet(
                pdf_record=record,
                ledger=reference_ledger,
            )
            if cross_issue:
                all_issues.append(cross_issue)

        elif reference_po is not None:
            cross_match, cross_issue = self.cross_verifier.reconcile_pdf_to_pdf(
                invoice_record=record,
                po_record=reference_po,
            )
            if cross_issue:
                all_issues.append(cross_issue)

        # 4. Synthesize HITL Decision Status
        has_errors = any(i.severity == ValidationSeverity.ERROR for i in all_issues)
        has_warnings = any(i.severity == ValidationSeverity.WARNING for i in all_issues)
        is_duplicate = any(
            i.rule_code in (ValidationRuleCode.EXACT_DUPLICATE_INVOICE, ValidationRuleCode.FUZZY_DUPLICATE_INVOICE)
            for i in all_issues
        )
        cross_variance = bool(cross_match and cross_match.match_status != "EXACT_MATCH")

        if is_duplicate:
            validation_status = "REJECTED_DUPLICATE"
            requires_manual_review = True
            is_valid = False
        elif has_errors or has_warnings or cross_variance:
            validation_status = "NEEDS_MANUAL_REVIEW"
            requires_manual_review = True
            is_valid = not has_errors
        else:
            validation_status = "AUTO_VERIFIED"
            requires_manual_review = False
            is_valid = True

        return RecordValidationResult(
            record_id=record.record_id,
            source_document_id=record.source_document_id,
            invoice_number=record.invoice_number,
            is_valid=is_valid,
            validation_status=validation_status,
            requires_manual_review=requires_manual_review,
            issues=all_issues,
            cross_verification=cross_match,
            computed_metrics=metrics,
        )

    def validate_batch(
        self,
        records: List[InvoiceExtractionRecord],
        reference_ledger: Optional[Union[SheetContent, List[Dict[str, Any]], List[InvoiceExtractionRecord]]] = None,
        reference_pdfs: Optional[Union[Dict[str, InvoiceExtractionRecord], List[InvoiceExtractionRecord]]] = None,
    ) -> BatchValidationReport:
        """Validate an entire batch of records with mutual deduplication and optional cross-verification."""
        results: List[RecordValidationResult] = []
        auto_verified = 0
        manual_review = 0
        duplicate_count = 0
        cross_exact = 0
        cross_variance = 0

        # Build mapping for reference PDFs if provided
        po_map: Dict[str, InvoiceExtractionRecord] = {}
        if isinstance(reference_pdfs, dict):
            po_map = reference_pdfs
        elif isinstance(reference_pdfs, list):
            for po in reference_pdfs:
                if po.invoice_number:
                    po_map[po.invoice_number] = po
                po_map[po.record_id] = po

        # Evaluate records
        seen_records: List[InvoiceExtractionRecord] = []
        for record in records:
            matching_po = None
            if record.invoice_number and record.invoice_number in po_map:
                matching_po = po_map[record.invoice_number]

            res = self.validate_record(
                record=record,
                candidate_pool=seen_records,
                reference_ledger=reference_ledger,
                reference_po=matching_po,
            )
            seen_records.append(record)
            results.append(res)

            # Tally metrics
            if res.validation_status == "AUTO_VERIFIED":
                auto_verified += 1
            if res.requires_manual_review:
                manual_review += 1
            if res.validation_status == "REJECTED_DUPLICATE":
                duplicate_count += 1

            if res.cross_verification:
                if res.cross_verification.match_status == "EXACT_MATCH":
                    cross_exact += 1
                elif res.cross_verification.match_status == "VARIANCE_DETECTED":
                    cross_variance += 1

        return BatchValidationReport(
            total_records_evaluated=len(records),
            auto_verified_count=auto_verified,
            manual_review_count=manual_review,
            duplicate_count=duplicate_count,
            cross_verified_exact_matches=cross_exact,
            cross_verified_variances=cross_variance,
            results=results,
        )
