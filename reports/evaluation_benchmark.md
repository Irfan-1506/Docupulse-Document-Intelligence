# DocuPulse Enterprise AP v4.2 - Evaluation Benchmark Report

**Report ID:** `bench_835d417c4d` | **Timestamp:** `2026-10-30T17:07:30.112201+00:00` | **Verdict:** **✅ PRODUCTION READY (PASSED)**

---

## 1. Executive Performance & Threshold Summary

| Metric | Target Threshold | Measured Result | Status |
| :--- | :--- | :--- | :--- |
| **Macro F1 Score** | $\ge 95.0\%$ | **99.16%** | ✅ PASS |
| **Macro Precision** | - | **100.00%** | ✅ Validated |
| **Macro Recall** | - | **98.35%** | ✅ Validated |
| **P95 Processing Latency** | $< 2.50\text{ s/page}$ | **0.158 s/page** (157.6 ms) | ✅ PASS |
| **P50 (Median) Latency** | - | **71.7 ms** | ✅ Validated |
| **P99 Processing Latency** | - | **9295.8 ms** | ✅ Validated |
| **Corruption Rejection Rate** | $100.0\%$ | **100.0%** | ✅ Isolated |
| **Straight-Through Rate (STP)** | - | **72.4%** | Operational |
| **HITL Review Rate** | - | **27.6%** | Flagged |

---

## 2. Field-Level Financial Extraction Accuracy

| Financial Field | Precision | Recall | F1 Score | Exact Matches | Evaluated |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `invoice_number` | 100.0% | 98.3% | **99.1%** | 57 | 58 |
| `invoice_date` | 100.0% | 100.0% | **100.0%** | 58 | 58 |
| `supplier_name` | 100.0% | 100.0% | **100.0%** | 58 | 58 |
| `customer_name` | 100.0% | 96.5% | **98.2%** | 56 | 58 |
| `subtotal` | 100.0% | 98.3% | **99.1%** | 57 | 58 |
| `tax` | 100.0% | 98.3% | **99.1%** | 57 | 58 |
| `total` | 100.0% | 100.0% | **100.0%** | 58 | 58 |
| `line_items` | 100.0% | 95.5% | **97.7%** | 56 | 58 |

---

## 3. Operational Throughput & Dataset Composition

- **Total Documents Evaluated:** 54
- **Total Document Pages Processed:** 70
- **Successfully Extracted Documents:** 50
- **Gracefully Isolated Corrupt Files:** 4
- **Hardware Latency Range:** 1.1 ms (min) to 10878.4 ms (max), mean 407.0 ms
