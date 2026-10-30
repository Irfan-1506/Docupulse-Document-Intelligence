"""Hybrid PDF processor for high-volume and 1,000+ page documents."""

from __future__ import annotations

import concurrent.futures
import gc
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple

import cv2
import numpy as np
import pymupdf

from src.ingestion.ocr_processor import OCRProcessor
from src.ingestion.schemas import ExtractionMethod, IngestionConfig, PageContent

logger = logging.getLogger(__name__)


class PDFProcessor:
    """Enterprise-grade hybrid PDF processor handling large volumes (1,000+ pages).

    Features:
    - Fast digital text and table extraction via PyMuPDF.
    - Page-level hybrid classification: automatically falls back to OCR if a page
      is a scanned image or has low digital text density.
    - Bounded-memory chunked streaming with worker pool parallelism.
    """

    def __init__(
        self,
        config: Optional[IngestionConfig] = None,
        ocr_processor: Optional[OCRProcessor] = None,
    ) -> None:
        self.config = config or IngestionConfig()
        self.ocr_processor = ocr_processor or OCRProcessor(config=self.config)

    def _is_scanned_page(self, page: pymupdf.Page) -> Tuple[bool, str, str]:
        """Classify whether a page requires OCR fallback or can use digital extraction.

        Returns:
            Tuple of (requires_ocr: bool, reason: str, raw_text: str).
        """
        raw_text = page.get_text("text") or ""
        non_ws_chars = len("".join(raw_text.split()))

        # Check full-page raster scan overlay
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height
        image_list = page.get_images(full=True)

        has_large_raster_overlay = False
        if image_list and page_area > 0:
            for img_info in image_list:
                xref = img_info[0]
                try:
                    rects = page.get_image_rects(xref)
                    for r in rects:
                        area = r.width * r.height
                        if (area / page_area) >= self.config.max_raster_coverage_ratio:
                            has_large_raster_overlay = True
                            break
                    if has_large_raster_overlay:
                        break
                except Exception:
                    continue

        if non_ws_chars < self.config.min_digital_text_chars:
            return True, f"Low character density ({non_ws_chars} < {self.config.min_digital_text_chars} chars)", raw_text

        if has_large_raster_overlay and non_ws_chars < (self.config.min_digital_text_chars * 2):
            return True, "Full-page raster scan overlay with low native digital text", raw_text

        return False, "Digital text above threshold without dominant raster scan", raw_text

    def _extract_tables(self, page: pymupdf.Page, raw_text: str = "") -> List[dict[str, Any]]:
        """Extract structured tables from native digital PDF pages with fast candidate filtering."""
        # Fast pre-filtering: only invoke expensive vector analysis if page has line art or multi-space columns
        drawings = page.get_drawings()
        has_table_candidate = len(drawings) > 0 or any(
            "\t" in line or "   " in line
            for line in raw_text.splitlines()[:20]
        )
        if not has_table_candidate:
            return []

        tables_data: List[dict[str, Any]] = []
        try:
            tabs = page.find_tables()
            if not tabs or not tabs.tables:
                return []

            for idx, tab in enumerate(tabs.tables):
                extracted = tab.extract()
                if not extracted or len(extracted) == 0:
                    continue

                raw_header = extracted[0]
                headers = [
                    str(col).strip() if col is not None and str(col).strip() else f"col_{i+1}"
                    for i, col in enumerate(raw_header)
                ]

                data_rows: List[dict[str, Any]] = []
                for row in extracted[1:]:
                    row_dict: dict[str, Any] = {}
                    for col_idx, val in enumerate(row):
                        col_key = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx+1}"
                        row_dict[col_key] = str(val).strip() if val is not None else ""
                    data_rows.append(row_dict)

                tables_data.append({
                    "table_index": idx + 1,
                    "bbox": [float(c) for c in tab.bbox],
                    "row_count": len(data_rows),
                    "headers": headers,
                    "rows": data_rows,
                    "raw_grid": extracted,
                })
        except Exception as exc:
            logger.debug(f"Table finder encountered error on page {page.number + 1}: {exc}")

        return tables_data

    def _rasterize_page_to_bgr(self, page: pymupdf.Page) -> np.ndarray:
        """Rasterize a PDF page to an OpenCV BGR numpy array."""
        pix = page.get_pixmap(dpi=self.config.render_dpi)
        img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))

        if pix.n == 4:
            bgr = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        elif pix.n == 1:
            bgr = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
        else:
            bgr = img_np

        # Clean up pixmap memory
        del pix
        return bgr

    def _process_single_page(self, page: pymupdf.Page) -> PageContent:
        """Process an individual page using hybrid digital / OCR routing."""
        page_num = page.number + 1
        requires_ocr, reason, raw_text = self._is_scanned_page(page)

        if not requires_ocr:
            # Native digital extraction
            tables = self._extract_tables(page, raw_text=raw_text)
            return PageContent(
                page_number=page_num,
                raw_text=raw_text.strip(),
                extraction_method=ExtractionMethod.DIGITAL_PDF,
                confidence_score=1.0,
                tables=tables,
                warnings=[],
            )

        # Scanned or image-overlay page: route to OCR pipeline
        logger.debug(f"Page {page_num} routed to OCR pipeline: {reason}")
        bgr_image = self._rasterize_page_to_bgr(page)
        page_content = self.ocr_processor.process_image_array(bgr_image, page_number=page_num)

        # Explicitly release raster array
        del bgr_image
        return page_content

    def _process_chunk(self, file_path: Path, start_idx: int, end_idx: int) -> List[PageContent]:
        """Process a slice of pages in a separate thread/process with dedicated file handle."""
        doc = pymupdf.open(file_path)
        chunk_pages: List[PageContent] = []
        try:
            for idx in range(start_idx, min(end_idx, len(doc))):
                page = doc[idx]
                chunk_pages.append(self._process_single_page(page))
        finally:
            doc.close()
            gc.collect()

        return chunk_pages

    def process(self, file_path: Path) -> List[PageContent]:
        """Process complete PDF document with memory-safe chunking and parallelism.

        Handles up to 1,000+ pages without unbounded memory growth.
        """
        doc = pymupdf.open(file_path)
        total_pages = len(doc)
        doc.close()

        if total_pages == 0:
            return []

        chunk_size = max(1, self.config.pdf_chunk_size)
        page_chunks: List[Tuple[int, int]] = [
            (i, min(i + chunk_size, total_pages))
            for i in range(0, total_pages, chunk_size)
        ]

        logger.info(
            f"Processing PDF '{file_path.name}' ({total_pages} pages) in {len(page_chunks)} chunks "
            f"using {self.config.max_parallel_workers} workers."
        )

        all_pages: List[PageContent] = []

        # If single chunk or small file, run sequentially to avoid thread pool overhead
        if len(page_chunks) == 1 or self.config.max_parallel_workers <= 1:
            for start_idx, end_idx in page_chunks:
                all_pages.extend(self._process_chunk(file_path, start_idx, end_idx))
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.config.max_parallel_workers
            ) as executor:
                future_to_chunk = {
                    executor.submit(self._process_chunk, file_path, start, end): (start, end)
                    for start, end in page_chunks
                }
                for future in concurrent.futures.as_completed(future_to_chunk):
                    chunk_results = future.result()
                    all_pages.extend(chunk_results)

        # Sort pages by natural page number
        all_pages.sort(key=lambda p: p.page_number)
        return all_pages
