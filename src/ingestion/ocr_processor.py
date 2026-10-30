"""Image restoration, preprocessing, and modular OCR extraction pipeline."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from src.ingestion.schemas import ExtractionMethod, IngestionConfig, PageContent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modular OCR Engine Interfaces
# ---------------------------------------------------------------------------

class BaseOCREngine(ABC):
    """Abstract Base Class for OCR engines (RapidOCR, Tesseract, etc.)."""

    @abstractmethod
    def extract_text_and_confidence(self, image: np.ndarray) -> Tuple[str, float]:
        """Extract textual content and mean confidence score from a preprocessed image.

        Args:
            image: Single or multi-channel numpy array image.

        Returns:
            Tuple of (extracted_text, mean_confidence) where confidence is in [0.0, 1.0].
        """
        pass


class RapidOCREngine(BaseOCREngine):
    """Production OCR engine powered by RapidOCR (ONNX Runtime)."""

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
            self._available = True
        except Exception as exc:
            logger.warning(f"Failed to initialize RapidOCR: {exc}")
            self._engine = None
            self._available = False

    def extract_text_and_confidence(self, image: np.ndarray) -> Tuple[str, float]:
        if not self._available or self._engine is None:
            raise RuntimeError("RapidOCR is not available.")

        # RapidOCR accepts grayscale or BGR
        if len(image.shape) == 2:
            proc_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        else:
            proc_img = image

        try:
            results, _ = self._engine(proc_img)
        except Exception as exc:
            logger.error(f"RapidOCR inference error: {exc}")
            return "", 0.0

        if not results:
            return "", 1.0

        text_lines: List[str] = []
        confidences: List[float] = []

        for item in results:
            # item format: [box, text, score]
            if len(item) >= 3:
                txt = str(item[1]).strip()
                score = float(item[2])
                if txt:
                    text_lines.append(txt)
                    confidences.append(score)

        merged_text = "\n".join(text_lines)
        mean_conf = float(np.mean(confidences)) if confidences else 1.0
        return merged_text, max(0.0, min(1.0, mean_conf))


class TesseractOCREngine(BaseOCREngine):
    """Modular fallback or alternative engine using pytesseract."""

    def __init__(self) -> None:
        try:
            import pytesseract
            self._pytesseract = pytesseract
            self._available = True
        except ImportError:
            self._pytesseract = None
            self._available = False

    def extract_text_and_confidence(self, image: np.ndarray) -> Tuple[str, float]:
        if not self._available or self._pytesseract is None:
            raise RuntimeError("pytesseract is not installed or available.")

        try:
            from pytesseract import Output
            data = self._pytesseract.image_to_data(image, output_type=Output.DICT)
            text_lines: List[str] = []
            confidences: List[float] = []

            n_boxes = len(data["text"])
            current_line: List[str] = []
            last_line_num = -1

            for i in range(n_boxes):
                txt = data["text"][i].strip()
                conf_val = float(data["conf"][i])
                line_num = data["line_num"][i]

                if conf_val >= 0 and txt:
                    if line_num != last_line_num and current_line:
                        text_lines.append(" ".join(current_line))
                        current_line = []
                    current_line.append(txt)
                    confidences.append(conf_val / 100.0)
                    last_line_num = line_num

            if current_line:
                text_lines.append(" ".join(current_line))

            merged_text = "\n".join(text_lines)
            mean_conf = float(np.mean(confidences)) if confidences else 1.0
            return merged_text, max(0.0, min(1.0, mean_conf))
        except Exception as exc:
            logger.warning(f"Tesseract OCR failed: {exc}")
            return "", 0.0


class FallbackOCREngine(BaseOCREngine):
    """Graceful no-op fallback when OCR backends are missing."""

    def extract_text_and_confidence(self, image: np.ndarray) -> Tuple[str, float]:
        logger.warning("Using FallbackOCREngine: no OCR engine configured or operational.")
        return "", 0.0


def get_ocr_engine(engine_name: str = "auto") -> BaseOCREngine:
    """Factory to instantiate the configured OCR engine."""
    norm = engine_name.lower().strip()
    if norm in ("auto", "rapidocr"):
        rapid = RapidOCREngine()
        if rapid._available:
            return rapid
        if norm == "rapidocr":
            logger.warning("RapidOCR requested but unavailable, attempting Tesseract.")

    if norm in ("auto", "tesseract"):
        tess = TesseractOCREngine()
        if tess._available:
            try:
                # Test minimal execution
                test_mat = np.ones((50, 50), dtype=np.uint8) * 255
                tess.extract_text_and_confidence(test_mat)
                return tess
            except Exception:
                pass

    # Default fallback
    rapid = RapidOCREngine()
    if rapid._available:
        return rapid

    logger.warning("No functional OCR engine found. Falling back to FallbackOCREngine.")
    return FallbackOCREngine()


# ---------------------------------------------------------------------------
# Image Preprocessor
# ---------------------------------------------------------------------------

class ImagePreprocessor:
    """OpenCV-based image enhancement and restoration pipeline."""

    @staticmethod
    def to_grayscale(image: np.ndarray) -> np.ndarray:
        """Convert input image to single-channel 8-bit grayscale."""
        if len(image.shape) == 2:
            return image
        if image.shape[2] == 4:
            # RGBA -> BGR -> Gray
            bgr = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def apply_clahe(gray: np.ndarray, clip_limit: float = 2.0) -> np.ndarray:
        """Apply Contrast Limited Adaptive Histogram Equalization."""
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
        return clahe.apply(gray)

    @staticmethod
    def denoise(gray: np.ndarray, h: int = 10) -> np.ndarray:
        """Apply Non-Local Means Denoising to filter scanning artifacts."""
        return cv2.fastNlMeansDenoising(
            gray,
            None,
            h=h,
            templateWindowSize=7,
            searchWindowSize=21,
        )

    @staticmethod
    def deskew(image: np.ndarray) -> Tuple[np.ndarray, float]:
        """Detect and correct document skew using minimum-area bounding box.

        Returns:
            Tuple of (deskewed_image, angle_in_degrees).
        """
        gray = ImagePreprocessor.to_grayscale(image)

        # Invert threshold so foreground text pixels are 255
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = np.column_stack(np.where(thresh > 0))

        if len(coords) < 30:
            return image, 0.0

        rect = cv2.minAreaRect(coords)
        angle = rect[-1]

        # Normalise OpenCV minAreaRect angle
        if angle < -45:
            angle = -(90 + angle)
        elif angle > 45:
            angle = 90 - angle

        # Only correct moderate skew angles typical in document scans
        if abs(angle) < 0.4 or abs(angle) > 45.0:
            return image, 0.0

        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        deskewed = cv2.warpAffine(
            image,
            M,
            (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255) if len(image.shape) == 3 else 255,
        )
        return deskewed, angle

    @staticmethod
    def apply_adaptive_threshold(gray: np.ndarray) -> np.ndarray:
        """Adaptive Gaussian thresholding for receipts and low-contrast scans."""
        return cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21,
            10,
        )

    def preprocess_pipeline(self, image: np.ndarray) -> np.ndarray:
        """Execute full 4-stage restoration pipeline:

        1. Grayscale & CLAHE contrast normalization.
        2. Non-local means denoising.
        3. Deskewing.
        4. Adaptive thresholding for maximum text contrast.
        """
        # Step 1: Grayscale & Contrast
        gray = self.to_grayscale(image)
        enhanced = self.apply_clahe(gray, clip_limit=2.0)

        # Step 2: Denoise
        denoised = self.denoise(enhanced, h=10)

        # Step 3: Deskew
        deskewed, angle = self.deskew(denoised)
        if abs(angle) > 0.5:
            logger.debug(f"Corrected document skew by {angle:.2f} degrees.")

        # Step 4: Adaptive thresholding
        thresholded = self.apply_adaptive_threshold(deskewed)

        return thresholded


# ---------------------------------------------------------------------------
# OCR Processor Orchestrator
# ---------------------------------------------------------------------------

class OCRProcessor:
    """Manages image enhancement and OCR execution for standalone images and raster pages."""

    def __init__(
        self,
        config: Optional[IngestionConfig] = None,
        engine: Optional[BaseOCREngine] = None,
    ) -> None:
        self.config = config or IngestionConfig()
        self.engine = engine or get_ocr_engine(self.config.ocr_engine)
        self.preprocessor = ImagePreprocessor()

    def process_image_array(
        self,
        image_array: np.ndarray,
        page_number: int = 1,
    ) -> PageContent:
        """Process an in-memory numpy image through restoration and OCR."""
        warnings: List[str] = []

        if self.config.enable_image_preprocessing:
            try:
                processed_img = self.preprocessor.preprocess_pipeline(image_array)
            except Exception as exc:
                logger.warning(f"Preprocessing failed on page {page_number}: {exc}. Using raw image.")
                processed_img = image_array
                warnings.append(f"Image preprocessing failed: {exc}")
        else:
            processed_img = image_array

        raw_text, confidence = self.engine.extract_text_and_confidence(processed_img)

        if confidence < self.config.ocr_confidence_threshold and raw_text:
            warn_msg = (
                f"Low OCR confidence ({confidence:.2f} < {self.config.ocr_confidence_threshold:.2f}); "
                "manual verification recommended."
            )
            warnings.append(warn_msg)
            logger.info(f"Page {page_number}: {warn_msg}")

        return PageContent(
            page_number=page_number,
            raw_text=raw_text,
            extraction_method=ExtractionMethod.OCR,
            confidence_score=confidence,
            tables=[],
            warnings=warnings,
        )

    def process_image_file(self, file_path: Path) -> List[PageContent]:
        """Process standalone image files (JPG, PNG, and multi-frame TIFFs)."""
        pages: List[PageContent] = []

        try:
            with Image.open(file_path) as pil_img:
                n_frames = getattr(pil_img, "n_frames", 1)

                for frame_idx in range(n_frames):
                    page_num = frame_idx + 1
                    pil_img.seek(frame_idx)
                    # Convert to RGB numpy array
                    frame_rgb = pil_img.convert("RGB")
                    img_np = np.array(frame_rgb)
                    # PIL uses RGB; convert to BGR for OpenCV
                    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

                    page_content = self.process_image_array(img_bgr, page_number=page_num)
                    pages.append(page_content)
        except Exception as exc:
            logger.error(f"Error processing image file {file_path}: {exc}")
            raise

        return pages
