"""Security tests for file upload pipeline, decompression bomb defense, and formula injection."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from src.api.main import app
from src.ingestion.schemas import DocumentStatus
from src.ingestion.spreadsheet_processor import SpreadsheetProcessor
from src.ingestion.validators import (
    _validate_image_deep,
    _validate_xlsx_deep,
    validate_file_integrity,
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_zip_bomb_decompression_attack_prevention(tmp_path: Path):
    """Verify that an XLSX archive with extreme compression ratio or large uncompressed bytes is rejected."""
    zip_bomb_path = tmp_path / "bomb.xlsx"

    # Create a high-ratio simulated ZIP bomb
    with zipfile.ZipFile(zip_bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 110 MB of zeros compresses to ~100 KB
        zf.writestr("xl/workbook.xml", b"\x00" * (110 * 1024 * 1024))
        zf.writestr("[Content_Types].xml", b"<Types/>")

    is_valid, status, ftype, err = _validate_xlsx_deep(zip_bomb_path)
    assert is_valid is False
    assert status == DocumentStatus.FAILED_CORRUPTED
    assert "limit" in err.lower() or "bomb" in err.lower()


def test_pixel_flood_image_bomb_prevention(tmp_path: Path):
    """Verify that an image exceeding 50 megapixels is rejected as a DoS attempt."""
    large_img_path = tmp_path / "giant_image.png"

    # Create a small-file PNG with 10,000 x 6,000 dimensions = 60 Megapixels (> 50MP ceiling)
    img = Image.new("L", (10000, 6000), color=255)
    img.save(large_img_path, format="PNG")

    is_valid, status, ftype, err = _validate_image_deep(large_img_path, "png")
    assert is_valid is False
    assert status == DocumentStatus.FAILED_CORRUPTED
    assert "exceed" in err.lower() or "limit" in err.lower()


def test_oversized_upload_file_rejected_with_413(client: TestClient):
    """Verify that upload requests exceeding 50MB are rejected immediately with 413 Payload Too Large."""
    # 52 MB payload
    oversized_data = b"%PDF-1.4 " + b"0" * (52 * 1024 * 1024)
    files = [("files", ("huge.pdf", oversized_data, "application/pdf"))]

    resp = client.post("/api/v1/documents/upload", files=files)
    assert resp.status_code == 413
    assert "exceeds maximum allowed size" in resp.json()["detail"]


def test_csv_formula_injection_defense():
    """Verify that CSV / Excel formula injection characters (=, +, -, @) are sanitized with single quotes."""
    processor = SpreadsheetProcessor()

    malicious_inputs = [
        "=cmd|' /C calc'!A0",
        "+cmd|' /C calc'!A0",
        "-2+3+cmd|' /C calc'!A0",
        "@SUM(1+1)*cmd|' /C calc'!A0",
        "\t=2+5",
    ]

    for raw in malicious_inputs:
        cleaned = processor.clean_record_value(raw)
        assert cleaned.startswith("'"), f"Formula injection payload was not neutralized: {raw} -> {cleaned}"
