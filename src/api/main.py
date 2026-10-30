"""FastAPI application entrypoint, CORS configuration, lifecycle management, and health checks."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.queue import get_job_queue
from src.api.routes import documents, jobs, records
from src.api.store import get_global_store

# Configure standard logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("document_intelligence.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize background job queue on startup and drain workers on shutdown."""
    logger.info("Initializing Document Intelligence API...")
    store = get_global_store()
    queue = get_job_queue(store)
    await queue.start()
    logger.info("Background job queue started and ready.")

    yield

    logger.info("Shutting down background job queue workers...")
    await queue.stop()
    logger.info("Document Intelligence API shutdown complete.")


app = FastAPI(
    title="AI-Powered Document Intelligence Engine API",
    version="1.0.0",
    description=(
        "High-performance REST API for asynchronous multi-file document ingestion, "
        "LLM financial extraction, strict arithmetic validation, duplicate detection, "
        "and human-in-the-loop (HITL) review."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Middleware Configuration (React / Next.js Frontend)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request Latency & Performance Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time_ms = (time.perf_counter() - start_time) * 1000.0
    response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    return response


# ---------------------------------------------------------------------------
# Health & Telemetry Endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/api/v1/health",
    tags=["telemetry"],
    summary="Service health and queue telemetry",
)
@app.get(
    "/health",
    tags=["telemetry"],
    include_in_schema=False,
)
def health_check() -> Dict[str, object]:
    """Check API operational health, store statistics, and background queue status."""
    store = get_global_store()
    all_jobs = store.list_jobs(limit=1)
    return {
        "status": "healthy",
        "service": "document-intelligence-engine",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_jobs_tracked": all_jobs.total,
        "summary_by_stage": all_jobs.summary_by_stage,
    }


# ---------------------------------------------------------------------------
# Router Inclusions
# ---------------------------------------------------------------------------
app.include_router(documents.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(records.router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Static Web Dashboard Mount (DocuPulse Enterprise AP v4.2)
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_dashboard():
        """Serve the integrated DocuPulse single-page web dashboard."""
        return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
