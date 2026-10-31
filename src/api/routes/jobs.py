"""Job telemetry, batch progress polling, and reprocessing endpoints."""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.queue import BaseJobQueue, get_job_queue
from src.api.schemas import (
    BatchStatusResponse,
    JobStage,
    JobStatusResponse,
    JobsListResponse,
    ReprocessJobRequest,
)
from src.api.store import DocumentStore, get_global_store
from src.auth.dependencies import UserContext, require_permission
from src.auth.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_store() -> DocumentStore:
    return get_global_store()


def get_queue(store: DocumentStore = Depends(get_store)) -> BaseJobQueue:
    return get_job_queue(store)


@router.get(
    "/{job_id}/status",
    response_model=JobStatusResponse,
    summary="Poll real-time progress and telemetry for a specific job",
)
def get_job_status(
    job_id: str,
    store: DocumentStore = Depends(get_store),
    current_user: UserContext = Depends(require_permission(Permission.DOCUMENT_READ)),
) -> JobStatusResponse:
    """Retrieve current processing stage, progress percentage, and diagnostics for a job."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )
    return job


@router.get(
    "/batches/{batch_id}/status",
    response_model=BatchStatusResponse,
    summary="Get aggregate progress across all documents in a batch",
)
def get_batch_status(
    batch_id: str,
    store: DocumentStore = Depends(get_store),
    current_user: UserContext = Depends(require_permission(Permission.DOCUMENT_READ)),
) -> BatchStatusResponse:
    """Retrieve batch-level progress summary including completed, in-progress, and failed jobs."""
    jobs = store.get_batch_jobs(batch_id)
    if not jobs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch with ID '{batch_id}' not found.",
        )

    completed = sum(1 for j in jobs if j.stage in (JobStage.COMPLETED, JobStage.PARTIAL_SUCCESS))
    failed = sum(1 for j in jobs if j.stage == JobStage.FAILED)
    in_progress = len(jobs) - completed - failed

    return BatchStatusResponse(
        batch_id=batch_id,
        total_jobs=len(jobs),
        completed_jobs=completed,
        failed_jobs=failed,
        in_progress_jobs=in_progress,
        jobs=jobs,
    )


@router.get(
    "",
    response_model=JobsListResponse,
    summary="List all jobs with dashboard stage summary",
)
def list_jobs(
    batch_id: Optional[str] = Query(None, description="Filter by batch ID"),
    stage: Optional[JobStage] = Query(None, description="Filter by current stage"),
    skip: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    store: DocumentStore = Depends(get_store),
    current_user: UserContext = Depends(require_permission(Permission.DOCUMENT_READ)),
) -> JobsListResponse:
    """List jobs filtered by batch or stage, with summary counts by stage."""
    return store.list_jobs(batch_id=batch_id, stage=stage, skip=skip, limit=limit)


@router.post(
    "/{job_id}/reprocess",
    response_model=JobStatusResponse,
    summary="Reprocess a specific job with optional parameter overrides",
)
async def reprocess_job(
    job_id: str,
    payload: Optional[ReprocessJobRequest] = None,
    store: DocumentStore = Depends(get_store),
    queue: BaseJobQueue = Depends(get_queue),
    current_user: UserContext = Depends(require_permission(Permission.DOCUMENT_REPROCESS)),
) -> JobStatusResponse:
    """Re-enqueue an existing job, resetting progress while preserving historical retry counts."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    file_path = store.get_job_file_path(job_id)
    if not file_path or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Original source file for job '{job_id}' is no longer accessible on disk.",
        )

    # Merge execution options
    existing_options = store.get_job_options(job_id)
    if payload:
        if payload.force_ocr:
            existing_options["force_ocr"] = True
        if payload.provider_override:
            existing_options["provider_override"] = payload.provider_override

    # Increment retry count and reset telemetry
    job.retry_count += 1
    job.stage = JobStage.QUEUED
    job.progress_percent = 0
    job.error_message = None
    job.processing_time_ms = None

    store.save_job(job=job, file_path=file_path, options=existing_options)
    await queue.enqueue(job_id=job_id, file_path=file_path, options=existing_options)

    return job


@router.post(
    "/reprocess-failed",
    response_model=List[JobStatusResponse],
    summary="Reprocess all failed jobs across the system",
)
async def reprocess_failed_jobs(
    store: DocumentStore = Depends(get_store),
    queue: BaseJobQueue = Depends(get_queue),
) -> List[JobStatusResponse]:
    """Identify and re-enqueue all jobs currently in FAILED status."""
    failed_jobs = store.list_jobs(stage=JobStage.FAILED, limit=500).jobs
    reprocessed: List[JobStatusResponse] = []

    for job in failed_jobs:
        file_path = store.get_job_file_path(job.job_id)
        if not file_path or not file_path.exists():
            continue

        options = store.get_job_options(job.job_id)
        job.retry_count += 1
        job.stage = JobStage.QUEUED
        job.progress_percent = 0
        job.error_message = None
        job.processing_time_ms = None

        store.save_job(job=job, file_path=file_path, options=options)
        await queue.enqueue(job_id=job.job_id, file_path=file_path, options=options)
        reprocessed.append(job)

    return reprocessed
