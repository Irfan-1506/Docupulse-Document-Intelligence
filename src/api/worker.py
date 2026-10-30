"""Background pipeline worker orchestrating Ingestion -> Extraction -> Validation stages."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.schemas import JobStage, JobStatusResponse
from src.api.store import DocumentStore
from src.extraction.engine import AIExtractionEngine
from src.extraction.schemas import ExtractionConfig, ExtractionProvider
from src.ingestion.router import DocumentIngestionEngine
from src.ingestion.schemas import DocumentProcessingResult, DocumentStatus, IngestionConfig
from src.validation.engine import ValidationEngine
from src.validation.schemas import RecordValidationResult, ValidationConfig

logger = logging.getLogger(__name__)


async def process_document_job(
    job_id: str,
    file_path: Path,
    options: Dict[str, Any],
    store: DocumentStore,
) -> JobStatusResponse:
    """Execute complete end-to-end background ingestion, extraction, and validation pipeline.

    Progress telemetry:
      - 10%: INGESTING (integrity check, parsing digital text / sheets)
      - 35%: OCR_PROCESSING (rasterization, layout inspection)
      - 65%: AI_EXTRACTING (LLM structured extraction or heuristic fallback)
      - 90%: VALIDATING (arithmetic rules, deduplication, cross-verification)
      - 100%: COMPLETED / PARTIAL_SUCCESS / FAILED
    """
    t_start = time.perf_counter()
    logger.info(f"Starting processing for Job ID {job_id} on file {file_path.name}")

    try:
        # =====================================================================
        # Stage 1: INGESTING (10%)
        # =====================================================================
        store.update_job_stage(
            job_id=job_id,
            stage=JobStage.INGESTING,
            progress_percent=10,
        )

        # Build IngestionConfig based on options
        force_ocr = bool(options.get("force_ocr", False))
        ingest_cfg = IngestionConfig(
            ocr_dpi=300,
            force_ocr_on_all_pages=force_ocr,
        )
        ingestion_engine = DocumentIngestionEngine(config=ingest_cfg)

        # Execute ingestion (runs CPU/IO bound tasks in threadpool)
        loop = asyncio.get_running_loop()
        ingestion_res: DocumentProcessingResult = await loop.run_in_executor(
            None,
            lambda: ingestion_engine.process_file(file_path),
        )

        # Check for corruption or fatal ingestion failure
        original_filename = options.get("original_filename")
        if original_filename:
            ingestion_res.file_name = original_filename

        if ingestion_res.status in (DocumentStatus.FAILED_CORRUPTED, DocumentStatus.UNSUPPORTED_FORMAT):
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            error_msg = ingestion_res.error_details or f"File ingestion failed with status {ingestion_res.status.value}"
            logger.warning(f"Job {job_id} failed during ingestion: {error_msg}")

            store.save_document_results(
                document_id=ingestion_res.document_id,
                file_name=original_filename or ingestion_res.file_name,
                file_type=ingestion_res.file_type,
                ingestion=ingestion_res,
            )
            updated = store.update_job_stage(
                job_id=job_id,
                stage=JobStage.FAILED,
                progress_percent=100,
                document_id=ingestion_res.document_id,
                error_message=error_msg,
                processing_time_ms=round(elapsed_ms, 2),
            )
            return updated or store.get_job(job_id)  # type: ignore

        # =====================================================================
        # Stage 2: OCR_PROCESSING / LAYOUT INSPECTION (35%)
        # =====================================================================
        total_pages = ingestion_res.total_pages_or_sheets
        store.update_job_stage(
            job_id=job_id,
            stage=JobStage.OCR_PROCESSING,
            progress_percent=35,
            document_id=ingestion_res.document_id,
            pages_processed=total_pages,
            total_pages=total_pages,
        )

        # =====================================================================
        # Stage 3: AI_EXTRACTING (65%)
        # =====================================================================
        store.update_job_stage(
            job_id=job_id,
            stage=JobStage.AI_EXTRACTING,
            progress_percent=65,
        )

        provider_name = options.get("provider_override") or options.get("provider")
        provider = ExtractionProvider.HEURISTIC_FALLBACK
        if provider_name:
            try:
                provider = ExtractionProvider(provider_name)
            except ValueError:
                provider = ExtractionProvider.HEURISTIC_FALLBACK

        extraction_cfg = ExtractionConfig(provider=provider)
        extraction_engine = AIExtractionEngine(config=extraction_cfg)

        extraction_res = await extraction_engine.extract_document_async(ingestion_res)

        # =====================================================================
        # Stage 4: VALIDATING & CROSS-VERIFYING (90%)
        # =====================================================================
        store.update_job_stage(
            job_id=job_id,
            stage=JobStage.VALIDATING,
            progress_percent=90,
        )

        validation_engine = ValidationEngine(config=ValidationConfig())
        validations: List[RecordValidationResult] = []

        # Pull existing records from the store as candidates for duplicate detection
        candidate_pool = store.get_all_records()

        # If document has spreadsheet sheets, check if we can cross-verify against them
        reference_sheet = ingestion_res.sheets[0] if ingestion_res.sheets else None

        for rec in extraction_res.records:
            val_res = validation_engine.validate_record(
                record=rec,
                candidate_pool=candidate_pool,
                reference_ledger=reference_sheet,
            )
            validations.append(val_res)
            # Add to candidate pool for mutual deduplication within the same batch
            candidate_pool.append(rec)

        # =====================================================================
        # Stage 5: COMPLETED / PARTIAL_SUCCESS (100%)
        # =====================================================================
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        # Persist full results to document store
        effective_file_name = original_filename or ingestion_res.file_name
        store.save_document_results(
            document_id=ingestion_res.document_id,
            file_name=effective_file_name,
            file_type=ingestion_res.file_type,
            ingestion=ingestion_res,
            extraction=extraction_res,
            validations=validations,
        )

        final_stage = (
            JobStage.PARTIAL_SUCCESS
            if ingestion_res.status == DocumentStatus.PARTIAL_SUCCESS
            else JobStage.COMPLETED
        )

        updated = store.update_job_stage(
            job_id=job_id,
            stage=final_stage,
            progress_percent=100,
            pages_processed=total_pages,
            total_pages=total_pages,
            processing_time_ms=round(elapsed_ms, 2),
        )

        logger.info(
            f"Successfully processed Job ID {job_id} in {elapsed_ms:.2f}ms. "
            f"Status: {final_stage.value}, Invoices: {len(extraction_res.records)}"
        )
        return updated or store.get_job(job_id)  # type: ignore

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        err = f"Unhandled pipeline worker error: {str(exc)}"
        logger.exception(f"Job ID {job_id} failed: {err}")
        updated = store.update_job_stage(
            job_id=job_id,
            stage=JobStage.FAILED,
            progress_percent=100,
            error_message=err,
            processing_time_ms=round(elapsed_ms, 2),
        )
        return updated or store.get_job(job_id)  # type: ignore
