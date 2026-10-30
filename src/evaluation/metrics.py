"""Evaluation metrics calculation engine using scikit-learn and financial precision rules."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.metrics import f1_score, precision_score, recall_score

from src.evaluation.schemas import FieldEvaluationScore, GroundTruthRecord
from src.extraction.schemas import InvoiceExtractionRecord


def normalize_string(val: Optional[str]) -> str:
    """Normalize string for robust semantic equivalence."""
    if not val:
        return ""
    # Strip punctuation and multiple spaces, lowercase
    cleaned = re.sub(r"[^\w\s]", "", str(val).lower())
    return " ".join(cleaned.split())


def numbers_match(val1: Optional[float], val2: Optional[float], tolerance: float = 0.02) -> bool:
    """Check if two financial float values match within specified currency tolerance."""
    if val1 is None and val2 is None:
        return True
    if val1 is None or val2 is None:
        return False
    return abs(float(val1) - float(val2)) <= tolerance


def _get_val(obj: Any) -> Any:
    """Helper to unwrap either raw values or ExtractedField envelopes."""
    if obj is None:
        return None
    if hasattr(obj, "value"):
        return obj.value
    return obj


class BenchmarkMetricsCalculator:
    """Calculates field-level precision, recall, and F1 scores across predicted and ground-truth records."""

    EVALUATED_FIELDS = [
        "invoice_number",
        "invoice_date",
        "supplier_name",
        "customer_name",
        "subtotal",
        "tax",
        "total",
        "line_items",
    ]

    def __init__(self):
        self.field_stats: Dict[str, Dict[str, int]] = {
            f: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "exact": 0, "total": 0}
            for f in self.EVALUATED_FIELDS
        }
        # Binary vectors for scikit-learn validation
        self.y_true_vectors: Dict[str, List[int]] = {f: [] for f in self.EVALUATED_FIELDS}
        self.y_pred_vectors: Dict[str, List[int]] = {f: [] for f in self.EVALUATED_FIELDS}

    def evaluate_pair(self, predicted: Optional[InvoiceExtractionRecord], truth: GroundTruthRecord) -> None:
        """Evaluate a single predicted record against its ground truth."""
        # 1. Invoice Number
        p_inv = _get_val(predicted.invoice_number) if predicted else None
        t_inv = truth.invoice_number
        self._record_field_match("invoice_number", p_inv, t_inv, is_string=True)

        # 2. Invoice Date
        p_date = _get_val(predicted.invoice_date) if predicted else None
        t_date = truth.invoice_date
        self._record_field_match("invoice_date", p_date, t_date, is_string=True)

        # 3. Supplier Name
        p_supp = _get_val(predicted.supplier_name) if predicted else None
        t_supp = truth.supplier_name
        self._record_field_match("supplier_name", p_supp, t_supp, is_string=True, fuzzy=True)

        # 4. Customer Name
        p_cust = _get_val(predicted.customer_name) if predicted else None
        t_cust = truth.customer_name
        self._record_field_match("customer_name", p_cust, t_cust, is_string=True, fuzzy=True)

        # 5. Subtotal
        p_sub = _get_val(predicted.subtotal) if predicted else None
        t_sub = truth.subtotal
        self._record_numeric_match("subtotal", p_sub, t_sub)

        # 6. Tax
        p_tax = _get_val(predicted.tax) if predicted else None
        t_tax = truth.tax
        self._record_numeric_match("tax", p_tax, t_tax)

        # 7. Total
        p_tot = _get_val(predicted.total) if predicted else None
        t_tot = truth.total
        self._record_numeric_match("total", p_tot, t_tot)

        # 8. Line Items
        p_lines = [item.model_dump() for item in predicted.line_items] if predicted and predicted.line_items else []
        t_lines = [item.model_dump() for item in truth.line_items]
        self._record_line_items_match("line_items", p_lines, t_lines)

    def _record_field_match(
        self,
        field: str,
        pred_val: Optional[str],
        truth_val: Optional[str],
        is_string: bool = True,
        fuzzy: bool = False,
    ) -> None:
        stats = self.field_stats[field]
        stats["total"] += 1

        has_truth = bool(truth_val and truth_val.strip())
        has_pred = bool(pred_val and str(pred_val).strip())

        if has_truth and has_pred:
            if is_string:
                n_pred = normalize_string(str(pred_val))
                n_truth = normalize_string(str(truth_val))
                matches = (n_pred == n_truth) or (fuzzy and (n_pred in n_truth or n_truth in n_pred))
            else:
                matches = pred_val == truth_val

            if matches:
                stats["tp"] += 1
                stats["exact"] += 1
                self.y_true_vectors[field].append(1)
                self.y_pred_vectors[field].append(1)
            else:
                stats["fp"] += 1
                stats["fn"] += 1
                self.y_true_vectors[field].append(1)
                self.y_pred_vectors[field].append(0)
        elif has_truth and not has_pred:
            stats["fn"] += 1
            self.y_true_vectors[field].append(1)
            self.y_pred_vectors[field].append(0)
        elif not has_truth and has_pred:
            stats["fp"] += 1
            self.y_true_vectors[field].append(0)
            self.y_pred_vectors[field].append(1)
        else:
            stats["tn"] += 1
            stats["exact"] += 1
            self.y_true_vectors[field].append(0)
            self.y_pred_vectors[field].append(0)

    def _record_numeric_match(
        self, field: str, pred_val: Optional[float], truth_val: Optional[float]
    ) -> None:
        stats = self.field_stats[field]
        stats["total"] += 1

        has_truth = truth_val is not None
        has_pred = pred_val is not None

        if has_truth and has_pred:
            if numbers_match(pred_val, truth_val):
                stats["tp"] += 1
                stats["exact"] += 1
                self.y_true_vectors[field].append(1)
                self.y_pred_vectors[field].append(1)
            else:
                stats["fp"] += 1
                stats["fn"] += 1
                self.y_true_vectors[field].append(1)
                self.y_pred_vectors[field].append(0)
        elif has_truth and not has_pred:
            stats["fn"] += 1
            self.y_true_vectors[field].append(1)
            self.y_pred_vectors[field].append(0)
        elif not has_truth and has_pred:
            stats["fp"] += 1
            self.y_true_vectors[field].append(0)
            self.y_pred_vectors[field].append(1)
        else:
            stats["tn"] += 1
            stats["exact"] += 1
            self.y_true_vectors[field].append(0)
            self.y_pred_vectors[field].append(0)

    def _record_line_items_match(
        self, field: str, pred_lines: List[Dict[str, Any]], truth_lines: List[Dict[str, Any]]
    ) -> None:
        stats = self.field_stats[field]
        stats["total"] += 1

        if not truth_lines and not pred_lines:
            stats["tn"] += 1
            stats["exact"] += 1
            self.y_true_vectors[field].append(0)
            self.y_pred_vectors[field].append(0)
            return

        if bool(truth_lines) != bool(pred_lines):
            if truth_lines:
                stats["fn"] += 1
                self.y_true_vectors[field].append(1)
                self.y_pred_vectors[field].append(0)
            else:
                stats["fp"] += 1
                self.y_true_vectors[field].append(0)
                self.y_pred_vectors[field].append(1)
            return

        # Check line items count and amounts
        t_amounts = sorted([round(float(item.get("amount") or item.get("line_total") or 0.0), 2) for item in truth_lines])
        p_amounts = sorted([round(float(item.get("line_total") or item.get("amount") or 0.0), 2) for item in pred_lines])

        count_matches = len(t_amounts) == len(p_amounts)
        amount_matches = count_matches and all(
            abs(t - p) <= 0.05 for t, p in zip(t_amounts, p_amounts)
        )

        if amount_matches:
            stats["tp"] += 1
            stats["exact"] += 1
            self.y_true_vectors[field].append(1)
            self.y_pred_vectors[field].append(1)
        else:
            stats["fp"] += 1
            stats["fn"] += 1
            self.y_true_vectors[field].append(1)
            self.y_pred_vectors[field].append(0)

    def compute_summary_report(self) -> Dict[str, Any]:
        """Compute scikit-learn validated precision, recall, and macro-F1 metrics."""
        field_metrics: Dict[str, FieldEvaluationScore] = {}
        all_precisions: List[float] = []
        all_recalls: List[float] = []
        all_f1s: List[float] = []

        for field in self.EVALUATED_FIELDS:
            s = self.field_stats[field]
            y_t = self.y_true_vectors[field]
            y_p = self.y_pred_vectors[field]

            if y_t and y_p:
                prec = float(precision_score(y_t, y_p, zero_division=1.0))
                rec = float(recall_score(y_t, y_p, zero_division=1.0))
                f1 = float(f1_score(y_t, y_p, zero_division=1.0))
            else:
                tp, fp, fn = s["tp"], s["fp"], s["fn"]
                prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0
                rec = tp / (tp + fn) if (tp + fn) > 0 else 1.0
                f1 = 2 * (prec * rec) / (prec + rec) if (prec + rec) > 0 else 1.0

            field_metrics[field] = FieldEvaluationScore(
                field_name=field,
                true_positives=s["tp"],
                false_positives=s["fp"],
                false_negatives=s["fn"],
                true_negatives=s["tn"],
                precision=round(prec, 4),
                recall=round(rec, 4),
                f1_score=round(f1, 4),
                exact_matches=s["exact"],
                total_evaluated=s["total"],
            )

            all_precisions.append(prec)
            all_recalls.append(rec)
            all_f1s.append(f1)

        macro_prec = round(float(np.mean(all_precisions)), 4)
        macro_rec = round(float(np.mean(all_recalls)), 4)
        macro_f1 = round(float(np.mean(all_f1s)), 4)

        return {
            "field_metrics": field_metrics,
            "macro_precision": macro_prec,
            "macro_recall": macro_rec,
            "macro_f1": macro_f1,
        }
