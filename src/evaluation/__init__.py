"""Evaluation and benchmarking module for Document Intelligence Engine."""

from src.evaluation.evaluator import BenchmarkEvaluator
from src.evaluation.generator import GoldenDatasetGenerator
from src.evaluation.metrics import BenchmarkMetricsCalculator
from src.evaluation.schemas import EvaluationBenchmarkReport, GoldenDocumentManifest

__all__ = [
    "BenchmarkEvaluator",
    "GoldenDatasetGenerator",
    "BenchmarkMetricsCalculator",
    "EvaluationBenchmarkReport",
    "GoldenDocumentManifest",
]
