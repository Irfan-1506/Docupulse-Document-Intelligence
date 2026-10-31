"""Multi-sheet spreadsheet (XLSX, XLS) and resilient CSV parsing engine."""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from charset_normalizer import from_bytes

from src.ingestion.schemas import IngestionConfig, SheetContent

logger = logging.getLogger(__name__)


class SpreadsheetProcessor:
    """Robust parser for multi-sheet financial workbooks (XLSX, XLS) and CSV files.

    Features:
    - Automatic detection and stripping of top-of-sheet title banners.
    - Automatic CSV character encoding detection (UTF-8, UTF-8-SIG, Latin-1, CP1252).
    - Robust CSV delimiter detection (comma, semicolon, tab, pipe).
    - Data cleansing: dropping empty rows/columns, type normalization, and NaN handling.
    - Dual representation output: structured JSON records and compact Markdown previews.
    """

    def __init__(self, config: Optional[IngestionConfig] = None) -> None:
        self.config = config or IngestionConfig()

    # -----------------------------------------------------------------------
    # CSV Encoding & Delimiter Auto-Detection
    # -----------------------------------------------------------------------

    @staticmethod
    def detect_encoding(file_path: Path) -> str:
        """Detect file encoding: check UTF-8/UTF-8-SIG first, then charset-normalizer or Latin-1."""
        try:
            with open(file_path, "rb") as f:
                raw_bytes = f.read(131072)  # Read up to 128 KB
        except Exception as exc:
            logger.warning(f"Encoding detection failed for {file_path}: {exc}")
            return "utf-8"

        # 1. Try utf-8
        try:
            raw_bytes.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass

        # 2. Try utf-8-sig
        try:
            raw_bytes.decode("utf-8-sig")
            return "utf-8-sig"
        except UnicodeDecodeError:
            pass

        # 3. Try charset-normalizer, preferring latin-1 / cp1252
        try:
            matches = from_bytes(raw_bytes)
            preferred = {"latin-1", "cp1252", "iso-8859-1"}
            for m in matches:
                enc_norm = m.encoding.lower().replace("_", "-")
                if enc_norm in preferred or m.encoding.lower() in preferred:
                    return m.encoding
            best_match = matches.best()
            if best_match and best_match.encoding:
                return best_match.encoding
        except Exception as exc:
            logger.warning(f"Charset normalizer failed for {file_path}: {exc}")

        return "latin-1"

    @staticmethod
    def detect_csv_delimiter(lines: List[str]) -> str:
        """Detect CSV delimiter (comma, semicolon, tab, pipe) by frequency and sniffer."""
        candidate_sample = "\n".join(lines[:30])
        delimiters = [",", ";", "\t", "|"]

        # Try Python standard csv.Sniffer
        try:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(candidate_sample, delimiters=delimiters)
            if dialect and dialect.delimiter in delimiters:
                return dialect.delimiter
        except Exception:
            pass

        # Heuristic frequency fallback across non-empty lines
        scores: Dict[str, int] = {d: 0 for d in delimiters}
        for line in lines[:30]:
            if not line.strip():
                continue
            for d in delimiters:
                scores[d] += line.count(d)

        # Return delimiter with highest count
        best_del = max(scores, key=lambda d: scores[d])
        return best_del if scores[best_del] > 0 else ","

    # -----------------------------------------------------------------------
    # Header & Banner Detection
    # -----------------------------------------------------------------------

    @staticmethod
    def detect_header_row_index(df_raw: pd.DataFrame, max_scan_rows: int = 15) -> int:
        """Locate real column headers below title banners in financial exports."""
        total_rows = len(df_raw)
        scan_limit = min(max_scan_rows, total_rows)

        best_row_idx = 0
        best_score = -1.0

        for r in range(scan_limit):
            row = df_raw.iloc[r]
            non_nulls = [v for v in row if pd.notna(v) and str(v).strip() != ""]
            count = len(non_nulls)

            if count < 2:
                continue

            # Check percentage of string headers vs purely numeric rows
            str_headers = sum(
                1 for v in non_nulls
                if isinstance(v, str) and not v.strip().replace(".", "", 1).isdigit()
            )
            str_ratio = str_headers / count

            # A good header row has multiple columns, predominantly non-numeric text labels
            score = (count * 1.5) + (str_ratio * 5.0)
            if score > best_score and str_ratio >= 0.4:
                best_score = score
                best_row_idx = r

        return best_row_idx

    @staticmethod
    def standardize_column_names(raw_headers: List[Any]) -> List[str]:
        """Produce clean, unique, non-empty column names."""
        seen: Dict[str, int] = {}
        standardized: List[str] = []

        for i, h in enumerate(raw_headers):
            if pd.isna(h) or str(h).strip() == "" or str(h).startswith("Unnamed:"):
                clean = f"col_{i+1}"
            else:
                clean = str(h).strip().replace("\n", " ").replace("\r", "")

            if clean in seen:
                seen[clean] += 1
                unique_name = f"{clean}_{seen[clean]}"
            else:
                seen[clean] = 1
                unique_name = clean

            standardized.append(unique_name)

        return standardized

    # -----------------------------------------------------------------------
    # Normalization & Output Builders
    # -----------------------------------------------------------------------

    @staticmethod
    def clean_record_value(val: Any) -> Any:
        """Ensure values are clean, JSON-serializable, handle timestamps, and neutralize formula injection."""
        if pd.isna(val):
            return None
        if isinstance(val, (datetime, pd.Timestamp)):
            return val.isoformat()
        if isinstance(val, date):
            return val.isoformat()
        if isinstance(val, (np.integer, int)):
            return int(val)
        if isinstance(val, (np.floating, float)):
            return float(val) if not np.isnan(val) else None
        if isinstance(val, (np.bool_, bool)):
            return bool(val)
        
        s_val = str(val).strip()
        # Neutralize CSV Formula / DDE Execution Injection (CWE-1236)
        if s_val and s_val[0] in ("=", "+", "-", "@", "\t", "\r"):
            return f"'{s_val}"
        return s_val

    @staticmethod
    def build_markdown_preview(df: pd.DataFrame, max_rows: int = 15) -> str:
        """Render a clean GitHub Flavored Markdown table preview."""
        if df.empty:
            return "*(Empty table)*"

        preview_df = df.head(max_rows)
        headers = [str(c) for c in preview_df.columns]

        lines: List[str] = []
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

        for _, row in preview_df.iterrows():
            formatted_cells = [
                str(val) if pd.notna(val) else ""
                for val in row.values
            ]
            lines.append("| " + " | ".join(formatted_cells) + " |")

        if len(df) > max_rows:
            lines.append(f"\n*Showing {max_rows} of {len(df)} total rows.*")

        return "\n".join(lines)

    @staticmethod
    def compute_summary_stats(df: pd.DataFrame) -> Dict[str, Any]:
        """Compute summary statistics for tabular dataset."""
        stats: Dict[str, Any] = {
            "total_rows": int(len(df)),
            "total_columns": int(len(df.columns)),
            "column_names": [str(c) for c in df.columns],
            "data_types": {str(c): str(t) for c, t in df.dtypes.items()},
            "null_counts": {str(c): int(df[c].isna().sum()) for c in df.columns},
            "numeric_columns": [],
            "numeric_metrics": {},
        }

        # Numeric column statistics
        num_cols = df.select_dtypes(include=[np.number]).columns
        stats["numeric_columns"] = [str(c) for c in num_cols]

        for col in num_cols:
            series = df[col].dropna()
            if not series.empty:
                stats["numeric_metrics"][str(col)] = {
                    "min": float(series.min()),
                    "max": float(series.max()),
                    "mean": float(round(series.mean(), 4)),
                    "sum": float(round(series.sum(), 4)),
                }

        return stats

    # -----------------------------------------------------------------------
    # Core Processing Methods
    # -----------------------------------------------------------------------

    def process_dataframe(self, df_raw: pd.DataFrame, sheet_name: str) -> SheetContent:
        """Process, clean, and structure a raw DataFrame into SheetContent."""
        # 1. Drop completely empty rows and columns
        df_clean = df_raw.dropna(how="all").dropna(how="all", axis=1)

        if df_clean.empty:
            return SheetContent(
                sheet_name=sheet_name,
                row_count=0,
                column_headers=[],
                records=[],
                markdown_preview="*(Empty sheet)*",
                summary_stats={"total_rows": 0, "total_columns": 0},
            )

        # 2. Identify header row below any title banners
        header_row_idx = self.detect_header_row_index(df_clean)
        raw_headers = list(df_clean.iloc[header_row_idx].values)
        column_headers = self.standardize_column_names(raw_headers)

        # 3. Slice data below headers
        data_df = df_clean.iloc[header_row_idx + 1:].copy()
        data_df.columns = column_headers

        # 4. Drop rows that became empty after header slicing
        data_df = data_df.dropna(how="all")

        # 5. Coerce numeric columns where applicable
        for col in column_headers:
            converted = pd.to_numeric(data_df[col], errors="coerce")
            if not converted.isna().all() and (data_df[col].isna().sum() == converted.isna().sum()):
                data_df[col] = converted

        # 6. Clean records into JSON-serializable dicts
        records: List[Dict[str, Any]] = []
        for _, row in data_df.iterrows():
            record = {
                col: self.clean_record_value(row[col])
                for col in column_headers
            }
            records.append(record)

        # 7. Generate Markdown preview & summary statistics
        markdown_preview = self.build_markdown_preview(
            data_df, max_rows=self.config.max_spreadsheet_preview_rows
        )
        summary_stats = self.compute_summary_stats(data_df)

        return SheetContent(
            sheet_name=sheet_name,
            row_count=len(records),
            column_headers=column_headers,
            records=records,
            markdown_preview=markdown_preview,
            summary_stats=summary_stats,
        )

    def process_csv(self, file_path: Path) -> List[SheetContent]:
        """Process CSV file with auto-detected encoding and delimiter."""
        encoding = self.detect_encoding(file_path)
        logger.debug(f"Detected encoding '{encoding}' for {file_path.name}")

        try:
            with open(file_path, "r", encoding=encoding, errors="replace") as f:
                raw_lines = [line.rstrip("\r\n") for line in f]
        except Exception:
            # Fallback to latin-1
            encoding = "latin-1"
            with open(file_path, "r", encoding=encoding, errors="replace") as f:
                raw_lines = [line.rstrip("\r\n") for line in f]

        non_empty_lines = [line for line in raw_lines if line.strip()]
        if not non_empty_lines:
            return [
                SheetContent(
                    sheet_name=file_path.stem,
                    row_count=0,
                    column_headers=[],
                    records=[],
                    markdown_preview="*(Empty CSV file)*",
                    summary_stats={"total_rows": 0, "total_columns": 0},
                )
            ]

        delimiter = self.detect_csv_delimiter(non_empty_lines)
        logger.debug(f"Detected delimiter '{delimiter}' for {file_path.name}")

        # Find where table data begins (skip banner lines with fewer delimiters than table)
        start_line_idx = 0
        max_delims = max(line.count(delimiter) for line in non_empty_lines[:20])

        for idx, line in enumerate(non_empty_lines[:20]):
            if line.count(delimiter) >= max(1, max_delims // 2):
                start_line_idx = idx
                break

        filtered_csv_text = "\n".join(non_empty_lines[start_line_idx:])
        df_raw = pd.read_csv(
            io.StringIO(filtered_csv_text),
            sep=delimiter,
            header=None,
            dtype=object,
            on_bad_lines="skip",
        )

        sheet = self.process_dataframe(df_raw, sheet_name=file_path.stem)
        return [sheet]

    def process_excel(self, file_path: Path) -> List[SheetContent]:
        """Process multi-sheet Excel file (.xlsx or .xls)."""
        sheets: List[SheetContent] = []
        engine = "openpyxl" if file_path.suffix.lower() == ".xlsx" else "xlrd"

        excel_file = pd.ExcelFile(file_path, engine=engine)
        sheet_names = excel_file.sheet_names

        logger.info(f"Processing Excel workbook '{file_path.name}' with sheets: {sheet_names}")

        for s_name in sheet_names:
            df_raw = excel_file.parse(sheet_name=s_name, header=None)
            sheet_content = self.process_dataframe(df_raw, sheet_name=str(s_name))
            sheets.append(sheet_content)

        return sheets

    def process(self, file_path: Path) -> List[SheetContent]:
        """Entrypoint for all tabular documents (.xlsx, .xls, .csv)."""
        ext = file_path.suffix.lower()
        if ext == ".csv":
            return self.process_csv(file_path)
        elif ext in (".xlsx", ".xls"):
            return self.process_excel(file_path)
        else:
            raise ValueError(f"Unsupported spreadsheet format: {ext}")
