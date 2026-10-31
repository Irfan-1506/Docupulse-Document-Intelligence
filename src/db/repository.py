"""High-performance SQLAlchemy database repository for documents, extraction, validation, and search."""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import date, datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.api.schemas import (
    AuditLogEntry,
    DocumentResultsResponse,
    JobStage,
    JobStatusResponse,
    JobsListResponse,
    RecordDetailResponse,
    SearchQueryParams,
    SearchResponse,
)
from src.db.models import (
    AuditLogModel,
    DocumentEmbeddingModel,
    DocumentModel,
    DocumentPageModel,
    DocumentSheetModel,
    ExtractedRecordModel,
    ProcessingJobModel,
    ValidationResultModel,
)
from src.db.search import HybridSearchEngine, get_default_embedder
from src.db.session import get_engine, get_session_factory
from src.extraction.schemas import (
    DocumentExtractionResponse,
    InvoiceExtractionRecord,
    LineItem,
)
from src.ingestion.schemas import (
    DocumentProcessingResult,
    DocumentStatus,
    PageContent,
    SheetContent,
)
from src.validation.schemas import (
    RecordValidationResult,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)


class SQLAlchemyRepository:
    """Production database repository replacing in-memory state with PostgreSQL / SQLite.

    Provides full CRUD operations for:
      - Raw documents, OCR pages, and spreadsheet sheets
      - Structured extracted invoice records
      - Financial validation results & mathematical diagnostics
      - Reviewer audit mutation logs
      - Text embeddings & hybrid keyword + semantic search
      - Background job telemetry and retry tracking
    """

    def __init__(
        self,
        engine: Optional[AsyncEngine] = None,
        search_engine: Optional[HybridSearchEngine] = None,
    ) -> None:
        self.engine = engine or get_engine()
        self.session_factory = get_session_factory(self.engine)
        self.search_engine = search_engine or HybridSearchEngine()
        self.embedder = get_default_embedder()

    # =========================================================================
    # Job Persistence
    # =========================================================================

    async def save_job(
        self,
        job: JobStatusResponse,
        file_path: Optional[Path] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> JobStatusResponse:
        """Register or update a processing job in the database."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = select(ProcessingJobModel).where(ProcessingJobModel.job_id == job.job_id)
                res = await session.execute(stmt)
                existing = res.scalar_one_or_none()

                if existing:
                    existing.stage = job.stage.value
                    existing.progress_percent = job.progress_percent
                    existing.pages_processed = job.pages_processed
                    existing.total_pages = job.total_pages
                    existing.document_id = job.document_id
                    existing.error_message = job.error_message
                    existing.processing_time_ms = job.processing_time_ms
                    existing.retry_count = job.retry_count
                    existing.updated_at = datetime.now(timezone.utc)
                    if file_path:
                        existing.file_path = str(file_path)
                    if options:
                        existing.options_json = options
                else:
                    new_job = ProcessingJobModel(
                        job_id=job.job_id,
                        batch_id=job.batch_id,
                        document_id=job.document_id,
                        file_name=job.file_name,
                        file_type=job.file_type,
                        stage=job.stage.value,
                        progress_percent=job.progress_percent,
                        pages_processed=job.pages_processed,
                        total_pages=job.total_pages,
                        file_path=str(file_path) if file_path else None,
                        options_json=options or {},
                        error_message=job.error_message,
                        retry_count=job.retry_count,
                        processing_time_ms=job.processing_time_ms,
                        created_at=job.created_at,
                        updated_at=job.updated_at,
                    )
                    session.add(new_job)

            return job.model_copy()

    async def get_job(self, job_id: str) -> Optional[JobStatusResponse]:
        """Fetch job telemetry by ID."""
        async with self.session_factory() as session:
            stmt = select(ProcessingJobModel).where(ProcessingJobModel.job_id == job_id)
            res = await session.execute(stmt)
            model = res.scalar_one_or_none()
            if not model:
                return None

            return JobStatusResponse(
                job_id=model.job_id,
                batch_id=model.batch_id,
                document_id=model.document_id,
                file_name=model.file_name,
                file_type=model.file_type,
                stage=JobStage(model.stage),
                progress_percent=model.progress_percent,
                pages_processed=model.pages_processed,
                total_pages=model.total_pages,
                created_at=model.created_at,
                updated_at=model.updated_at,
                processing_time_ms=model.processing_time_ms,
                error_message=model.error_message,
                retry_count=model.retry_count,
            )

    async def get_job_file_path(self, job_id: str) -> Optional[Path]:
        """Retrieve stored file path for a job."""
        async with self.session_factory() as session:
            stmt = select(ProcessingJobModel.file_path).where(ProcessingJobModel.job_id == job_id)
            res = await session.execute(stmt)
            p = res.scalar_one_or_none()
            return Path(p) if p else None

    async def get_job_options(self, job_id: str) -> Dict[str, Any]:
        """Retrieve execution parameters for a job."""
        async with self.session_factory() as session:
            stmt = select(ProcessingJobModel.options_json).where(ProcessingJobModel.job_id == job_id)
            res = await session.execute(stmt)
            opts = res.scalar_one_or_none()
            return opts or {}

    async def update_job_stage(
        self,
        job_id: str,
        stage: JobStage,
        progress_percent: Optional[int] = None,
        pages_processed: Optional[int] = None,
        total_pages: Optional[int] = None,
        document_id: Optional[str] = None,
        error_message: Optional[str] = None,
        processing_time_ms: Optional[float] = None,
    ) -> Optional[JobStatusResponse]:
        """Atomically update a job's progress stage."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = select(ProcessingJobModel).where(ProcessingJobModel.job_id == job_id)
                res = await session.execute(stmt)
                model = res.scalar_one_or_none()
                if not model:
                    return None

                model.stage = stage.value
                model.updated_at = datetime.now(timezone.utc)
                if progress_percent is not None:
                    model.progress_percent = max(0, min(100, progress_percent))
                if pages_processed is not None:
                    model.pages_processed = pages_processed
                if total_pages is not None:
                    model.total_pages = total_pages
                if document_id is not None:
                    model.document_id = document_id
                if error_message is not None:
                    model.error_message = error_message
                if processing_time_ms is not None:
                    model.processing_time_ms = processing_time_ms

            return await self.get_job(job_id)

    async def list_jobs(
        self,
        batch_id: Optional[str] = None,
        stage: Optional[JobStage] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> JobsListResponse:
        """List jobs with pagination and summary by stage."""
        async with self.session_factory() as session:
            # Stage summary query
            summary: Dict[str, int] = {st.value: 0 for st in JobStage}
            stmt_summary = select(ProcessingJobModel.stage, func.count(ProcessingJobModel.job_id)).group_by(ProcessingJobModel.stage)
            sum_res = await session.execute(stmt_summary)
            for st, cnt in sum_res.all():
                if st in summary:
                    summary[st] = cnt

            # Filtered jobs query
            query = select(ProcessingJobModel)
            if batch_id:
                query = query.where(ProcessingJobModel.batch_id == batch_id)
            if stage:
                query = query.where(ProcessingJobModel.stage == stage.value)

            count_stmt = select(func.count()).select_from(query.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0

            query = query.order_by(ProcessingJobModel.created_at.desc()).offset(skip).limit(limit)
            models = (await session.execute(query)).scalars().all()

            jobs_list = [
                JobStatusResponse(
                    job_id=m.job_id,
                    batch_id=m.batch_id,
                    document_id=m.document_id,
                    file_name=m.file_name,
                    file_type=m.file_type,
                    stage=JobStage(m.stage),
                    progress_percent=m.progress_percent,
                    pages_processed=m.pages_processed,
                    total_pages=m.total_pages,
                    created_at=m.created_at,
                    updated_at=m.updated_at,
                    processing_time_ms=m.processing_time_ms,
                    error_message=m.error_message,
                    retry_count=m.retry_count,
                )
                for m in models
            ]

            return JobsListResponse(
                total=total,
                summary_by_stage=summary,
                jobs=jobs_list,
            )

    async def get_batch_jobs(self, batch_id: str) -> List[JobStatusResponse]:
        """Fetch all jobs for a specific batch ID."""
        async with self.session_factory() as session:
            stmt = select(ProcessingJobModel).where(ProcessingJobModel.batch_id == batch_id).order_by(ProcessingJobModel.created_at.asc())
            models = (await session.execute(stmt)).scalars().all()
            return [
                JobStatusResponse(
                    job_id=m.job_id,
                    batch_id=m.batch_id,
                    document_id=m.document_id,
                    file_name=m.file_name,
                    file_type=m.file_type,
                    stage=JobStage(m.stage),
                    progress_percent=m.progress_percent,
                    pages_processed=m.pages_processed,
                    total_pages=m.total_pages,
                    created_at=m.created_at,
                    updated_at=m.updated_at,
                    processing_time_ms=m.processing_time_ms,
                    error_message=m.error_message,
                    retry_count=m.retry_count,
                )
                for m in models
            ]

    # =========================================================================
    # Document, Record, and Validation Persistence
    # =========================================================================

    async def save_document_results(
        self,
        document_id: str,
        file_name: str,
        file_type: str,
        ingestion: Optional[DocumentProcessingResult] = None,
        extraction: Optional[DocumentExtractionResponse] = None,
        validations: Optional[List[RecordValidationResult]] = None,
    ) -> DocumentResultsResponse:
        """Atomically persist complete document intelligence outputs across normalized tables."""
        async with self.session_factory() as session:
            async with session.begin():
                full_text = ingestion.full_merged_text if ingestion else ""
                file_hash = ingestion.file_hash if ingestion else f"hash_{document_id}"
                status_str = ingestion.status.value if ingestion else "SUCCESS"
                total_units = ingestion.total_pages_or_sheets if ingestion else 0
                err = ingestion.error_details if ingestion else None
                proc_time = ingestion.processing_time_ms if ingestion else None

                # 1. Upsert DocumentModel
                stmt = select(DocumentModel).where(DocumentModel.id == document_id)
                res = await session.execute(stmt)
                doc_model = res.scalar_one_or_none()

                if not doc_model and file_hash:
                    stmt_h = select(DocumentModel).where(DocumentModel.file_hash == file_hash)
                    res_h = await session.execute(stmt_h)
                    doc_model = res_h.scalar_one_or_none()

                if doc_model:
                    document_id = doc_model.id
                    doc_model.file_name = file_name
                    doc_model.file_type = file_type
                    doc_model.file_hash = file_hash
                    doc_model.total_pages_or_sheets = total_units
                    doc_model.status = status_str
                    doc_model.full_merged_text = full_text
                    doc_model.error_details = err
                    doc_model.processing_time_ms = proc_time
                    doc_model.updated_at = datetime.now(timezone.utc)
                else:
                    doc_model = DocumentModel(
                        id=document_id,
                        file_name=file_name,
                        file_type=file_type,
                        file_hash=file_hash,
                        total_pages_or_sheets=total_units,
                        status=status_str,
                        full_merged_text=full_text,
                        error_details=err,
                        processing_time_ms=proc_time,
                    )
                    session.add(doc_model)

                # 2. Persist Pages
                if ingestion and ingestion.pages:
                    # Clear existing pages if updating
                    await session.execute(delete(DocumentPageModel).where(DocumentPageModel.document_id == document_id))
                    for p in ingestion.pages:
                        page_m = DocumentPageModel(
                            document_id=document_id,
                            page_number=p.page_number,
                            raw_text=p.raw_text,
                            extraction_method=p.extraction_method,
                            confidence_score=p.confidence_score,
                            tables_json=p.tables or [],
                            warnings=p.warnings or [],
                        )
                        session.add(page_m)

                # 3. Persist Sheets
                if ingestion and ingestion.sheets:
                    await session.execute(delete(DocumentSheetModel).where(DocumentSheetModel.document_id == document_id))
                    for sh in ingestion.sheets:
                        sheet_m = DocumentSheetModel(
                            document_id=document_id,
                            sheet_name=sh.sheet_name,
                            row_count=sh.row_count,
                            column_headers=sh.column_headers or [],
                            records_json=sh.records or [],
                            markdown_preview=sh.markdown_preview,
                            summary_stats=sh.summary_stats or {},
                        )
                        session.add(sheet_m)

                # 4. Generate & Persist Vector Embeddings
                if full_text:
                    await session.execute(delete(DocumentEmbeddingModel).where(DocumentEmbeddingModel.document_id == document_id))
                    # Chunk full text into ~1000 character windows
                    chunks = [full_text[i : i + 1000] for i in range(0, len(full_text), 800)]
                    for idx, chunk in enumerate(chunks[:10]):  # Cap initial embeddings
                        vec = self.embedder.embed_text(chunk)
                        emb_m = DocumentEmbeddingModel(
                            document_id=document_id,
                            chunk_index=idx,
                            chunk_text=chunk,
                            embedding=vec,
                            embedding_dim=len(vec),
                            metadata_json={"file_name": file_name, "chunk": idx},
                        )
                        session.add(emb_m)

                # 5. Persist Extracted Records
                if extraction and extraction.records:
                    # Clean up prior records and validations for this document to ensure idempotency on retries
                    prior_records = await session.execute(
                        select(ExtractedRecordModel.record_id).where(ExtractedRecordModel.document_id == document_id)
                    )
                    prior_rids = prior_records.scalars().all()
                    if prior_rids:
                        await session.execute(
                            delete(ValidationResultModel).where(ValidationResultModel.record_id.in_(prior_rids))
                        )
                        await session.execute(
                            delete(ExtractedRecordModel).where(ExtractedRecordModel.document_id == document_id)
                        )

                    for rec in extraction.records:
                        r_stmt = select(ExtractedRecordModel).where(ExtractedRecordModel.record_id == rec.record_id)
                        r_res = await session.execute(r_stmt)
                        existing_r = r_res.scalar_one_or_none()

                        d_val = None
                        if rec.invoice_date:
                            try:
                                d_val = datetime.strptime(rec.invoice_date, "%Y-%m-%d").date()
                            except ValueError:
                                pass

                        line_items_dump = [item.model_dump() for item in rec.line_items]

                        if existing_r:
                            existing_r.invoice_number = rec.invoice_number
                            existing_r.invoice_date = d_val
                            existing_r.supplier_name = rec.supplier_name
                            existing_r.customer_name = rec.customer_name
                            existing_r.subtotal = rec.subtotal
                            existing_r.tax = rec.tax
                            existing_r.total = rec.total
                            existing_r.currency = rec.currency
                            existing_r.line_items = line_items_dump
                            existing_r.field_confidences = rec.field_confidences
                            existing_r.uncertain_fields = rec.uncertain_fields
                            existing_r.extraction_warnings = rec.extraction_warnings
                            existing_r.updated_at = datetime.now(timezone.utc)
                        else:
                            new_r = ExtractedRecordModel(
                                document_id=document_id,
                                record_id=rec.record_id,
                                page_range_or_sheet=rec.page_range_or_sheet,
                                invoice_number=rec.invoice_number,
                                invoice_date=d_val,
                                supplier_name=rec.supplier_name,
                                customer_name=rec.customer_name,
                                subtotal=rec.subtotal,
                                tax=rec.tax,
                                total=rec.total,
                                currency=rec.currency,
                                line_items=line_items_dump,
                                field_confidences=rec.field_confidences,
                                uncertain_fields=rec.uncertain_fields,
                                extraction_warnings=rec.extraction_warnings,
                            )
                            session.add(new_r)

                # 6. Persist Validations
                if validations:
                    for v in validations:
                        v_stmt = select(ValidationResultModel).where(ValidationResultModel.record_id == v.record_id)
                        v_res = await session.execute(v_stmt)
                        existing_v = v_res.scalar_one_or_none()

                        issues_dump = [i.model_dump() for i in v.issues]
                        cross_dump = v.cross_verification.model_dump() if v.cross_verification else None

                        if existing_v:
                            existing_v.is_valid = v.is_valid
                            existing_v.validation_status = v.validation_status
                            existing_v.requires_manual_review = v.requires_manual_review
                            existing_v.issues = issues_dump
                            existing_v.cross_verification_json = cross_dump
                            existing_v.computed_metrics = v.computed_metrics
                        else:
                            new_v = ValidationResultModel(
                                record_id=v.record_id,
                                is_valid=v.is_valid,
                                validation_status=v.validation_status,
                                requires_manual_review=v.requires_manual_review,
                                issues=issues_dump,
                                cross_verification_json=cross_dump,
                                computed_metrics=v.computed_metrics,
                            )
                            session.add(new_v)

        return await self.get_document_results(document_id)  # type: ignore

    async def get_document_results(self, document_id: str) -> Optional[DocumentResultsResponse]:
        """Fetch full document intelligence hierarchy."""
        async with self.session_factory() as session:
            stmt = select(DocumentModel).where(DocumentModel.id == document_id)
            res = await session.execute(stmt)
            doc_model = res.scalar_one_or_none()
            if not doc_model:
                return None

            # Fetch pages
            p_stmt = select(DocumentPageModel).where(DocumentPageModel.document_id == document_id).order_by(DocumentPageModel.page_number)
            p_res = await session.execute(p_stmt)
            pages = [
                PageContent(
                    page_number=pm.page_number,
                    raw_text=pm.raw_text,
                    extraction_method=pm.extraction_method, # type: ignore
                    confidence_score=pm.confidence_score,
                    tables=pm.tables_json,
                    warnings=pm.warnings,
                )
                for pm in p_res.scalars().all()
            ]

            # Fetch sheets
            s_stmt = select(DocumentSheetModel).where(DocumentSheetModel.document_id == document_id)
            s_res = await session.execute(s_stmt)
            sheets = [
                SheetContent(
                    sheet_name=sm.sheet_name,
                    row_count=sm.row_count,
                    column_headers=sm.column_headers,
                    records=sm.records_json,
                    markdown_preview=sm.markdown_preview or "",
                    summary_stats=sm.summary_stats,
                )
                for sm in s_res.scalars().all()
            ]

            ingestion_res = DocumentProcessingResult(
                document_id=doc_model.id,
                file_name=doc_model.file_name,
                file_type=doc_model.file_type,
                file_hash=doc_model.file_hash,
                status=DocumentStatus(doc_model.status),
                total_pages_or_sheets=doc_model.total_pages_or_sheets,
                pages=pages,
                sheets=sheets,
                full_merged_text=doc_model.full_merged_text or "",
                processing_time_ms=doc_model.processing_time_ms or 0.0,
                error_details=doc_model.error_details,
            )

            # Fetch records
            r_stmt = select(ExtractedRecordModel).where(ExtractedRecordModel.document_id == document_id)
            r_res = await session.execute(r_stmt)
            records = []
            validations = []
            audits = []

            for rm in r_res.scalars().all():
                rec = InvoiceExtractionRecord(
                    record_id=rm.record_id,
                    source_document_id=rm.document_id,
                    page_range_or_sheet=rm.page_range_or_sheet or "",
                    invoice_number=rm.invoice_number,
                    invoice_date=rm.invoice_date.isoformat() if rm.invoice_date else None,
                    supplier_name=rm.supplier_name,
                    customer_name=rm.customer_name,
                    subtotal=float(rm.subtotal) if rm.subtotal is not None else None,
                    tax=float(rm.tax) if rm.tax is not None else None,
                    total=float(rm.total) if rm.total is not None else None,
                    currency=rm.currency,
                    line_items=[LineItem(**item) for item in (rm.line_items or [])],
                    field_confidences=rm.field_confidences or {},
                    uncertain_fields=rm.uncertain_fields or [],
                    extraction_warnings=rm.extraction_warnings or [],
                )
                records.append(rec)

                # Validation
                v_stmt = select(ValidationResultModel).where(ValidationResultModel.record_id == rm.record_id)
                v_res = await session.execute(v_stmt)
                vm = v_res.scalar_one_or_none()
                if vm:
                    validations.append(
                        RecordValidationResult(
                            record_id=rm.record_id,
                            source_document_id=rm.document_id,
                            invoice_number=rm.invoice_number,
                            is_valid=vm.is_valid,
                            validation_status=vm.validation_status, # type: ignore
                            requires_manual_review=vm.requires_manual_review,
                            issues=[ValidationIssue(**iss) for iss in (vm.issues or [])],
                            cross_verification=vm.cross_verification_json, # type: ignore
                            computed_metrics=vm.computed_metrics or {},
                        )
                    )

                # Audit logs
                a_stmt = select(AuditLogModel).where(AuditLogModel.record_id == rm.record_id).order_by(AuditLogModel.timestamp)
                a_res = await session.execute(a_stmt)
                for am in a_res.scalars().all():
                    audits.append(
                        AuditLogEntry(
                            entry_id=am.id,
                            record_id=am.record_id,
                            timestamp=am.timestamp,
                            actor=am.actor,
                            field_changed=am.field_changed,
                            old_value=am.old_value,
                            new_value=am.new_value,
                            notes=am.notes,
                        )
                    )

            extraction_res = DocumentExtractionResponse(
                document_id=doc_model.id,
                file_name=doc_model.file_name,
                total_invoices_detected=len(records),
                records=records,
                model_used="hybrid",
                processing_time_ms=doc_model.processing_time_ms or 0.0,
            )

            return DocumentResultsResponse(
                document_id=doc_model.id,
                file_name=doc_model.file_name,
                file_type=doc_model.file_type,
                ingestion=ingestion_res,
                extraction=extraction_res,
                validation=validations,
                audit_trail=audits,
            )

    async def get_record(self, record_id: str) -> Optional[RecordDetailResponse]:
        """Retrieve single invoice record, its validation results, and audit history."""
        async with self.session_factory() as session:
            stmt = select(ExtractedRecordModel).where(ExtractedRecordModel.record_id == record_id)
            res = await session.execute(stmt)
            rm = res.scalar_one_or_none()
            if not rm:
                return None

            rec = InvoiceExtractionRecord(
                record_id=rm.record_id,
                source_document_id=rm.document_id,
                page_range_or_sheet=rm.page_range_or_sheet or "",
                invoice_number=rm.invoice_number,
                invoice_date=rm.invoice_date.isoformat() if rm.invoice_date else None,
                supplier_name=rm.supplier_name,
                customer_name=rm.customer_name,
                subtotal=float(rm.subtotal) if rm.subtotal is not None else None,
                tax=float(rm.tax) if rm.tax is not None else None,
                total=float(rm.total) if rm.total is not None else None,
                currency=rm.currency,
                line_items=[LineItem(**item) for item in (rm.line_items or [])],
                field_confidences=rm.field_confidences or {},
                uncertain_fields=rm.uncertain_fields or [],
                extraction_warnings=rm.extraction_warnings or [],
            )

            v_stmt = select(ValidationResultModel).where(ValidationResultModel.record_id == record_id)
            vm = (await session.execute(v_stmt)).scalar_one_or_none()
            if vm:
                val = RecordValidationResult(
                    record_id=rm.record_id,
                    source_document_id=rm.document_id,
                    invoice_number=rm.invoice_number,
                    is_valid=vm.is_valid,
                    validation_status=vm.validation_status, # type: ignore
                    requires_manual_review=vm.requires_manual_review,
                    issues=[ValidationIssue(**iss) for iss in (vm.issues or [])],
                    cross_verification=vm.cross_verification_json, # type: ignore
                    computed_metrics=vm.computed_metrics or {},
                )
            else:
                val = RecordValidationResult(
                    record_id=rm.record_id,
                    source_document_id=rm.document_id,
                    is_valid=True,
                    validation_status="AUTO_VERIFIED",
                    requires_manual_review=False,
                    issues=[],
                )

            a_stmt = select(AuditLogModel).where(AuditLogModel.record_id == record_id).order_by(AuditLogModel.timestamp)
            audits = [
                AuditLogEntry(
                    entry_id=am.id,
                    record_id=am.record_id,
                    timestamp=am.timestamp,
                    actor=am.actor,
                    field_changed=am.field_changed,
                    old_value=am.old_value,
                    new_value=am.new_value,
                    notes=am.notes,
                )
                for am in (await session.execute(a_stmt)).scalars().all()
            ]

            return RecordDetailResponse(
                record=rec,
                validation=val,
                audit_trail=audits,
            )

    async def update_record(
        self,
        record_id: str,
        updated_record: InvoiceExtractionRecord,
        updated_validation: RecordValidationResult,
        audit_entries: List[AuditLogEntry],
    ) -> Optional[RecordDetailResponse]:
        """Persist manual reviewer corrections, audit trail entries, and validation re-evaluation."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = select(ExtractedRecordModel).where(ExtractedRecordModel.record_id == record_id)
                rm = (await session.execute(stmt)).scalar_one_or_none()
                if not rm:
                    return None

                d_val = None
                if updated_record.invoice_date:
                    try:
                        d_val = datetime.strptime(updated_record.invoice_date, "%Y-%m-%d").date()
                    except ValueError:
                        pass

                rm.invoice_number = updated_record.invoice_number
                rm.invoice_date = d_val
                rm.supplier_name = updated_record.supplier_name
                rm.customer_name = updated_record.customer_name
                rm.subtotal = updated_record.subtotal
                rm.tax = updated_record.tax
                rm.total = updated_record.total
                rm.currency = updated_record.currency
                rm.line_items = [item.model_dump() for item in updated_record.line_items]
                rm.updated_at = datetime.now(timezone.utc)

                # Upsert validation
                v_stmt = select(ValidationResultModel).where(ValidationResultModel.record_id == record_id)
                vm = (await session.execute(v_stmt)).scalar_one_or_none()
                issues_dump = [i.model_dump() for i in updated_validation.issues]
                cross_dump = updated_validation.cross_verification.model_dump() if updated_validation.cross_verification else None

                if vm:
                    vm.is_valid = updated_validation.is_valid
                    vm.validation_status = updated_validation.validation_status
                    vm.requires_manual_review = updated_validation.requires_manual_review
                    vm.issues = issues_dump
                    vm.cross_verification_json = cross_dump
                    vm.computed_metrics = updated_validation.computed_metrics
                else:
                    vm = ValidationResultModel(
                        record_id=record_id,
                        is_valid=updated_validation.is_valid,
                        validation_status=updated_validation.validation_status,
                        requires_manual_review=updated_validation.requires_manual_review,
                        issues=issues_dump,
                        cross_verification_json=cross_dump,
                        computed_metrics=updated_validation.computed_metrics,
                    )
                    session.add(vm)

                # Append audit entries with cryptographic hash chaining (SEC-04 / SEC-11)
                last_log_stmt = (
                    select(AuditLogModel)
                    .where(AuditLogModel.record_id == record_id)
                    .order_by(AuditLogModel.timestamp.desc())
                    .limit(1)
                )
                last_log = (await session.execute(last_log_stmt)).scalar_one_or_none()
                current_prev_hash = last_log.record_hash if (last_log and last_log.record_hash) else ("0" * 64)

                for entry in audit_entries:
                    old_str = str(entry.old_value) if entry.old_value is not None else ""
                    new_str = str(entry.new_value) if entry.new_value is not None else ""
                    raw_payload = f"{current_prev_hash}:{entry.entry_id}:{record_id}:{entry.actor}:{entry.field_changed}:{old_str}:{new_str}"
                    entry_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

                    am = AuditLogModel(
                        id=entry.entry_id,
                        record_id=record_id,
                        actor=entry.actor,
                        field_changed=entry.field_changed,
                        old_value=str(entry.old_value) if entry.old_value is not None else None,
                        new_value=str(entry.new_value) if entry.new_value is not None else None,
                        notes=entry.notes,
                        prev_hash=current_prev_hash,
                        record_hash=entry_hash,
                        timestamp=entry.timestamp,
                    )
                    session.add(am)
                    current_prev_hash = entry_hash

        return await self.get_record(record_id)

    async def verify_audit_trail_integrity(self, record_id: str) -> Tuple[bool, Optional[str]]:
        """Cryptographically verify the hash chain integrity for an invoice record's audit log.

        Returns:
            Tuple of (is_valid, failure_reason)
        """
        async with self.session_factory() as session:
            stmt = select(AuditLogModel).where(AuditLogModel.record_id == record_id).order_by(AuditLogModel.timestamp.asc())
            logs = (await session.execute(stmt)).scalars().all()
            if not logs:
                return True, None

            expected_prev = "0" * 64
            for idx, log in enumerate(logs):
                if log.prev_hash != expected_prev:
                    return False, f"Broken chain at index {idx}: expected prev_hash {expected_prev}, found {log.prev_hash}"
                old_str = log.old_value or ""
                new_str = log.new_value or ""
                expected_hash = hashlib.sha256(
                    f"{log.prev_hash}:{log.id}:{record_id}:{log.actor}:{log.field_changed}:{old_str}:{new_str}".encode("utf-8")
                ).hexdigest()
                if log.record_hash != expected_hash:
                    return False, f"Tampered record at index {idx} ({log.id}): hash mismatch {log.record_hash} != {expected_hash}"
                expected_prev = log.record_hash

            return True, None

    async def get_all_records(self) -> List[InvoiceExtractionRecord]:
        """Fetch all active extracted invoice records across documents."""
        async with self.session_factory() as session:
            stmt = select(ExtractedRecordModel)
            models = (await session.execute(stmt)).scalars().all()
            return [
                InvoiceExtractionRecord(
                    record_id=rm.record_id,
                    source_document_id=rm.document_id,
                    page_range_or_sheet=rm.page_range_or_sheet or "",
                    invoice_number=rm.invoice_number,
                    invoice_date=rm.invoice_date.isoformat() if rm.invoice_date else None,
                    supplier_name=rm.supplier_name,
                    customer_name=rm.customer_name,
                    subtotal=float(rm.subtotal) if rm.subtotal is not None else None,
                    tax=float(rm.tax) if rm.tax is not None else None,
                    total=float(rm.total) if rm.total is not None else None,
                    currency=rm.currency,
                    line_items=[LineItem(**item) for item in (rm.line_items or [])],
                    field_confidences=rm.field_confidences or {},
                    uncertain_fields=rm.uncertain_fields or [],
                    extraction_warnings=rm.extraction_warnings or [],
                )
                for rm in models
            ]

    async def search_records(self, params: SearchQueryParams) -> SearchResponse:
        """Execute hybrid keyword and vector search via HybridSearchEngine."""
        async with self.session_factory() as session:
            return await self.search_engine.search(session, params)

    async def clear_all(self) -> None:
        """Wipe database state (for test isolation)."""
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(delete(AuditLogModel))
                await session.execute(delete(ValidationResultModel))
                await session.execute(delete(ExtractedRecordModel))
                await session.execute(delete(DocumentEmbeddingModel))
                await session.execute(delete(DocumentSheetModel))
                await session.execute(delete(DocumentPageModel))
                await session.execute(delete(DocumentModel))
                await session.execute(delete(ProcessingJobModel))
