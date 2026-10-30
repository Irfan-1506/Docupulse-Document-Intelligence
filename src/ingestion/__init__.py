"""Document ingestion engine components and orchestrator."""

from src.ingestion.schemas import (
    DocumentProcessingResult,
    DocumentStatus,
    ExtractionMethod,
    IngestionConfig,
    PageContent,
    SheetContent,
)

__all__ = [
    "DocumentProcessingResult",
    "DocumentStatus",
    "ExtractionMethod",
    "IngestionConfig",
    "PageContent",
    "SheetContent",
    "DocumentIngestionEngine",
]


def __getattr__(name: str):
    if name == "DocumentIngestionEngine":
        from src.ingestion.router import DocumentIngestionEngine
        return DocumentIngestionEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
