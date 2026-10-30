"""File validation, magic-byte verification, and corruption guard."""

from __future__ import annotations

import hashlib
import logging
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Tuple

import pymupdf
from PIL import Image

from src.ingestion.schemas import DocumentProcessingResult, DocumentStatus

logger = logging.getLogger(__name__)

# Magic byte signatures
MAGIC_BYTES = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "tiff": [b"II*\x00", b"MM\x00*"],
    "tif": [b"II*\x00", b"MM\x00*"],
    "xlsx": [b"PK\x03\x04"],
    "xls": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
}

SUPPORTED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".xlsx", ".xls", ".csv"
}


def compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hash of a file efficiently using chunked reads."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def detect_file_type_from_header(file_path: Path) -> Optional[str]:
    """Detect file type by checking header magic bytes."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)
    except Exception as exc:
        logger.warning(f"Could not read file header for {file_path}: {exc}")
        return None

    if not header:
        return None

    for ftype, signatures in MAGIC_BYTES.items():
        for sig in signatures:
            if header.startswith(sig):
                return ftype

    # Check if CSV/plain text: no binary null bytes in initial sample
    try:
        sample = header[:32]
        if b"\x00" not in sample and any(32 <= b <= 126 or b in (9, 10, 13) for b in sample):
            return "csv"
    except Exception:
        pass

    return None


def validate_file_integrity(
    file_path: Path,
) -> Tuple[bool, DocumentStatus, str, Optional[str]]:
    """Validate file existence, size, magic bytes, and container integrity.

    Returns:
        Tuple of (is_valid, status, file_type, error_message).
    """
    if not file_path.exists():
        return False, DocumentStatus.FAILED_CORRUPTED, "unknown", f"File does not exist: {file_path}"

    if not file_path.is_file():
        return False, DocumentStatus.FAILED_CORRUPTED, "unknown", f"Path is not a regular file: {file_path}"

    file_size = file_path.stat().st_size
    if file_size == 0:
        return False, DocumentStatus.FAILED_CORRUPTED, "unknown", "File is empty (0 bytes)."

    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return (
            False,
            DocumentStatus.UNSUPPORTED_FORMAT,
            ext.lstrip("."),
            f"Unsupported document extension: '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    norm_type = ext.lstrip(".")
    if norm_type in ("jpeg", "tif"):
        norm_type = "jpg" if norm_type == "jpeg" else "tiff"

    detected_type = detect_file_type_from_header(file_path)

    # Magic byte mismatch check for binary formats
    if norm_type in ("pdf", "png", "jpg", "tiff", "xlsx", "xls"):
        expected_sigs = MAGIC_BYTES.get(norm_type, [])
        if detected_type is None or not any(
            detected_type == k for k in ([norm_type] if norm_type != "jpg" else ["jpg", "jpeg"])
        ):
            # Check if signature matches directly
            try:
                with open(file_path, "rb") as f:
                    head = f.read(16)
                if not any(head.startswith(sig) for sig in expected_sigs):
                    return (
                        False,
                        DocumentStatus.FAILED_CORRUPTED,
                        norm_type,
                        f"Magic byte mismatch: file has '{ext}' extension but header bytes {head[:8]!r} do not match.",
                    )
            except Exception as exc:
                return False, DocumentStatus.FAILED_CORRUPTED, norm_type, f"Error reading header: {exc}"

    # Format-specific deep checks
    if norm_type == "pdf":
        return _validate_pdf_deep(file_path)
    elif norm_type in ("png", "jpg", "tiff"):
        return _validate_image_deep(file_path, norm_type)
    elif norm_type == "xlsx":
        return _validate_xlsx_deep(file_path)
    elif norm_type == "xls":
        return _validate_xls_deep(file_path)
    elif norm_type == "csv":
        return True, DocumentStatus.SUCCESS, "csv", None

    return True, DocumentStatus.SUCCESS, norm_type, None


def _validate_pdf_deep(file_path: Path) -> Tuple[bool, DocumentStatus, str, Optional[str]]:
    """Deep inspection of PDF container and encryption status."""
    try:
        doc = pymupdf.open(file_path)
        if doc.is_encrypted or doc.needs_pass:
            doc.close()
            return (
                False,
                DocumentStatus.FAILED_CORRUPTED,
                "pdf",
                "PDF is password-protected or encrypted; credentials required.",
            )
        page_count = len(doc)
        if page_count == 0:
            doc.close()
            return False, DocumentStatus.FAILED_CORRUPTED, "pdf", "PDF file contains 0 pages."
        # Read first page to verify xref catalog integrity
        _ = doc[0].get_text()
        doc.close()
        return True, DocumentStatus.SUCCESS, "pdf", None
    except Exception as exc:
        return False, DocumentStatus.FAILED_CORRUPTED, "pdf", f"Malformed or truncated PDF: {str(exc)}"


def _validate_image_deep(file_path: Path, norm_type: str) -> Tuple[bool, DocumentStatus, str, Optional[str]]:
    """Deep inspection of standalone image files."""
    try:
        with Image.open(file_path) as img:
            img.verify()
        # Verify again by actually loading pixels (verify can pass corrupted payloads)
        with Image.open(file_path) as img:
            img.load()
        return True, DocumentStatus.SUCCESS, norm_type, None
    except Exception as exc:
        return False, DocumentStatus.FAILED_CORRUPTED, norm_type, f"Corrupted or unreadable image file: {str(exc)}"


def _validate_xlsx_deep(file_path: Path) -> Tuple[bool, DocumentStatus, str, Optional[str]]:
    """Deep inspection of XLSX ZIP/XML packaging."""
    try:
        if not zipfile.is_zipfile(file_path):
            return False, DocumentStatus.FAILED_CORRUPTED, "xlsx", "XLSX file is not a valid ZIP archive."
        with zipfile.ZipFile(file_path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file:
                return False, DocumentStatus.FAILED_CORRUPTED, "xlsx", f"Corrupt entry in XLSX archive: {bad_file}"
            namelist = zf.namelist()
            if not any("workbook.xml" in name.lower() or "[content_types].xml" in name.lower() for name in namelist):
                return False, DocumentStatus.FAILED_CORRUPTED, "xlsx", "XLSX archive missing required workbook XML structures."
        return True, DocumentStatus.SUCCESS, "xlsx", None
    except Exception as exc:
        return False, DocumentStatus.FAILED_CORRUPTED, "xlsx", f"Corrupt XLSX file: {str(exc)}"


def _validate_xls_deep(file_path: Path) -> Tuple[bool, DocumentStatus, str, Optional[str]]:
    """Deep inspection of legacy OLE2 XLS file."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return False, DocumentStatus.FAILED_CORRUPTED, "xls", "Invalid OLE2 Compound Document header for XLS."
        return True, DocumentStatus.SUCCESS, "xls", None
    except Exception as exc:
        return False, DocumentStatus.FAILED_CORRUPTED, "xls", f"Corrupt XLS file: {str(exc)}"


def build_error_result(
    file_path: Path,
    status: DocumentStatus,
    error_msg: str,
    file_type: str = "unknown",
    file_hash: str = "",
    processing_time_ms: float = 0.0,
) -> DocumentProcessingResult:
    """Build a failure result cleanly without throwing unhandled exceptions."""
    if not file_hash and file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0:
        try:
            file_hash = compute_sha256(file_path)
        except Exception:
            file_hash = "unavailable"
    elif not file_hash:
        file_hash = "none"

    return DocumentProcessingResult(
        document_id=str(uuid.uuid4()),
        file_name=file_path.name,
        file_type=file_type,
        file_hash=file_hash,
        status=status,
        total_pages_or_sheets=0,
        pages=[],
        sheets=[],
        full_merged_text="",
        processing_time_ms=processing_time_ms,
        error_details=error_msg,
    )
