"""Document ingestion, multi-file upload, search, and cross-verification endpoints."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status

from src.api.queue import BaseJobQueue, QueueUnavailableError, get_job_queue
from src.api.schemas import (
    BatchUploadResponse,
    CrossVerifyRequest,
    DocumentResultsResponse,
    JobStage,
    JobStatusResponse,
    SearchQueryParams,
    SearchResponse,
)
from src.api.store import DocumentStore, get_global_store
from src.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


def get_store() -> DocumentStore:
    return get_global_store()


def get_queue(store: DocumentStore = Depends(get_store)) -> BaseJobQueue:
    return get_job_queue(store)


@router.post(
    "/upload",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload single or multiple heterogeneous financial documents",
)
async def upload_documents(
    files: List[UploadFile] = File(..., description="One or more financial files (PDF, JPG, PNG, TIFF, XLSX, XLS, CSV)"),
    force_ocr: bool = Form(False, description="Force OCR rasterization on all pages"),
    provider_override: Optional[str] = Form(None, description="Optional LLM provider override"),
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key", description="Optional client idempotency key"),
    store: DocumentStore = Depends(get_store),
    queue: BaseJobQueue = Depends(get_queue),
) -> BatchUploadResponse:
    """Accept multi-file upload batches, immediately return 202 Accepted, and enqueue background jobs."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided for upload.")

    batch_id = str(uuid.uuid4())
    queued_jobs: List[JobStatusResponse] = []

    for file_obj in files:
        file_name = file_obj.filename or "unknown_file"
        file_ext = Path(file_name).suffix.lower()
        unique_file_id = f"{uuid.uuid4()}{file_ext}"
        destination_path = store.upload_dir / unique_file_id

        # Write uploaded bytes to disk
        content = await file_obj.read()
        destination_path.write_bytes(content)

        job_id = str(uuid.uuid4())
        file_type = file_ext.lstrip(".").upper() or "UNKNOWN"

        job = JobStatusResponse(
            job_id=job_id,
            batch_id=batch_id,
            file_name=file_name,
            file_type=file_type,
            stage=JobStage.QUEUED,
            progress_percent=0,
            pages_processed=0,
            total_pages=0,
            idempotency_key=x_idempotency_key,
        )

        options = {
            "force_ocr": force_ocr,
            "provider_override": provider_override,
            "original_filename": file_name,
            "idempotency_key": x_idempotency_key,
        }

        # Save job in store and enqueue
        store.save_job(job=job, file_path=destination_path, options=options)
        try:
            await queue.enqueue(
                job_id=job_id,
                file_path=destination_path,
                options=options,
                idempotency_key=x_idempotency_key,
            )
        except QueueUnavailableError as exc:
            logger.error(f"Queue service unavailable for job {job_id}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Queue backend is unavailable: {exc}",
            )
        queued_jobs.append(job)

    return BatchUploadResponse(
        batch_id=batch_id,
        total_files=len(queued_jobs),
        jobs=queued_jobs,
        message=f"Successfully queued {len(queued_jobs)} document(s) for asynchronous ingestion.",
    )


@router.get(
    "/{document_id}/results",
    response_model=DocumentResultsResponse,
    summary="Retrieve full extraction and validation results for an ingested document",
)
def get_document_results(
    document_id: str,
    store: DocumentStore = Depends(get_store),
) -> DocumentResultsResponse:
    """Fetch complete document intelligence payload including ingestion, extraction, and validation."""
    results = store.get_document_results(document_id)
    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with ID '{document_id}' not found.",
        )
    return results


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search extracted financial records via GET query parameters",
)
def search_documents_get(
    query: Optional[str] = Query(None, description="Full-text search keyword"),
    supplier: Optional[str] = Query(None, description="Supplier name filter"),
    customer: Optional[str] = Query(None, description="Customer name filter"),
    invoice_number: Optional[str] = Query(None, description="Invoice number filter"),
    currency: Optional[str] = Query(None, description="Currency filter (e.g. USD)"),
    min_total: Optional[float] = Query(None, description="Minimum total amount"),
    max_total: Optional[float] = Query(None, description="Maximum total amount"),
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    validation_status: Optional[str] = Query(None, description="Validation status filter"),
    requires_manual_review: Optional[bool] = Query(None, description="Review flag filter"),
    file_type: Optional[str] = Query(None, description="Document file type filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    store: DocumentStore = Depends(get_store),
) -> SearchResponse:
    """Search and filter extracted invoice records using URL query parameters."""
    params = SearchQueryParams(
        query=query,
        supplier=supplier,
        customer=customer,
        invoice_number=invoice_number,
        currency=currency,
        min_total=min_total,
        max_total=max_total,
        date_from=date_from,
        date_to=date_to,
        validation_status=validation_status,
        requires_manual_review=requires_manual_review,
        file_type=file_type,
        page=page,
        page_size=page_size,
    )
    return store.search_records(params)


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search extracted financial records via POST request body",
)
def search_documents_post(
    params: SearchQueryParams,
    store: DocumentStore = Depends(get_store),
) -> SearchResponse:
    """Search and filter extracted invoice records using JSON request body."""
    return store.search_records(params)


