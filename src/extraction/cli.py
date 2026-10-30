"""CLI entrypoint integrating Task 1 Ingestion and Task 2 AI Extraction."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from src.ingestion.router import DocumentIngestionEngine
from src.extraction.engine import AIExtractionEngine
from src.extraction.schemas import ExtractionConfig, ExtractionProvider

logger = logging.getLogger(__name__)


def main() -> None:
    """CLI runner: python -m src.extraction.cli <file_or_dir> [--output-json <path>] [--provider <provider>]."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="AI Document Intelligence Extraction Engine CLI (Task 1 + Task 2 Pipeline)."
    )
    parser.add_argument(
        "target",
        type=str,
        help="Path to an individual file or directory of financial documents.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to export full JSON extraction responses.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="heuristic_fallback",
        choices=["gemini", "heuristic_fallback", "openai", "ollama"],
        help="Extraction provider (default: heuristic_fallback).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gemini-2.5-flash",
        help="Model name for LLM provider (default: gemini-2.5-flash).",
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

    print(f"\n[*] Initializing End-to-End Ingestion + Extraction Pipeline on {len(file_list)} file(s)...")
    print(f"[*] Provider: {args.provider} | Model: {args.model}")

    # Initialize engines
    ingestion_engine = DocumentIngestionEngine()
    extraction_config = ExtractionConfig(
        provider=ExtractionProvider(args.provider),
        model_name=args.model,
    )
    extraction_engine = AIExtractionEngine(config=extraction_config)

    all_responses = []
    t_start = time.perf_counter()

    print("\n" + "=" * 105)
    print(f"{'STATUS':<12} | {'INVOICES':<9} | {'INV NUMBERS':<20} | {'TOTALS':<18} | {'TIME(ms)':<10} | {'FILE NAME'}")
    print("-" * 105)

    for file_path in file_list:
        # Step 1: Ingest
        ingest_res = ingestion_engine.process_file(file_path)

        if ingest_res.status.value.startswith("FAILED") or ingest_res.status.value.startswith("UNSUPPORTED"):
            print(f"{ingest_res.status.value:<12} | {'0':<9} | {'-':<20} | {'-':<18} | {ingest_res.processing_time_ms:<10.1f} | {file_path.name}")
            continue

        # Step 2: Extract
        extract_res = extraction_engine.extract_document(ingest_res)
        all_responses.append(extract_res)

        inv_nums = ", ".join([r.invoice_number or "N/A" for r in extract_res.records[:2]])
        if len(extract_res.records) > 2:
            inv_nums += f" (+{len(extract_res.records)-2} more)"

        totals = ", ".join([f"{r.currency or ''} {r.total or 0:,.2f}" for r in extract_res.records[:2]])
        if len(extract_res.records) > 2:
            totals += "..."

        total_time = ingest_res.processing_time_ms + extract_res.processing_time_ms
        print(f"{'SUCCESS':<12} | {extract_res.total_invoices_detected:<9} | {inv_nums:<20} | {totals:<18} | {total_time:<10.1f} | {file_path.name}")

    total_batch_time = (time.perf_counter() - t_start) * 1000.0
    print("=" * 105)
    print(f"End-to-end processing completed in {total_batch_time:.2f} ms ({len(file_list)} files).\n")

    if args.output_json:
        out_path = Path(args.output_json).resolve()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([r.model_dump(mode="json") for r in all_responses], f, indent=2)
        print(f"Full JSON extraction results saved to: {out_path}")


if __name__ == "__main__":
    main()
