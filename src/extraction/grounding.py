"""Grounding verifier, anti-hallucination guard, and Task 1 OCR warning propagator."""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Set

from src.extraction.schemas import ExtractionConfig, InvoiceExtractionRecord

logger = logging.getLogger(__name__)


class GroundingVerifier:
    """Anti-hallucination verification engine.

    Ensures every extracted field is grounded in the source text, rejects
    hallucinated numbers or fabricated entities, and propagates upstream OCR
    degradation warnings from Task 1.
    """

    def __init__(self, config: Optional[ExtractionConfig] = None) -> None:
        self.config = config or ExtractionConfig()

    @staticmethod
    def _normalize_text_for_search(text: str) -> str:
        """Normalize whitespace and punctuation for relaxed token search."""
        return re.sub(r"\s+", " ", text).strip().lower()

    @staticmethod
    def number_in_text(val: float, raw_text: str) -> bool:
        """Check whether a floating point number appears in source text in common financial formats."""
        if val is None:
            return True

        # Generate common representations: e.g. 1250.5 -> ["1250.50", "1,250.50", "1250.5", "1 250.50"]
        int_part = int(val)
        frac_part = abs(val - int_part)

        candidates: Set[str] = {
            f"{val:.2f}",
            f"{val:.2f}".replace(".", ","),  # European comma decimal
            f"{val:g}",
            f"{int_part:,}.{int(round(frac_part * 100)):02d}",  # 1,250.50
            f"{int_part:,}".replace(",", "."),  # 1.250
            str(int_part),
        }

        # Also search for compact digit sequences
        raw_clean = raw_text.replace(",", "").replace(" ", "").replace("\xa0", "")
        clean_val_str = f"{val:.2f}".replace(",", "").replace(" ", "")

        if clean_val_str in raw_clean:
            return True

        for cand in candidates:
            if cand in raw_text:
                return True

        return False

    @staticmethod
    def string_in_text(val: str, raw_text: str) -> bool:
        """Check whether a text string or its principal tokens appear in source text."""
        if not val or not val.strip():
            return True

        val_norm = GroundingVerifier._normalize_text_for_search(val)
        raw_norm = GroundingVerifier._normalize_text_for_search(raw_text)

        if val_norm in raw_norm:
            return True

        # Check alphanumeric stripped search (e.g. "INV-2026-001" vs "INV2026001")
        val_alnum = re.sub(r"[^a-zA-Z0-9]", "", val_norm)
        raw_alnum = re.sub(r"[^a-zA-Z0-9]", "", raw_norm)
        if val_alnum and val_alnum in raw_alnum:
            return True

        # Token-level presence: if multi-word name, at least 70% of distinctive tokens should match
        tokens = [t for t in val_norm.split() if len(t) > 2 and t not in ("inc", "corp", "ltd", "gmbh", "the")]
        if tokens:
            matches = sum(1 for t in tokens if t in raw_norm)
            return (matches / len(tokens)) >= 0.7

        return False

    def verify_and_harden_record(
        self,
        record: InvoiceExtractionRecord,
        source_text: str,
        ocr_confidence: float = 1.0,
        ocr_warnings: Optional[List[str]] = None,
    ) -> InvoiceExtractionRecord:
        """Run comprehensive anti-hallucination checks, audit citations, and calibrate confidences."""
        if not self.config.enable_grounding_verification:
            return record

        ocr_warnings = ocr_warnings or []
        raw_norm = source_text

        # 1. Propagate OCR degradation warnings from Task 1
        if ocr_confidence < 0.60 or any("Low OCR" in w for w in ocr_warnings):
            warn_msg = f"Upstream OCR quality was flagged (confidence: {ocr_confidence:.2f}); manual verification recommended."
            if warn_msg not in record.extraction_warnings:
                record.extraction_warnings.append(warn_msg)
            # Downgrade field confidences
            for field in list(record.field_confidences.keys()):
                record.field_confidences[field] = round(min(record.field_confidences[field], ocr_confidence), 2)
                if field not in record.uncertain_fields:
                    record.uncertain_fields.append(field)

        # 2. Verify Invoice Number
        if record.invoice_number is not None:
            if not self.string_in_text(record.invoice_number, raw_norm):
                record.extraction_warnings.append(
                    f"Hallucination rejected: invoice_number '{record.invoice_number}' not grounded in source text."
                )
                record.field_confidences["invoice_number"] = 0.0
                if "invoice_number" not in record.uncertain_fields:
                    record.uncertain_fields.append("invoice_number")
                record.invoice_number = None

        # 3. Verify Numerical Fields (total, subtotal, tax)
        for num_field in ("total", "subtotal", "tax"):
            val = getattr(record, num_field, None)
            if val is not None:
                if not self.number_in_text(val, raw_norm):
                    record.extraction_warnings.append(
                        f"Hallucination rejected: {num_field} value {val} has no numerical evidence in source text."
                    )
                    record.field_confidences[num_field] = 0.0
                    if num_field not in record.uncertain_fields:
                        record.uncertain_fields.append(num_field)
                    setattr(record, num_field, None)

        # 4. Verify Supplier & Customer Names
        for name_field in ("supplier_name", "customer_name"):
            val = getattr(record, name_field, None)
            if val is not None:
                if not self.string_in_text(val, raw_norm):
                    record.extraction_warnings.append(
                        f"Ungrounded entity warning: {name_field} '{val}' could not be strongly matched in source text."
                    )
                    record.field_confidences[name_field] = round(
                        min(record.field_confidences.get(name_field, 0.8), 0.40), 2
                    )
                    if name_field not in record.uncertain_fields:
                        record.uncertain_fields.append(name_field)

        # 5. Populate Missing Required Fields
        for req in self.config.required_fields:
            val = getattr(record, req, None)
            if val is None and req not in record.missing_required_fields:
                record.missing_required_fields.append(req)

        # 6. Flag any fields below uncertainty threshold
        for field, conf in record.field_confidences.items():
            if conf < self.config.uncertainty_threshold and field not in record.uncertain_fields:
                record.uncertain_fields.append(field)

        # Deduplicate lists
        record.uncertain_fields = sorted(list(set(record.uncertain_fields)))
        record.missing_required_fields = sorted(list(set(record.missing_required_fields)))
        record.extraction_warnings = list(dict.fromkeys(record.extraction_warnings))

        return record
