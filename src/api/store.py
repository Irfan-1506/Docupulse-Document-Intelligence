"""Thread-safe database-backed repository bridge connecting FastAPI with SQLAlchemy and pgvector."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

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
from src.db.repository import SQLAlchemyRepository
from src.db.session import init_db
from src.extraction.schemas import DocumentExtractionResponse, InvoiceExtractionRecord
from src.ingestion.schemas import DocumentProcessingResult
from src.validation.schemas import RecordValidationResult

logger = logging.getLogger(__name__)


class AsyncBridge:
    """Singleton background event loop ensuring thread-safe async-to-sync delegation.

    Prevents 'event loop already running' collisions across test clients,
    worker tasks, and multi-threaded request contexts.
    """
    _instance: Optional[AsyncBridge] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="db-async-bridge",
        )
        self._thread.start()
        # Initialize database tables on the bridge loop
        future = asyncio.run_coroutine_threadsafe(init_db(), self._loop)
        future.result(timeout=15.0)

    @classmethod
    def get_instance(cls) -> AsyncBridge:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


class DocumentStore:
    """Production database-backed repository interface.

    Persists all document intelligence states, telemetry, and audit logs
    into normalized PostgreSQL/SQLite tables with hybrid vector search.
    """

    def __init__(self, upload_dir: Optional[Path] = None) -> None:
        self.upload_dir = upload_dir or (Path.cwd() / "data" / "uploads")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.repo = SQLAlchemyRepository()
        self._bridge = AsyncBridge.get_instance()

    # =========================================================================
    # Job Management
    # =========================================================================

    def save_job(
        self,
        job: JobStatusResponse,
        file_path: Optional[Path] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> JobStatusResponse:
        """Register or overwrite a job record."""
        return self._bridge.run(self.repo.save_job(job, file_path, options))

    def get_job(self, job_id: str) -> Optional[JobStatusResponse]:
        """Fetch a job status response by ID."""
        return self._bridge.run(self.repo.get_job(job_id))

    def get_job_file_path(self, job_id: str) -> Optional[Path]:
        """Get the stored file path for a job."""
        return self._bridge.run(self.repo.get_job_file_path(job_id))

    def get_job_options(self, job_id: str) -> Dict[str, Any]:
        """Get execution options associated with a job."""
        return self._bridge.run(self.repo.get_job_options(job_id))

    def update_job_stage(
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
        """Update job stage and telemetry atomically."""
        return self._bridge.run(
            self.repo.update_job_stage(
                job_id=job_id,
                stage=stage,
                progress_percent=progress_percent,
                pages_processed=pages_processed,
                total_pages=total_pages,
                document_id=document_id,
                error_message=error_message,
                processing_time_ms=processing_time_ms,
            )
        )

    def list_jobs(
        self,
        batch_id: Optional[str] = None,
        stage: Optional[JobStage] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> JobsListResponse:
        """List jobs filtered by batch or stage, with summary counts."""
        return self._bridge.run(self.repo.list_jobs(batch_id=batch_id, stage=stage, skip=skip, limit=limit))

    def get_batch_jobs(self, batch_id: str) -> List[JobStatusResponse]:
        """Retrieve all jobs belonging to a batch."""
        return self._bridge.run(self.repo.get_batch_jobs(batch_id))

    # =========================================================================
    # Document & Record Persistence
    # =========================================================================

    def save_document_results(
        self,
        document_id: str,
        file_name: str,
        file_type: str,
        ingestion: Optional[DocumentProcessingResult] = None,
        extraction: Optional[DocumentExtractionResponse] = None,
        validations: Optional[List[RecordValidationResult]] = None,
    ) -> DocumentResultsResponse:
        """Persist full pipeline artifacts for an ingested document."""
        return self._bridge.run(
            self.repo.save_document_results(
                document_id=document_id,
                file_name=file_name,
                file_type=file_type,
                ingestion=ingestion,
                extraction=extraction,
                validations=validations,
            )
        )

    def get_document_results(self, document_id: str) -> Optional[DocumentResultsResponse]:
        """Retrieve complete document results."""
        return self._bridge.run(self.repo.get_document_results(document_id))

    def get_record(self, record_id: str) -> Optional[RecordDetailResponse]:
        """Fetch an individual invoice record with its validation and audit history."""
        return self._bridge.run(self.repo.get_record(record_id))

    def update_record(
        self,
        record_id: str,
        updated_record: InvoiceExtractionRecord,
        updated_validation: RecordValidationResult,
        audit_entries: List[AuditLogEntry],
    ) -> Optional[RecordDetailResponse]:
        """Update an existing record, replacing its extraction record and validation result."""
        return self._bridge.run(
            self.repo.update_record(
                record_id=record_id,
                updated_record=updated_record,
                updated_validation=updated_validation,
                audit_entries=audit_entries,
            )
        )

    def get_all_records(self) -> List[InvoiceExtractionRecord]:
        """Retrieve all active invoice records across all documents."""
        return self._bridge.run(self.repo.get_all_records())

    def verify_audit_trail_integrity(self, record_id: str) -> tuple[bool, Optional[str]]:
        """Cryptographically verify the hash chain integrity for an invoice record's audit log."""
        return self._bridge.run(self.repo.verify_audit_trail_integrity(record_id))

    # =========================================================================
    # Search & Filtering
    # =========================================================================

    def search_records(self, params: SearchQueryParams) -> SearchResponse:
        """Search and filter records via HybridSearchEngine."""
        return self._bridge.run(self.repo.search_records(params))

    def clear(self) -> None:
        """Reset repository state (useful for test isolation)."""
        self._bridge.run(self.repo.clear_all())


_global_store: Optional[DocumentStore] = None


def get_global_store() -> DocumentStore:
    """Retrieve or initialize the global singleton DocumentStore."""
    global _global_store
    if _global_store is None:
        _global_store = DocumentStore()
    return _global_store