@router.post(
    "/cross-verify",
    summary="Cross-verify extracted invoice documents against a reference PO or spreadsheet ledger",
)
def cross_verify_documents(
    payload: CrossVerifyRequest,
    store: DocumentStore = Depends(get_store),
):
    """Execute on-demand cross-verification between invoice records and a reference document."""
    ref_doc = store.get_document_results(payload.reference_document_id)
    if not ref_doc:
        raise HTTPException(
            status_code=404,
            detail=f"Reference document '{payload.reference_document_id}' not found.",
        )

    # Collect source records from invoice documents
    target_ids = [d for d in (payload.invoice_document_ids or []) if d and d != "all"]
    source_records = []
    if target_ids:
        for doc_id in target_ids:
            doc = store.get_document_results(doc_id)
            if doc and doc.extraction and doc.extraction.records:
                source_records.extend(doc.extraction.records)
    else:
        # Pull all records across store excluding reference document itself
        all_recs = store.get_all_records()
        source_records = [r for r in all_recs if r.source_document_id != payload.reference_document_id]

    if not source_records:
        raise HTTPException(
            status_code=400,
            detail="No extracted invoice records found in the specified invoice documents.",
        )

    validation_engine = ValidationEngine()

    # Determine reference: spreadsheet sheets or PO invoice records
    ref_ledger = None
    ref_pdfs = None

    if ref_doc.ingestion and ref_doc.ingestion.sheets:
        from src.extraction.chunker import SpreadsheetInvoiceMapper
        combined_rows = []
        for sheet in ref_doc.ingestion.sheets:
            if not sheet.records:
                continue
            role_map = SpreadsheetInvoiceMapper.match_column_roles(sheet.column_headers)
            for idx, r in enumerate(sheet.records):
                row_dict = {"row_id": f"{sheet.sheet_name or 'Sheet'}_Row_{idx+1}"}
                for k, v in r.items():
                    role = role_map.get(k)
                    if role:
                        row_dict[role] = v
                    else:
                        clean_k = str(k).strip().lower().replace(" ", "_")
                        row_dict[clean_k] = v
                combined_rows.append(row_dict)
        if combined_rows:
            ref_ledger = combined_rows
        elif ref_doc.ingestion.sheets:
            ref_ledger = ref_doc.ingestion.sheets[0]
    elif ref_doc.extraction and ref_doc.extraction.records:
        ref_pdfs = ref_doc.extraction.records

    report = validation_engine.validate_batch(
        records=source_records,
        reference_ledger=ref_ledger,
        reference_pdfs=ref_pdfs,
    )

    rec_map = {r.record_id: r for r in source_records}
    enriched_results = []
    for r in report.results:
        dumped = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        src_rec = rec_map.get(r.record_id)
        if src_rec:
            dumped["extracted_record"] = src_rec.model_dump() if hasattr(src_rec, "model_dump") else src_rec
            dumped["supplier_name"] = src_rec.supplier_name
            dumped["total"] = float(src_rec.total) if src_rec.total is not None else None
            dumped["subtotal"] = float(src_rec.subtotal) if src_rec.subtotal is not None else None
            dumped["tax"] = float(src_rec.tax) if src_rec.tax is not None else None
            dumped["invoice_date"] = str(src_rec.invoice_date) if src_rec.invoice_date is not None else None
            dumped["currency"] = src_rec.currency
        enriched_results.append(dumped)

    return {
        "reference_document_id": payload.reference_document_id,
        "total_evaluated": report.total_records_evaluated,
        "auto_verified_count": report.auto_verified_count,
        "manual_review_count": report.manual_review_count,
        "cross_verified_exact_matches": report.cross_verified_exact_matches,
        "cross_verified_variances": report.cross_verified_variances,
        "results": enriched_results,
    }
