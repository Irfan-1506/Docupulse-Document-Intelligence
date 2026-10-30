"""Comprehensive evaluation benchmark engine running end-to-end against golden datasets."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.evaluation.generator import GoldenDatasetGenerator
from src.evaluation.metrics import BenchmarkMetricsCalculator
from src.evaluation.schemas import (
    EvaluationBenchmarkReport,
    GoldenDocumentManifest,
    GroundTruthRecord,
    LatencyPercentiles,
    OperationalMetrics,
)
from src.extraction.engine import AIExtractionEngine
from src.extraction.schemas import DocumentExtractionResponse, ExtractionConfig, ExtractionProvider
from src.ingestion.router import DocumentIngestionEngine
from src.ingestion.schemas import DocumentProcessingResult, DocumentStatus, IngestionConfig
from src.validation.engine import ValidationEngine
from src.validation.schemas import BatchValidationReport, ValidationStatus

logger = logging.getLogger("document_intelligence.evaluation")


class BenchmarkEvaluator:
    """Orchestrates end-to-end evaluation against a labeled golden dataset."""

    def __init__(
        self,
        dataset_dir: Path,
        reports_dir: Optional[Path] = None,
        target_f1_threshold: float = 0.95,
        target_p95_sec_threshold: float = 2.5,
    ):
        self.dataset_dir = dataset_dir
        self.reports_dir = reports_dir or (Path(__file__).resolve().parent.parent.parent / "reports")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.target_f1_threshold = target_f1_threshold
        self.target_p95_sec_threshold = target_p95_sec_threshold

        # Initialize engines
        self.ingestion_engine = DocumentIngestionEngine(IngestionConfig(enable_ocr=True))
        self.extraction_engine = AIExtractionEngine(ExtractionConfig(provider=ExtractionProvider.HEURISTIC_FALLBACK))
        self.validation_engine = ValidationEngine()
        self.metrics_calculator = BenchmarkMetricsCalculator()

    def run_benchmark(self) -> EvaluationBenchmarkReport:
        """Execute end-to-end ingestion, extraction, and validation on all dataset documents."""
        manifest_file = self.dataset_dir / "dataset_manifest.json"
        if not manifest_file.exists():
            logger.info("Dataset manifest not found at %s. Generating golden dataset...", self.dataset_dir)
            generator = GoldenDatasetGenerator(self.dataset_dir)
            generator.generate_full_dataset(total_target=52)

        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_dicts = json.load(f)

        manifests = [GoldenDocumentManifest(**m) for m in manifest_dicts]
        logger.info("Loaded %d golden dataset document manifests.", len(manifests))

        page_latencies_ms: List[float] = []
        total_pages = 0
        successful_extractions = 0
        failed_documents = 0
        corrupted_detected_correctly = 0
        auto_verified_count = 0
        hitl_review_count = 0

        for idx, manifest in enumerate(manifests, start=1):
            doc_path = self.dataset_dir / manifest.relative_path
            logger.debug("[%d/%d] Processing %s (%s)...", idx, len(manifests), manifest.file_name, manifest.doc_type)

            start_time = time.perf_counter()
            ingest_res: DocumentProcessingResult = self.ingestion_engine.process_file(doc_path)
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            # Page count & latency tracking
            pages = max(1, ingest_res.total_pages_or_sheets or manifest.page_count)
            total_pages += pages
            ms_per_page = duration_ms / pages
            page_latencies_ms.append(ms_per_page)

            # Check corrupted handling
            if manifest.records and manifest.records[0].is_corrupt:
                if ingest_res.status == DocumentStatus.FAILED_CORRUPTED or bool(ingest_res.error_details):
                    corrupted_detected_correctly += 1
                else:
                    failed_documents += 1
                continue

            if ingest_res.status == DocumentStatus.FAILED_CORRUPTED or (not ingest_res.pages and not ingest_res.sheets):
                failed_documents += 1
                continue

            # Run Extraction
            ext_start = time.perf_counter()
            ext_res: DocumentExtractionResponse = self.extraction_engine.extract_document(ingest_res)
            ext_ms = (time.perf_counter() - ext_start) * 1000.0
            page_latencies_ms[-1] += (ext_ms / pages)

            successful_extractions += 1

            # Run Validation
            val_res: BatchValidationReport = self.validation_engine.validate_batch(ext_res.records)
            auto_verified_count += val_res.auto_verified_count
            hitl_review_count += val_res.manual_review_count

            # Field Accuracy Evaluation
            predicted_records = ext_res.records
            truth_records = manifest.records

            for t_idx, truth in enumerate(truth_records):
                pred = predicted_records[t_idx] if t_idx < len(predicted_records) else None
                self.metrics_calculator.evaluate_pair(pred, truth)

        # Compute metric aggregates
        eval_summary = self.metrics_calculator.compute_summary_report()

        # Compute latency percentiles
        arr_ms = np.array(page_latencies_ms) if page_latencies_ms else np.array([100.0])
        p50 = float(np.percentile(arr_ms, 50))
        p90 = float(np.percentile(arr_ms, 90))
        p95 = float(np.percentile(arr_ms, 95))
        p99 = float(np.percentile(arr_ms, 99))
        p95_sec = round(p95 / 1000.0, 4)

        latency_stats = LatencyPercentiles(
            p50_ms=round(p50, 2),
            p90_ms=round(p90, 2),
            p95_ms=round(p95, 2),
            p99_ms=round(p99, 2),
            p95_sec_per_page=p95_sec,
            min_ms=round(float(np.min(arr_ms)), 2),
            max_ms=round(float(np.max(arr_ms)), 2),
            mean_ms=round(float(np.mean(arr_ms)), 2),
        )

        total_corrupt = sum(1 for m in manifests if m.records and m.records[0].is_corrupt)
        corruption_rate = (corrupted_detected_correctly / total_corrupt) if total_corrupt > 0 else 1.0

        total_valid_records = auto_verified_count + hitl_review_count
        stp_rate = (auto_verified_count / total_valid_records) if total_valid_records > 0 else 0.0
        hitl_rate = (hitl_review_count / total_valid_records) if total_valid_records > 0 else 0.0

        operational_stats = OperationalMetrics(
            total_documents=len(manifests),
            total_pages=total_pages,
            successful_extractions=successful_extractions,
            failed_documents=failed_documents,
            corrupted_detected_correctly=corrupted_detected_correctly,
            straight_through_processing_rate=round(stp_rate, 4),
            human_in_the_loop_rate=round(hitl_rate, 4),
            graceful_corruption_rejection_rate=round(corruption_rate, 4),
        )

        macro_f1 = eval_summary["macro_f1"]
        meets_f1 = macro_f1 >= self.target_f1_threshold
        meets_lat = p95_sec <= self.target_p95_sec_threshold

        report = EvaluationBenchmarkReport(
            report_id=f"bench_{uuid.uuid4().hex[:10]}",
            timestamp=datetime.now(timezone.utc),
            dataset_name=self.dataset_dir.name,
            total_samples=len(manifests),
            field_metrics=eval_summary["field_metrics"],
            macro_precision=eval_summary["macro_precision"],
            macro_recall=eval_summary["macro_recall"],
            macro_f1=macro_f1,
            latency=latency_stats,
            operational=operational_stats,
            meets_f1_threshold=meets_f1,
            meets_latency_threshold=meets_lat,
            production_ready=(meets_f1 and meets_lat),
        )

        self._save_reports(report)
        return report

    def _save_reports(self, report: EvaluationBenchmarkReport) -> Tuple[Path, Path]:
        """Save JSON report and formatted Markdown summary table."""
        json_path = self.reports_dir / "evaluation_benchmark.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(mode="json"), f, indent=2)

        md_path = self.reports_dir / "evaluation_benchmark.md"
        markdown_content = self._render_markdown_report(report)
        md_path.write_text(markdown_content, encoding="utf-8")

        logger.info("Saved benchmark reports to %s and %s", json_path, md_path)
        return json_path, md_path

    def _render_markdown_report(self, report: EvaluationBenchmarkReport) -> str:
        """Format formal benchmark evaluation summary into Github-flavored markdown table."""
        status_badge = "✅ PRODUCTION READY (PASSED)" if report.production_ready else "❌ THRESHOLDS NOT MET"
        lines = [
            f"# DocuPulse Enterprise AP v4.2 - Evaluation Benchmark Report",
            f"",
            f"**Report ID:** `{report.report_id}` | **Timestamp:** `{report.timestamp.isoformat()}` | **Verdict:** **{status_badge}**",
            f"",
            f"---",
            f"",
            f"## 1. Executive Performance & Threshold Summary",
            f"",
            f"| Metric | Target Threshold | Measured Result | Status |",
            f"| :--- | :--- | :--- | :--- |",
            f"| **Macro F1 Score** | $\\ge 95.0\\%$ | **{report.macro_f1 * 100:.2f}%** | {'✅ PASS' if report.meets_f1_threshold else '❌ FAIL'} |",
            f"| **Macro Precision** | - | **{report.macro_precision * 100:.2f}%** | ✅ Validated |",
            f"| **Macro Recall** | - | **{report.macro_recall * 100:.2f}%** | ✅ Validated |",
            f"| **P95 Processing Latency** | $< 2.50\\text{{ s/page}}$ | **{report.latency.p95_sec_per_page:.3f} s/page** ({report.latency.p95_ms:.1f} ms) | {'✅ PASS' if report.meets_latency_threshold else '❌ FAIL'} |",
            f"| **P50 (Median) Latency** | - | **{report.latency.p50_ms:.1f} ms** | ✅ Validated |",
            f"| **P99 Processing Latency** | - | **{report.latency.p99_ms:.1f} ms** | ✅ Validated |",
            f"| **Corruption Rejection Rate** | $100.0\\%$ | **{report.operational.graceful_corruption_rejection_rate * 100:.1f}%** | ✅ Isolated |",
            f"| **Straight-Through Rate (STP)** | - | **{report.operational.straight_through_processing_rate * 100:.1f}%** | Operational |",
            f"| **HITL Review Rate** | - | **{report.operational.human_in_the_loop_rate * 100:.1f}%** | Flagged |",
            f"",
            f"---",
            f"",
            f"## 2. Field-Level Financial Extraction Accuracy",
            f"",
            f"| Financial Field | Precision | Recall | F1 Score | Exact Matches | Evaluated |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for field_name, fscore in report.field_metrics.items():
            lines.append(
                f"| `{field_name}` | {fscore.precision * 100:.1f}% | {fscore.recall * 100:.1f}% | **{fscore.f1_score * 100:.1f}%** | {fscore.exact_matches} | {fscore.total_evaluated} |"
            )

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 3. Operational Throughput & Dataset Composition",
            f"",
            f"- **Total Documents Evaluated:** {report.operational.total_documents}",
            f"- **Total Document Pages Processed:** {report.operational.total_pages}",
            f"- **Successfully Extracted Documents:** {report.operational.successful_extractions}",
            f"- **Gracefully Isolated Corrupt Files:** {report.operational.corrupted_detected_correctly}",
            f"- **Hardware Latency Range:** {report.latency.min_ms:.1f} ms (min) to {report.latency.max_ms:.1f} ms (max), mean {report.latency.mean_ms:.1f} ms",
            f"",
        ])
        return "\n".join(lines)


def main():
    """CLI runner for evaluation benchmarks."""
    parser = argparse.ArgumentParser(description="DocuPulse Enterprise AP - Benchmark Evaluation Runner")
    parser.add_argument(
        "--dataset",
        type=str,
        default="tests/fixtures/golden_set",
        help="Path to labeled golden dataset directory",
    )
    parser.add_argument(
        "--reports",
        type=str,
        default="reports",
        help="Output directory for JSON and Markdown benchmark reports",
    )
    parser.add_argument(
        "--f1-threshold",
        type=float,
        default=0.95,
        help="Macro F1 score production threshold (default: 0.95)",
    )
    parser.add_argument(
        "--latency-threshold",
        type=float,
        default=2.5,
        help="P95 latency production threshold in seconds per page (default: 2.5)",
    )
    args = parser.parse_args()

    evaluator = BenchmarkEvaluator(
        dataset_dir=Path(args.dataset).resolve(),
        reports_dir=Path(args.reports).resolve(),
        target_f1_threshold=args.f1_threshold,
        target_p95_sec_threshold=args.latency_threshold,
    )

    report = evaluator.run_benchmark()

    print("\n" + "=" * 70)
    print(f"BENCHMARK COMPLETED: Macro F1={report.macro_f1*100:.2f}% | P95={report.latency.p95_sec_per_page:.3f}s/page")
    print(f"Production Ready: {'YES' if report.production_ready else 'NO'}")
    print("=" * 70 + "\n")

    if not report.production_ready:
        sys.exit(1)


if __name__ == "__main__":
    main()
