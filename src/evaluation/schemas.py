"""Pydantic v2 schemas for ground truth datasets, field metrics, and benchmark reporting."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    """Document format classification in evaluation benchmark."""
    DIGITAL_PDF = "DIGITAL_PDF"
    SCANNED_PDF = "SCANNED_PDF"
    IMAGE_INVOICE = "IMAGE_INVOICE"
    EXCEL_WORKBOOK = "EXCEL_WORKBOOK"
    CSV_SPREADSHEET = "CSV_SPREADSHEET"
    CORRUPTED_BINARY = "CORRUPTED_BINARY"


class GroundTruthLineItem(BaseModel):
    """Ground truth expected line item."""
    description: str
    quantity: float
    unit_price: float
    amount: float


class GroundTruthRecord(BaseModel):
    """Ground truth expected invoice extraction data for a single document or sheet."""
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    supplier_name: Optional[str] = None
    customer_name: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = "USD"
    line_items: List[GroundTruthLineItem] = Field(default_factory=list)
    is_corrupt: bool = False
    expected_page_count: int = 1


class GoldenDocumentManifest(BaseModel):
    """Manifest describing a labeled golden dataset document."""
    file_name: str
    relative_path: str
    doc_type: DocumentType
    page_count: int
    records: List[GroundTruthRecord] = Field(default_factory=list)
    description: str = ""


class FieldEvaluationScore(BaseModel):
    """Precision, recall, and F1 metrics for a specific field."""
    field_name: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    exact_matches: int = 0
    total_evaluated: int = 0


class LatencyPercentiles(BaseModel):
    """Latency distribution per page in milliseconds and seconds."""
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    p95_sec_per_page: float
    min_ms: float
    max_ms: float
    mean_ms: float


class OperationalMetrics(BaseModel):
    """High-level operational throughput and reliability metrics."""
    total_documents: int
    total_pages: int
    successful_extractions: int
    failed_documents: int
    corrupted_detected_correctly: int
    straight_through_processing_rate: float
    human_in_the_loop_rate: float
    graceful_corruption_rejection_rate: float


class EvaluationBenchmarkReport(BaseModel):
    """Formal benchmark evaluation report output."""
    report_id: str
    timestamp: datetime
    system_version: str = "DocuPulse Enterprise AP v4.2"
    dataset_name: str
    total_samples: int
    field_metrics: Dict[str, FieldEvaluationScore]
    macro_precision: float
    macro_recall: float
    macro_f1: float
    latency: LatencyPercentiles
    operational: OperationalMetrics
    meets_f1_threshold: bool
    meets_latency_threshold: bool
    production_ready: bool
