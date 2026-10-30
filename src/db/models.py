"""SQLAlchemy 2.0 relational models for documents, records, validation, audit logs, and vector embeddings."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    """Declarative base class for document intelligence database models."""
    pass


class VectorType(TypeDecorator):
    """Cross-dialect vector embedding type.

    Uses pgvector's Vector type on PostgreSQL, falling back to JSON serialization
    for SQLite local development and CI testing.
    """
    impl = JSON
    cache_ok = True

    def __init__(self, dimension: int = 1536, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.dimension = dimension

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector
                return dialect.type_descriptor(Vector(self.dimension))
            except ImportError:
                return dialect.type_descriptor(JSONB)
        return dialect.type_descriptor(JSON)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            if dialect.name == "postgresql":
                return [float(x) for x in value]
            return [float(x) for x in value]
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value


class DocumentModel(Base):
    """Primary document metadata record."""
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    storage_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    total_pages_or_sheets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="SUCCESS", index=True, nullable=False)
    full_merged_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    pages: Mapped[List[DocumentPageModel]] = relationship(
        "DocumentPageModel",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentPageModel.page_number",
    )
    sheets: Mapped[List[DocumentSheetModel]] = relationship(
        "DocumentSheetModel",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    records: Mapped[List[ExtractedRecordModel]] = relationship(
        "ExtractedRecordModel",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    embeddings: Mapped[List[DocumentEmbeddingModel]] = relationship(
        "DocumentEmbeddingModel",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentPageModel(Base):
    """Page-level OCR and digital text extraction results."""
    __tablename__ = "document_pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(50), default="digital_pdf", nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    tables_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped[DocumentModel] = relationship("DocumentModel", back_populates="pages")

    __table_args__ = (
        Index("ix_doc_page_number", "document_id", "page_number"),
    )


class DocumentSheetModel(Base):
    """Spreadsheet workbook sheet structure and markdown tables."""
    __tablename__ = "document_sheets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    column_headers: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    records_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    markdown_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_stats: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped[DocumentModel] = relationship("DocumentModel", back_populates="sheets")


class ExtractedRecordModel(Base):
    """Extracted and schema-validated invoice record."""
    __tablename__ = "extracted_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    record_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    page_range_or_sheet: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    invoice_date: Mapped[Optional[date]] = mapped_column(Date, index=True, nullable=True)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    subtotal: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    tax: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    total: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), index=True, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), index=True, nullable=True)
    line_items: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    field_confidences: Mapped[Dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    uncertain_fields: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    extraction_warnings: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped[DocumentModel] = relationship("DocumentModel", back_populates="records")
    validation_result: Mapped[Optional[ValidationResultModel]] = relationship(
        "ValidationResultModel",
        back_populates="record",
        uselist=False,
        cascade="all, delete-orphan",
    )
    audit_logs: Mapped[List[AuditLogModel]] = relationship(
        "AuditLogModel",
        back_populates="record",
        cascade="all, delete-orphan",
        order_by="AuditLogModel.timestamp",
    )


class ValidationResultModel(Base):
    """Validation outcome and mathematical consistency diagnostics."""
    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    record_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extracted_records.record_id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(50), default="AUTO_VERIFIED", index=True, nullable=False)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    issues: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    cross_verification_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    computed_metrics: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    record: Mapped[ExtractedRecordModel] = relationship("ExtractedRecordModel", back_populates="validation_result")


class AuditLogModel(Base):
    """Field-level reviewer correction history."""
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    record_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extracted_records.record_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    field_changed: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    record: Mapped[ExtractedRecordModel] = relationship("ExtractedRecordModel", back_populates="audit_logs")


class DocumentEmbeddingModel(Base):
    """Document text chunks and vector embeddings for semantic similarity search."""
    __tablename__ = "document_embeddings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[List[float]] = mapped_column(VectorType(dimension=1536), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=1536, nullable=False)
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped[DocumentModel] = relationship("DocumentModel", back_populates="embeddings")


class ProcessingJobModel(Base):
    """Persistent background processing jobs."""
    __tablename__ = "processing_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    document_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    options_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
