"""Standardized Pydantic v2 schemas for document ingestion and processing."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class ExtractionMethod(str, Enum):
    """Method used to extract text and content from a page or sheet."""
    DIGITAL_PDF = "digital_pdf"
    OCR = "ocr"
    SPREADSHEET = "spreadsheet"


class DocumentStatus(str, Enum):
    """Terminal or processing status of a document."""
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED_CORRUPTED = "FAILED_CORRUPTED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"


class PageContent(BaseModel):
    """Extracted text, structured tables, and metadata for a single document page."""
    model_config = ConfigDict(extra="ignore")

    page_number: int = Field(
        ...,
        description="1-based page number within the original document.",
        ge=1,
    )
    raw_text: str = Field(
        ...,
        description="Raw extracted or OCR-recognized textual content of the page.",
    )
    extraction_method: ExtractionMethod = Field(
        ...,
        description="Pipeline method used ('digital_pdf', 'ocr', 'spreadsheet').",
    )
    confidence_score: float = Field(
        ...,
        description="Confidence score for the extraction (1.0 for digital, mean OCR confidence for scanned).",
        ge=0.0,
        le=1.0,
    )
    tables: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured tables detected on the page, represented as dictionaries or matrix lists.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Any diagnostic warnings (e.g. low OCR confidence, partial readability).",
    )


class SheetContent(BaseModel):
    """Structured data and LLM-friendly previews for an individual spreadsheet sheet."""
    model_config = ConfigDict(extra="ignore")

    sheet_name: str = Field(
        ...,
        description="Name or title of the worksheet.",
    )
    row_count: int = Field(
        ...,
        description="Total number of non-empty data rows extracted.",
        ge=0,
    )
    column_headers: list[str] = Field(
        ...,
        description="Cleaned, standardized column header names.",
    )
    records: list[dict[str, Any]] = Field(
        ...,
        description="Structured tabular records as list of row dictionaries.",
    )
    markdown_preview: str = Field(
        ...,
        description="Compact Markdown-formatted table preview for LLM / cross-verification prompts.",
    )
    summary_stats: dict[str, Any] = Field(
        default_factory=dict,
        description="Statistical summary of numeric columns, missing values, and shape.",
    )


class DocumentProcessingResult(BaseModel):
    """Unified document processing container consumed downstream by LLM and DB loaders."""
    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(
        ...,
        description="Globally unique identifier (UUID) assigned to this processing run.",
    )
    file_name: str = Field(
        ...,
        description="Original name of the input file.",
    )
    file_type: str = Field(
        ...,
        description="Normalized file extension / document category (e.g., pdf, xlsx, png).",
    )
    file_hash: str = Field(
        ...,
        description="SHA-256 cryptographic hash of the input file.",
    )
    status: DocumentStatus = Field(
        ...,
        description="Terminal pipeline status (SUCCESS, PARTIAL_SUCCESS, FAILED_CORRUPTED, UNSUPPORTED_FORMAT).",
    )
    total_pages_or_sheets: int = Field(
        default=0,
        description="Count of processed pages (for PDFs/images) or sheets (for spreadsheets).",
        ge=0,
    )
    pages: list[PageContent] = Field(
        default_factory=list,
        description="Page-level content for multi-page documents (PDFs, multi-frame TIFFs, images).",
    )
    sheets: list[SheetContent] = Field(
        default_factory=list,
        description="Worksheet content for tabular spreadsheets (XLSX, XLS, CSV).",
    )
    full_merged_text: str = Field(
        default="",
        description="Aggregated textual representation of all pages or sheets.",
    )
    processing_time_ms: float = Field(
        ...,
        description="Total execution latency in milliseconds.",
        ge=0.0,
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Human-readable error details and diagnostics if status is FAILED_*.",
    )


class IngestionConfig(BaseModel):
    """Tunable runtime configuration for the ingestion engine."""
    model_config = ConfigDict(extra="ignore")

    # PDF Processing
    pdf_chunk_size: int = Field(
        default=30,
        description="Number of pages to batch in a single memory-safe processing chunk.",
        ge=1,
    )
    min_digital_text_chars: int = Field(
        default=40,
        description="Minimum character count to consider a PDF page digitally native before OCR fallback.",
        ge=0,
    )
    max_raster_coverage_ratio: float = Field(
        default=0.85,
        description="Ratio of page area covered by raster image to trigger OCR inspection.",
        ge=0.0,
        le=1.0,
    )
    render_dpi: int = Field(
        default=150,
        description="DPI used when rasterizing PDF pages for OCR fallback.",
        ge=72,
        le=600,
    )

    # OCR & Image Processing
    ocr_confidence_threshold: float = Field(
        default=0.60,
        description="Confidence threshold below which warnings are flagged.",
        ge=0.0,
        le=1.0,
    )
    enable_image_preprocessing: bool = Field(
        default=True,
        description="Whether to run OpenCV CLAHE, denoising, and deskew before OCR.",
    )
    ocr_engine: str = Field(
        default="auto",
        description="OCR backend: 'auto', 'rapidocr', or 'tesseract'.",
    )

    # Spreadsheet Processing
    max_spreadsheet_preview_rows: int = Field(
        default=15,
        description="Maximum rows to include in markdown previews.",
        ge=1,
    )

    # Parallelism
    max_parallel_workers: int = Field(
        default=4,
        description="Maximum worker threads/processes for page-level parallelism.",
        ge=1,
    )
