"""Financial Data Validation & Cross-Verification Engine."""

from src.validation.schemas import (
    BatchValidationReport,
    CrossVerificationMatch,
    RecordValidationResult,
    ValidationConfig,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)
from src.validation.rules import FinancialRulesValidator, to_decimal
from src.validation.deduplicator import (
    InvoiceDeduplicator,
    normalize_entity_name,
    normalize_invoice_number,
    ocr_soft_normalize_invoice_number,
)
from src.validation.cross_verifier import CrossVerificationEngine
from src.validation.engine import ValidationEngine

__all__ = [
    "ValidationEngine",
    "FinancialRulesValidator",
    "InvoiceDeduplicator",
    "CrossVerificationEngine",
    "to_decimal",
    "normalize_entity_name",
    "normalize_invoice_number",
    "ocr_soft_normalize_invoice_number",
    "BatchValidationReport",
    "CrossVerificationMatch",
    "RecordValidationResult",
    "ValidationConfig",
    "ValidationIssue",
    "ValidationRuleCode",
    "ValidationSeverity",
]
