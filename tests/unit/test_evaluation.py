"""Unit tests for Task 7 evaluation benchmark generator, metrics, and report formatting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evaluation.generator import GoldenDatasetGenerator
from src.evaluation.metrics import BenchmarkMetricsCalculator, normalize_string, numbers_match
from src.evaluation.schemas import (
    DocumentType,
    FieldEvaluationScore,
    GoldenDocumentManifest,
    GroundTruthLineItem,
    GroundTruthRecord,
)
from src.extraction.schemas import ExtractedField, InvoiceExtractionRecord, LineItem


def test_string_normalization_and_number_matching():
    """Test text normalization and financial tolerance matching."""
    assert normalize_string("  Acme Cloud, Inc. ") == "acme cloud inc"
    assert normalize_string(None) == ""
    assert numbers_match(100.00, 100.01, tolerance=0.02) is True
    assert numbers_match(100.00, 100.05, tolerance=0.02) is False
    assert numbers_match(None, None) is True
    assert numbers_match(10.0, None) is False


def test_benchmark_metrics_calculator_perfect_match():
    """Test metrics calculator on exact match pairs."""
    calc = BenchmarkMetricsCalculator()

    truth = GroundTruthRecord(
        invoice_number="INV-2026-001",
        invoice_date="2026-03-15",
        supplier_name="Acme Cloud Technologies",
        customer_name="Enterprise Holdings",
        subtotal=1000.00,
        tax=100.00,
        total=1100.00,
        line_items=[GroundTruthLineItem(description="Cloud Server", quantity=1, unit_price=1000.0, amount=1000.0)],
    )

    pred = InvoiceExtractionRecord(
        record_id="rec_test_001",
        source_document_id="doc_test_001",
        page_range_or_sheet="page 1",
        invoice_number="INV-2026-001",
        invoice_date="2026-03-15",
        supplier_name="Acme Cloud Technologies",
        customer_name="Enterprise Holdings",
        subtotal=1000.00,
        tax=100.00,
        total=1100.00,
        currency="USD",
        line_items=[LineItem(description="Cloud Server", quantity=1.0, unit_price=1000.0, line_total=1000.0)],
    )

    calc.evaluate_pair(pred, truth)
    summary = calc.compute_summary_report()

    assert summary["macro_precision"] == 1.0
    assert summary["macro_recall"] == 1.0
    assert summary["macro_f1"] == 1.0
    assert summary["field_metrics"]["invoice_number"].exact_matches == 1


def test_golden_dataset_generation(tmp_path: Path):
    """Test generating a mini golden dataset with diverse document types."""
    gen = GoldenDatasetGenerator(tmp_path)
    manifests = gen.generate_full_dataset(total_target=10)

    assert len(manifests) >= 10
    manifest_file = tmp_path / "dataset_manifest.json"
    assert manifest_file.exists()

    types = {m.doc_type for m in manifests}
    assert DocumentType.DIGITAL_PDF in types
    assert DocumentType.SCANNED_PDF in types
    assert DocumentType.EXCEL_WORKBOOK in types
    assert DocumentType.CSV_SPREADSHEET in types
    assert DocumentType.CORRUPTED_BINARY in types


def test_benchmark_evaluator_end_to_end(tmp_path: Path):
    """Test BenchmarkEvaluator executing on a synthetic mini dataset."""
    from src.evaluation.evaluator import BenchmarkEvaluator

    data_dir = tmp_path / "mini_data"
    rep_dir = tmp_path / "mini_reports"
    gen = GoldenDatasetGenerator(data_dir)
    # Generate 1 digital pdf, 1 excel, 1 csv, 1 corrupt
    manifests = [
        gen._generate_clean_digital_pdf(1),
        gen._generate_excel_workbook(1),
        gen._generate_csv_document(1),
        gen._generate_corrupted_document(1),
    ]
    index_file = data_dir / "dataset_manifest.json"
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump([m.model_dump() for m in manifests], f, indent=2)

    evaluator = BenchmarkEvaluator(
        dataset_dir=data_dir,
        reports_dir=rep_dir,
        target_f1_threshold=0.90,
        target_p95_sec_threshold=2.5,
    )
    report = evaluator.run_benchmark()

    assert report.total_samples == 4
    assert report.operational.total_documents == 4
    assert report.operational.corrupted_detected_correctly == 1
    assert (rep_dir / "evaluation_benchmark.json").exists()
    assert (rep_dir / "evaluation_benchmark.md").exists()
    assert "DocuPulse Enterprise AP" in (rep_dir / "evaluation_benchmark.md").read_text(encoding="utf-8")


def test_cli_runners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test CLI entrypoints for extraction and validation."""
    from src.extraction import cli as ext_cli
    from src.validation import cli as val_cli
    from src.evaluation import evaluator as eval_cli

    gen = GoldenDatasetGenerator(tmp_path)
    sample_doc = gen._generate_clean_digital_pdf(99)
    doc_path = str(tmp_path / sample_doc.relative_path)
    out_ext = tmp_path / "out_ext.json"
    out_val = tmp_path / "out_val.json"

    # Test extraction CLI
    monkeypatch.setattr("sys.argv", ["cli.py", doc_path, "--output-json", str(out_ext)])
    ext_cli.main()
    assert out_ext.exists()

    # Test validation CLI
    monkeypatch.setattr("sys.argv", ["cli.py", doc_path, "--output-json", str(out_val)])
    val_cli.main()
    assert out_val.exists()

    # Test evaluator CLI --help
    with pytest.raises(SystemExit) as exc:
        monkeypatch.setattr("sys.argv", ["evaluator.py", "--help"])
        eval_cli.main()
    assert exc.value.code == 0
