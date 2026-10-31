"""Background pipeline worker orchestrating Ingestion -> Extraction -> Validation stages."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.schemas import JobStage, JobStatusResponse
from src.api.store import DocumentStore, get_global_store
from src.core.logging import ErrorCategory, get_structured_logger, sanitize_sensitive_data
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
    ctx_logger = get_structured_logger(
        "src.api.worker",
        job_id=job_id,
        worker_id=options.get("worker_id"),
        attempt=options.get("attempt", 1),
    )
    ctx_logger.info(f"Starting processing for Job ID {job_id} on file {file_path.name}")

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
            document_id=ingestion_res.document_id,
            processing_time_ms=round(elapsed_ms, 2),
        )

        logger.info(
            f"Successfully processed Job ID {job_id} in {elapsed_ms:.2f}ms. "
            f"Status: {final_stage.value}, Invoices: {len(extraction_res.records)}"
        )
        return updated or store.get_job(job_id)  # type: ignore

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        sanitized_err = sanitize_sensitive_data(str(exc))
        err = f"[{ErrorCategory.WORKER_ERROR.value}] Unhandled pipeline worker error: {sanitized_err}"
        ctx_logger.exception(f"Job ID {job_id} failed: {err}")
        updated = store.update_job_stage(
            job_id=job_id,
            stage=JobStage.FAILED,
            progress_percent=100,
            error_message=err,
            processing_time_ms=round(elapsed_ms, 2),
        )
        return updated or store.get_job(job_id)  # type: ignore


# ===========================================================================
# Standalone Worker Daemon Service (Objective B)
# ===========================================================================

class WorkerDaemon:
    """Production standalone background worker process.

    Continuously consumes document tasks from the configured queue (RedisJobQueue
    or AsyncWorkerQueue), executes pipeline stages, reports telemetry, and handles
    signals for graceful zero-data-loss shutdown.
    """

    def __init__(
        self,
        store: Optional[DocumentStore] = None,
        redis_url: Optional[str] = None,
        concurrency: int = 4,
        worker_id: Optional[str] = None,
        queue: Optional[Any] = None,
    ) -> None:
        self.store = store or get_global_store()
        self.concurrency = max(1, concurrency)
        self.worker_id = worker_id or f"worker-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Import here to avoid circular dependencies
        from src.api.queue import AsyncWorkerQueue, BaseJobQueue, RedisJobQueue

        if queue is not None:
            self.queue: BaseJobQueue = queue
            logger.info("WorkerDaemon initialized with injected queue instance.")
        elif self.redis_url and "redis://" in self.redis_url:
            self.queue = RedisJobQueue(
                store=self.store,
                redis_url=self.redis_url,
                concurrency=self.concurrency,
                worker_id=self.worker_id,
            )
            logger.info(f"WorkerDaemon initialized with RedisJobQueue ({self.redis_url}).")
        else:
            self.queue = AsyncWorkerQueue(
                store=self.store,
                concurrency=self.concurrency,
            )
            logger.info("WorkerDaemon initialized with AsyncWorkerQueue (local mode).")

    async def start(self) -> None:
        """Start queue consumers without blocking (useful for embedded or test harnesses)."""
        self._running = True
        await self.queue.start()

    async def stop(self) -> None:
        """Stop queue consumers and wait for drainage."""
        self.trigger_shutdown()
        await self.queue.stop()

    async def run(self) -> None:
        """Start worker consumers and block until shutdown signal received."""
        self._running = True
        logger.info(
            f"=== [DocuPulse AP Worker] Starting Worker Daemon ===\n"
            f"Worker ID:    {self.worker_id}\n"
            f"Concurrency:  {self.concurrency}\n"
            f"Queue Engine: {self.queue.__class__.__name__}"
        )

        # Start queue consumers
        await self.queue.start()

        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info(f"[DocuPulse AP Worker] Shutting down worker {self.worker_id}...")
            await self.queue.stop()
            logger.info(f"[DocuPulse AP Worker] Worker {self.worker_id} cleanly stopped.")

    def trigger_shutdown(self) -> None:
        """Signal the worker to gracefully complete in-flight tasks and exit."""
        logger.info(f"[DocuPulse AP Worker] Received shutdown signal for {self.worker_id}.")
        self._running = False
        self._shutdown_event.set()


async def run_worker_cli(args: argparse.Namespace) -> None:
    """Async entrypoint for standalone worker command."""
    # Configure logging
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    daemon = WorkerDaemon(
        redis_url=args.redis_url,
        concurrency=args.concurrency,
        worker_id=args.worker_id,
    )

    # Register OS signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.trigger_shutdown)
        except NotImplementedError:
            # Signal handlers not implemented on Windows event loop for signal.add_signal_handler
            # Handled via KeyboardInterrupt in main
            pass

    try:
        await daemon.run()
    except (KeyboardInterrupt, SystemExit):
        daemon.trigger_shutdown()


def main() -> None:
    """CLI entrypoint for running: python -m src.api.worker"""
    parser = argparse.ArgumentParser(description="DocuPulse Enterprise AP Background Worker Daemon")
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL"),
        help="Redis connection URL (e.g. redis://localhost:6379/0)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("QUEUE_CONCURRENCY", "4")),
        help="Number of concurrent document processing consumers (default: 4)",
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("WORKER_ID"),
        help="Unique worker identifier",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level: DEBUG, INFO, WARNING, ERROR",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_worker_cli(args))
    except KeyboardInterrupt:
        print("\n[DocuPulse AP Worker] Stopped by user (SIGINT).")
        sys.exit(0)


if __name__ == "__main__":
    main()
