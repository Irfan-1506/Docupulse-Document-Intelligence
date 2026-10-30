"""Strict Pydantic v2 schemas for financial information extraction."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ExtractionProvider(str, Enum):
    """Supported LLM / rule-based extraction providers."""
    GEMINI = "gemini"
    OPENAI = "openai"
    OLLAMA = "ollama"
    HEURISTIC_FALLBACK = "heuristic_fallback"


class ExtractedField(BaseModel, Generic[T]):
    """Generic envelope capturing extracted value, confidence, and grounding citation."""
    model_config = ConfigDict(extra="ignore")

    value: Optional[T] = Field(
        default=None,
        description="Extracted value, or None if absent or ambiguous on the source document.",
    )
    confidence: float = Field(
        default=0.0,
        description="Confidence score for this field between 0.0 and 1.0.",
        ge=0.0,
        le=1.0,
    )
    raw_evidence: Optional[str] = Field(
        default=None,
        description="Exact verbatim text snippet from the document serving as source proof.",
    )
    source_page_or_sheet: Optional[str] = Field(
        default=None,
        description="Citation indicating the specific page or sheet containing the evidence.",
    )
    is_uncertain: bool = Field(
        default=False,
        description="True if value was partially illegible, ambiguous, or below confidence threshold.",
    )


class LineItem(BaseModel):
    """Standardized invoice line item."""
    model_config = ConfigDict(extra="ignore")

    description: Optional[str] = Field(
        default=None,
        description="Item description or service rendered.",
    )
    quantity: Optional[float] = Field(
        default=None,
        description="Quantity billed.",
    )
    unit_price: Optional[float] = Field(
        default=None,
        description="Unit price per item.",
    )
    tax_amount: Optional[float] = Field(
        default=None,
        description="Tax amount specifically attributed to this line item.",
    )
    line_total: Optional[float] = Field(
        default=None,
        description="Total charge for this line item.",
    )
    sku_or_code: Optional[str] = Field(
        default=None,
        description="Product code, SKU, or part number.",
    )
    confidence: float = Field(
        default=0.0,
        description="Confidence score for this line item extraction.",
        ge=0.0,
        le=1.0,
    )
    raw_evidence: Optional[str] = Field(
        default=None,
        description="Source table row or text snippet.",
    )


class InvoiceExtractionRecord(BaseModel):
    """Fully schema-validated extraction record representing an individual financial invoice."""
    model_config = ConfigDict(extra="ignore")

    record_id: str = Field(
        ...,
        description="Unique identifier for this invoice extraction record.",
    )
    source_document_id: str = Field(
        ...,
        description="ID of the parent document from Task 1 DocumentProcessingResult.",
    )
    page_range_or_sheet: str = Field(
        ...,
        description="Span of pages or sheet name (e.g., 'pages 1-2' or 'Sheet: Q3_Invoices').",
    )
    invoice_number: Optional[str] = Field(
        default=None,
        description="Unique invoice identifier or code.",
    )
    invoice_date: Optional[str] = Field(
        default=None,
        description="Invoice date normalized to YYYY-MM-DD when unambiguous, or verbatim raw string.",
    )
    supplier_name: Optional[str] = Field(
        default=None,
        description="Legal vendor, supplier, or billing entity name.",
    )
    customer_name: Optional[str] = Field(
        default=None,
        description="Recipient, client, or billed party name.",
    )
    subtotal: Optional[float] = Field(
        default=None,
        description="Pre-tax subtotal amount explicitly stated on document. None if not written.",
    )
    tax: Optional[float] = Field(
        default=None,
        description="Total tax amount explicitly stated on document. None if not written.",
    )
    total: Optional[float] = Field(
        default=None,
        description="Gross invoice total amount payable. None if not written.",
    )
    currency: Optional[str] = Field(
        default=None,
        description="ISO 4217 3-letter currency code (e.g., USD, EUR, BDT, GBP). None if absent.",
    )
    line_items: List[LineItem] = Field(
        default_factory=list,
        description="Individual itemized product/service charges.",
    )
    field_confidences: Dict[str, float] = Field(
        default_factory=dict,
        description="Confidence scores per extracted field key.",
    )
    raw_evidence_snippets: Dict[str, str] = Field(
        default_factory=dict,
        description="Verbatim text snippets grounding each extracted field to the document.",
    )
    uncertain_fields: List[str] = Field(
        default_factory=list,
        description="Field names flagged for low confidence, OCR degradation, or ambiguity.",
    )
    missing_required_fields: List[str] = Field(
        default_factory=list,
        description="Expected fields that were absent or null on the source document.",
    )
    extraction_warnings: List[str] = Field(
        default_factory=list,
        description="Diagnostic warnings regarding illegibility, formatting, or validation.",
    )


class DocumentExtractionResponse(BaseModel):
    """Container returned by the AI Information Extraction Engine for a document."""
    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(
        ...,
        description="UUID of the ingested document from Task 1.",
    )
    file_name: str = Field(
        ...,
        description="Name of the source document file.",
    )
    total_invoices_detected: int = Field(
        default=0,
        description="Count of distinct invoice records extracted.",
        ge=0,
    )
    records: List[InvoiceExtractionRecord] = Field(
        default_factory=list,
        description="List of extracted invoice records.",
    )
    model_used: str = Field(
        ...,
        description="Model or engine identifier used for extraction.",
    )
    processing_time_ms: float = Field(
        ...,
        description="Total latency in milliseconds for extraction.",
        ge=0.0,
    )
    telemetry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional tracking telemetry such as token counts or worker execution times.",
    )


class ExtractionConfig(BaseModel):
    """Configuration settings governing extraction, provider selection, and thresholds."""
    model_config = ConfigDict(extra="ignore")

    provider: ExtractionProvider = Field(
        default=ExtractionProvider.HEURISTIC_FALLBACK,
        description="Extraction backend to employ ('gemini', 'openai', 'ollama', 'heuristic_fallback').",
    )
    model_name: str = Field(
        default="gemini-2.5-flash",
        description="LLM model identifier to invoke.",
    )
    temperature: float = Field(
        default=0.0,
        description="Sampling temperature (0.0 for deterministic factual extraction).",
        ge=0.0,
        le=1.0,
    )
    uncertainty_threshold: float = Field(
        default=0.75,
        description="Confidence threshold below which fields are tagged in uncertain_fields.",
        ge=0.0,
        le=1.0,
    )
    required_fields: List[str] = Field(
        default_factory=lambda: ["invoice_number", "invoice_date", "total", "supplier_name"],
        description="Fields expected to be present on standard invoices.",
    )
    max_concurrency: int = Field(
        default=5,
        description="Maximum concurrent async extraction tasks for multi-page documents.",
        ge=1,
    )
    enable_grounding_verification: bool = Field(
        default=True,
        description="Verify extracted values against raw source text to reject hallucinations.",
    )
