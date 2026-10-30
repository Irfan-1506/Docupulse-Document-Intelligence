"""CI/CD automation script executing pytest test suite with coverage and formal benchmark evaluation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.evaluation.evaluator import BenchmarkEvaluator
from src.evaluation.generator import GoldenDatasetGenerator


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run complete test suite, coverage analysis, and benchmark evaluation.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest test suite and run only benchmark evaluation")
    parser.add_argument("--dataset-dir", default=str(PROJECT_ROOT / "tests" / "fixtures" / "golden_set"))
    parser.add_argument("--reports-dir", default=str(PROJECT_ROOT / "reports"))
    parser.add_argument("--f1-threshold", type=float, default=0.95)
    parser.add_argument("--latency-threshold", type=float, default=2.5)
    args = parser.parse_args()

    dataset_path = Path(args.dataset_dir)
    reports_path = Path(args.reports_dir)
    reports_path.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("STEP 1: Checking / Generating Golden Dataset (50+ Diverse Financial Documents)")
    print("=" * 80)
    manifest_file = dataset_path / "dataset_manifest.json"
    if not manifest_file.exists():
        print(f"Generating synthetic golden dataset in: {dataset_path} ...")
        generator = GoldenDatasetGenerator(dataset_path)
        manifests = generator.generate_full_dataset(total_target=52)
        print(f"Generated {len(manifests)} labeled documents with ground-truth manifests.")
    else:
        print(f"Golden dataset verified at: {dataset_path}")

    if not args.skip_tests:
        print("\n" + "=" * 80)
        print("STEP 2: Executing PyTest Automated Test Suite with Code Coverage Analysis")
        print("=" * 80)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "--cov=src",
            "--cov-report=term-missing",
            "-v",
        ]
        test_proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if test_proc.returncode != 0:
            print("\n❌ PyTest suite failed. Aborting benchmark verification.")
            sys.exit(test_proc.returncode)
        print("\n✅ All PyTest suites passed with high test coverage.")

    print("\n" + "=" * 80)
    print("STEP 3: Running Formal Evaluation Benchmark (Macro F1 & P95 Latency)")
    print("=" * 80)
    evaluator = BenchmarkEvaluator(
        dataset_dir=dataset_path,
        reports_dir=reports_path,
        target_f1_threshold=args.f1_threshold,
        target_p95_sec_threshold=args.latency_threshold,
    )

    report = evaluator.run_benchmark()

    print("\n" + "=" * 80)
    print("BENCHMARK EVALUATION SUMMARY")
    print("=" * 80)
    print(f"Total Documents: {report.operational.total_documents}")
    print(f"Total Pages: {report.operational.total_pages}")
    print(f"Macro Precision: {report.macro_precision * 100:.2f}%")
    print(f"Macro Recall:    {report.macro_recall * 100:.2f}%")
    print(f"Macro F1 Score:  {report.macro_f1 * 100:.2f}% (Threshold: >={args.f1_threshold * 100:.1f}%)")
    print(f"P95 Latency:     {report.latency.p95_sec_per_page:.3f} s/page (Threshold: <{args.latency_threshold} s/page)")
    print(f"P50 Latency:     {report.latency.p50_ms:.1f} ms/page")
    print(f"Graceful Rejection Rate: {report.operational.graceful_corruption_rejection_rate * 100:.1f}%")
    ready_text = "[PASS] YES (PASSED)" if report.production_ready else "[FAIL] NO (FAILED)"
    print(f"Production Ready: {ready_text}")
    print(f"JSON Report:     {reports_path / 'evaluation_benchmark.json'}")
    print(f"Markdown Report: {reports_path / 'evaluation_benchmark.md'}")
    print("=" * 80 + "\n")

    if not report.production_ready:
        print("[FAIL] System did not meet production thresholds.")
        sys.exit(1)

    print("[SUCCESS] CI/CD Pipeline Passed Successfully!")
    sys.exit(0)


if __name__ == "__main__":
    main()
