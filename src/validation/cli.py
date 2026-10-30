"""CLI entrypoint integrating Task 1 Ingestion, Task 2 Extraction, and Task 3 Validation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List

from src.ingestion.router import DocumentIngestionEngine
from src.extraction.engine import AIExtractionEngine
from src.extraction.schemas import ExtractionConfig, ExtractionProvider, InvoiceExtractionRecord
from src.validation.engine import ValidationEngine
from src.validation.schemas import ValidationConfig

logger = logging.getLogger(__name__)


def main() -> None:
    """CLI runner: python -m src.validation.cli <targets...> [--ledger <path>] [--output-json <path>]."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="End-to-End Financial Document Ingestion, Extraction, and Validation CLI."
    )
    parser.add_argument(
        "targets",
        nargs="+",
        type=str,
        help="One or more document file paths or directories.",
    )
    parser.add_argument(
        "--ledger",
        type=str,
        default=None,
        help="Optional path to master spreadsheet ledger (.xlsx or .csv) for cross-verification.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to export full JSON validation report.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="heuristic_fallback",
        choices=["gemini", "heuristic_fallback", "openai", "ollama"],
        help="Extraction provider (default: heuristic_fallback).",
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

    # Discover target files
    from src.ingestion.validators import SUPPORTED_EXTENSIONS
    file_list: List[Path] = []
    for tgt in args.targets:
        p = Path(tgt).resolve()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            file_list.append(p)
        elif p.is_dir():
            file_list.extend(
                [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
            )

    if not file_list:
        print("No supported document files found.", file=sys.stderr)
        sys.exit(1)

    print(f"\n[*] Initializing End-to-End Pipeline (Tasks 1, 2 & 3) on {len(file_list)} file(s)...")

    # Optional Ledger Ingestion for Cross-Verification
    reference_ledger = None
    if args.ledger:
        ledger_path = Path(args.ledger).resolve()
        if ledger_path.exists():
            print(f"[*] Ingesting reference spreadsheet ledger: {ledger_path.name}")
            ingest_engine = DocumentIngestionEngine()
            ledger_ingest = ingest_engine.process_file(ledger_path)
            if ledger_ingest.sheets:
                reference_ledger = ledger_ingest.sheets[0]

    # Initialize Engines
    ingestion_engine = DocumentIngestionEngine()
    extraction_engine = AIExtractionEngine(config=ExtractionConfig(provider=ExtractionProvider(args.provider)))
    validation_engine = ValidationEngine(config=ValidationConfig())

    all_extracted_records: List[InvoiceExtractionRecord] = []
    t_start = time.perf_counter()

    for f_path in file_list:
        # Avoid treating the ledger itself as an invoice input if passed in target
        if args.ledger and f_path == Path(args.ledger).resolve():
            continue

        ingest_res = ingestion_engine.process_file(f_path)
        if ingest_res.status.value.startswith("FAILED") or ingest_res.status.value.startswith("UNSUPPORTED"):
            print(f"[-] Skipped corrupted or unsupported file: {f_path.name}")
            continue

        extract_res = extraction_engine.extract_document(ingest_res)
        all_extracted_records.extend(extract_res.records)

    # Run Task 3 Batch Validation
    report = validation_engine.validate_batch(
        records=all_extracted_records,
        reference_ledger=reference_ledger,
    )
    total_time = (time.perf_counter() - t_start) * 1000.0

    print("\n" + "=" * 115)
    print(f"{'HITL STATUS':<20} | {'INVOICE #':<16} | {'VALID':<7} | {'ISSUES':<8} | {'CROSS-VERIFY':<18} | {'RECORD ID'}")
    print("-" * 115)

    for res in report.results:
        cross_status = res.cross_verification.match_status if res.cross_verification else "N/A"
        issue_count = str(len(res.issues))
        valid_str = "YES" if res.is_valid else "NO"
        inv_str = res.invoice_number or "(Missing)"
        rec_id_str = res.record_id[:16] + "..."
        print(f"{res.validation_status:<20} | {inv_str:<16} | {valid_str:<7} | {issue_count:<8} | {cross_status:<18} | {rec_id_str}")
        for iss in res.issues:
            sev_tag = f"[{iss.severity.value}]"
            print(f"   -> {sev_tag:<9} {iss.rule_code.value}: {iss.message}")

    print("=" * 115)
    print(
        f"Summary: Evaluated: {report.total_records_evaluated} | "
        f"Auto-Verified: {report.auto_verified_count} | "
        f"Manual Review: {report.manual_review_count} | "
        f"Duplicates: {report.duplicate_count} | "
        f"Cross-Verify Matches: {report.cross_verified_exact_matches} | "
        f"Cross-Verify Variances: {report.cross_verified_variances}"
    )
    print(f"Total processing latency: {total_time:.2f} ms.\n")

    if args.output_json:
        out_path = Path(args.output_json).resolve()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(mode="json"), f, indent=2)
        print(f"Full JSON validation report exported to: {out_path}")


if __name__ == "__main__":
    main()
