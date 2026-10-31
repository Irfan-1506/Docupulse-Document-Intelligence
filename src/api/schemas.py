"""Strict Pydantic v2 schemas for REST API contracts, job telemetry, and HITL workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.extraction.schemas import DocumentExtractionResponse, InvoiceExtractionRecord, LineItem
from src.ingestion.schemas import DocumentProcessingResult
from src.validation.schemas import BatchValidationReport, CrossVerificationMatch, RecordValidationResult


class JobStage(str, Enum):
    """Lifecycle stages for document background processing."""
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    INGESTING = "INGESTING"
    OCR_PROCESSING = "OCR_PROCESSING"
    AI_EXTRACTING = "AI_EXTRACTING"
    VALIDATING = "VALIDATING"
    CROSS_VERIFYING = "CROSS_VERIFYING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class JobStatusResponse(BaseModel):
    """Real-time progress telemetry and diagnostic status for a background job."""
    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(..., description="Unique job identifier.")
    batch_id: Optional[str] = Field(default=None, description="Batch identifier if part of a multi-file upload.")
    document_id: Optional[str] = Field(default=None, description="Ingested document ID once known.")
    file_name: str = Field(..., description="Name of the uploaded file.")
    file_type: str = Field(..., description="Normalized document format.")
    stage: JobStage = Field(..., description="Current processing lifecycle stage.")
    progress_percent: int = Field(default=0, ge=0, le=100, description="Completion percentage (0-100).")
    pages_processed: int = Field(default=0, ge=0, description="Pages or sheets processed so far.")
    total_pages: int = Field(default=0, ge=0, description="Total pages or sheets in the document.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processing_time_ms: Optional[float] = Field(default=None, description="Elapsed wall-clock latency in ms.")
    error_message: Optional[str] = Field(default=None, description="Diagnostic error details if stage is FAILED.")
    retry_count: int = Field(default=0, ge=0, description="Number of times this job has been retried.")
    attempt: int = Field(default=1, ge=1, description="Current processing attempt.")
    max_attempts: int = Field(default=3, ge=1, description="Maximum allowed attempts before dead-lettering.")
    idempotency_key: Optional[str] = Field(default=None, description="Idempotency key to prevent duplicate processing.")


class BatchStatusResponse(BaseModel):
    """Batch-level aggregate progress summary."""
    model_config = ConfigDict(extra="ignore")

    batch_id: str = Field(..., description="Unique batch identifier.")
    total_jobs: int = Field(default=0, ge=0)
    completed_jobs: int = Field(default=0, ge=0)
    failed_jobs: int = Field(default=0, ge=0)
    in_progress_jobs: int = Field(default=0, ge=0)
    jobs: List[JobStatusResponse] = Field(default_factory=list)


class BatchUploadResponse(BaseModel):
    """202 Accepted response returned immediately upon multi-file document upload."""
    model_config = ConfigDict(extra="ignore")

    batch_id: str = Field(..., description="Generated batch identifier.")
    total_files: int = Field(..., ge=1, description="Number of files queued.")
    jobs: List[JobStatusResponse] = Field(default_factory=list)
    message: str = Field(default="Files accepted and queued for asynchronous processing.")


class ManualCorrectionRequest(BaseModel):
    """Payload for human reviewer overrides on an extracted invoice record."""
    model_config = ConfigDict(extra="ignore")

    corrected_by: str = Field(default="human_reviewer", description="User ID or name of the reviewer.")
    correction_notes: Optional[str] = Field(default=None, description="Audit rationale for the modification.")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    supplier_name: Optional[str] = None
    customer_name: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    line_items: Optional[List[LineItem]] = None
    mark_as_verified: bool = Field(
        default=False,
        description="Explicitly approve and mark record as verified, clearing manual review requirements.",
    )


class AuditLogEntry(BaseModel):
    """Immutable audit trail entry recording an edit to an invoice field."""
    model_config = ConfigDict(extra="ignore")

    entry_id: str = Field(..., description="Unique log entry identifier.")
    record_id: str = Field(..., description="Target invoice record ID.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = Field(..., description="User or automated system that performed the change.")
    field_changed: str = Field(..., description="Name of the updated attribute.")
    old_value: Optional[Any] = Field(default=None, description="Previous value before correction.")
    new_value: Optional[Any] = Field(default=None, description="Updated value.")
    notes: Optional[str] = Field(default=None, description="Contextual reviewer comments.")


class RecordDetailResponse(BaseModel):
    """Comprehensive details for a single invoice record including validation and audit trail."""
    model_config = ConfigDict(extra="ignore")

    record: InvoiceExtractionRecord
    validation: RecordValidationResult
    audit_trail: List[AuditLogEntry] = Field(default_factory=list)


class DocumentResultsResponse(BaseModel):
    """Full extraction, validation, and ingestion results for a document."""
    model_config = ConfigDict(extra="ignore")

    document_id: str
    file_name: str
    file_type: str
    ingestion: Optional[DocumentProcessingResult] = None
    extraction: Optional[DocumentExtractionResponse] = None
    validation: List[RecordValidationResult] = Field(default_factory=list)
    audit_trail: List[AuditLogEntry] = Field(default_factory=list)


class SearchQueryParams(BaseModel):
    """Search and filter parameters for financial records."""
    model_config = ConfigDict(extra="ignore")

    query: Optional[str] = Field(default=None, description="Full-text keyword search across text, entities, and line items.")
    supplier: Optional[str] = None
    customer: Optional[str] = None
    invoice_number: Optional[str] = None
    currency: Optional[str] = None
    min_total: Optional[float] = None
    max_total: Optional[float] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    validation_status: Optional[str] = Field(
        default=None,
        description="Filter by status: 'AUTO_VERIFIED', 'NEEDS_MANUAL_REVIEW', 'REJECTED_DUPLICATE'.",
    )
    requires_manual_review: Optional[bool] = None
    file_type: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class SearchResponse(BaseModel):
    """Paginated search response containing matched invoice records."""
    model_config = ConfigDict(extra="ignore")

    total: int = Field(default=0, ge=0)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1)
    total_pages: int = Field(default=1, ge=1)
    items: List[RecordDetailResponse] = Field(default_factory=list)


class JobsListResponse(BaseModel):
    """List of all background jobs with dashboard summary statistics."""
    model_config = ConfigDict(extra="ignore")

    total: int = Field(default=0, ge=0)
    summary_by_stage: Dict[str, int] = Field(default_factory=dict)
    jobs: List[JobStatusResponse] = Field(default_factory=list)


class ReprocessJobRequest(BaseModel):
    """Optional configuration overrides for reprocessing a job."""
    model_config = ConfigDict(extra="ignore")

    force_ocr: bool = Field(default=False, description="Force full-page OCR rasterization.")
    provider_override: Optional[str] = Field(default=None, description="Override LLM provider (e.g., 'gemini', 'heuristic_fallback').")


class CrossVerifyRequest(BaseModel):
    """Payload for on-demand cross-verification between specific documents."""
    model_config = ConfigDict(extra="ignore")

    invoice_document_ids: List[str] = Field(..., description="Document IDs representing primary invoice PDFs.")
    reference_document_id: str = Field(..., description="Document ID representing reference ledger (Excel/CSV) or PO PDF.")
