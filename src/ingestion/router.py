"""Main orchestrator and CLI entrypoint for Document Intelligence Ingestion."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Union

from src.ingestion.ocr_processor import OCRProcessor
from src.ingestion.pdf_processor import PDFProcessor
from src.ingestion.schemas import (
    DocumentProcessingResult,
    DocumentStatus,
    IngestionConfig,
    PageContent,
    SheetContent,
)
from src.ingestion.spreadsheet_processor import SpreadsheetProcessor
from src.ingestion.validators import (
    build_error_result,
    compute_sha256,
    validate_file_integrity,
)

logger = logging.getLogger(__name__)


class DocumentIngestionEngine:
    """Enterprise document processing and ingestion engine.

    Orchestrates validation, hybrid PDF processing, OCR image restoration,
    and multi-sheet financial spreadsheet ingestion.
    """

    def __init__(self, config: Optional[IngestionConfig] = None) -> None:
        self.config = config or IngestionConfig()
        self.ocr_processor = OCRProcessor(config=self.config)
        self.pdf_processor = PDFProcessor(config=self.config, ocr_processor=self.ocr_processor)
        self.spreadsheet_processor = SpreadsheetProcessor(config=self.config)

    def process_file(self, file_path: Union[str, Path]) -> DocumentProcessingResult:
        """Process a single document file with complete corruption isolation."""
        start_time = time.perf_counter()
        path = Path(file_path).resolve()
        doc_id = str(uuid.uuid4())

        # Step 1: Pre-validation & Corruption Guard
        try:
            is_valid, val_status, detected_type, error_msg = validate_file_integrity(path)
            if not is_valid:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                logger.warning(f"File validation failed for {path.name}: {error_msg} (status={val_status})")
                return build_error_result(
                    file_path=path,
                    status=val_status,
                    error_msg=error_msg or "Validation failed",
                    file_type=detected_type,
                    processing_time_ms=elapsed_ms,
                )

            file_hash = compute_sha256(path)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Integrity check crashed on {path.name}: {exc}\n{traceback.format_exc()}")
            return build_error_result(
                file_path=path,
                status=DocumentStatus.FAILED_CORRUPTED,
                error_msg=f"Integrity check exception: {str(exc)}",
                file_type=path.suffix.lstrip("."),
                processing_time_ms=elapsed_ms,
            )

        # Step 2: Route to Specialized Processor with Exception Boundary
        pages: List[PageContent] = []
        sheets: List[SheetContent] = []
        full_merged_text_parts: List[str] = []
        has_warnings = False

        try:
            if detected_type == "pdf":
                pages = self.pdf_processor.process(path)
                for p in pages:
                    header = f"--- Page {p.page_number} [{p.extraction_method.value}] ---"
                    full_merged_text_parts.append(f"{header}\n{p.raw_text}")
                    if p.warnings or p.confidence_score < self.config.ocr_confidence_threshold:
                        has_warnings = True

            elif detected_type in ("png", "jpg", "tiff"):
                pages = self.ocr_processor.process_image_file(path)
                for p in pages:
                    header = f"--- Frame {p.page_number} [OCR: {p.confidence_score:.2f}] ---"
                    full_merged_text_parts.append(f"{header}\n{p.raw_text}")
                    if p.warnings or p.confidence_score < self.config.ocr_confidence_threshold:
                        has_warnings = True

            elif detected_type in ("xlsx", "xls", "csv"):
                sheets = self.spreadsheet_processor.process(path)
                for s in sheets:
                    full_merged_text_parts.append(f"### Sheet: {s.sheet_name}\n{s.markdown_preview}")

            else:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                return build_error_result(
                    file_path=path,
                    status=DocumentStatus.UNSUPPORTED_FORMAT,
                    error_msg=f"Unsupported format '{detected_type}'.",
                    file_type=detected_type,
                    file_hash=file_hash,
                    processing_time_ms=elapsed_ms,
                )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            error_details = f"Processing exception in {detected_type} handler: {str(exc)}\n{traceback.format_exc()}"
            logger.error(f"Failed to process {path.name}: {error_details}")
            return build_error_result(
                file_path=path,
                status=DocumentStatus.FAILED_CORRUPTED,
                error_msg=error_details,
                file_type=detected_type,
                file_hash=file_hash,
                processing_time_ms=elapsed_ms,
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        status = DocumentStatus.PARTIAL_SUCCESS if has_warnings else DocumentStatus.SUCCESS
        total_items = len(pages) if pages else len(sheets)

        return DocumentProcessingResult(
            document_id=doc_id,
            file_name=path.name,
            file_type=detected_type,
            file_hash=file_hash,
            status=status,
            total_pages_or_sheets=total_items,
            pages=pages,
            sheets=sheets,
            full_merged_text="\n\n".join(full_merged_text_parts),
            processing_time_ms=round(elapsed_ms, 2),
            error_details=None,
        )

    def process_batch(
        self,
        file_paths: Sequence[Union[str, Path]],
        max_workers: Optional[int] = None,
    ) -> List[DocumentProcessingResult]:
        """Process multiple heterogeneous documents in parallel while preserving exact input ordering."""
        workers = max_workers or self.config.max_parallel_workers
        paths = [Path(p) for p in file_paths]

        logger.info(f"Beginning batch ingestion of {len(paths)} documents with {workers} workers.")

        if len(paths) <= 1 or workers <= 1:
            return [self.process_file(p) for p in paths]

        results: List[Optional[DocumentProcessingResult]] = [None] * len(paths)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {executor.submit(self.process_file, p): idx for idx, p in enumerate(paths)}
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()

        return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI runner: python -m src.ingestion.router <path_or_dir> [--output-json <file>]."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Enterprise Document Intelligence Ingestion Engine CLI."
    )
    parser.add_argument(
        "target",
        type=str,
        help="Path to an individual file or directory of documents.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to export full JSON results.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent worker threads (default: 4).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed debug logging.",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    target_path = Path(args.target).resolve()
    if not target_path.exists():
        print(f"Error: Target path does not exist: {target_path}", file=sys.stderr)
        sys.exit(1)

    # Discover files
    from src.ingestion.validators import SUPPORTED_EXTENSIONS
    if target_path.is_file():
        file_list = [target_path]
    else:
        file_list = [
            p for p in target_path.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]

    if not file_list:
        print(f"No supported document files found in: {target_path}", file=sys.stderr)
        sys.exit(0)

    print(f"\n[*] Initializing Document Ingestion Engine on {len(file_list)} file(s)...")
    config = IngestionConfig(max_parallel_workers=args.workers)
    engine = DocumentIngestionEngine(config=config)

    t0 = time.perf_counter()
    results = engine.process_batch(file_list, max_workers=args.workers)
    total_time = (time.perf_counter() - t0) * 1000.0

    print("\n" + "=" * 90)
    print(f"{'STATUS':<18} | {'PAGES/SHEETS':<12} | {'TIME (ms)':<10} | {'FILE NAME'}")
    print("-" * 90)
    for r in results:
        status_str = r.status.value if hasattr(r.status, "value") else str(r.status)
        count_str = str(r.total_pages_or_sheets)
        time_str = f"{r.processing_time_ms:.1f}"
        print(f"{status_str:<18} | {count_str:<12} | {time_str:<10} | {r.file_name}")
        if r.error_details:
            print(f"   -> Error: {r.error_details}")
    print("=" * 90)
    print(f"Batch completed in {total_time:.2f} ms ({len(results)} files processed).\n")

    if args.output_json:
        out_path = Path(args.output_json).resolve()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([r.model_dump(mode="json") for r in results], f, indent=2)
        print(f"Full JSON results saved to: {out_path}")


if __name__ == "__main__":
    main()
