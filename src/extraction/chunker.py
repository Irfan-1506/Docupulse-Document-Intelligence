"""Multi-page document boundary splitter and structured spreadsheet invoice mapper."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set

from src.ingestion.schemas import DocumentProcessingResult, PageContent, SheetContent
from src.extraction.schemas import (
    InvoiceExtractionRecord,
    LineItem,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-Page Boundary Segment
# ---------------------------------------------------------------------------

class InvoiceSegment:
    """Represents a discrete multi-page boundary segment belonging to a single invoice."""

    def __init__(
        self,
        segment_id: str,
        document_id: str,
        page_numbers: List[int],
        source_reference: str,
        combined_text: str,
        tables: List[Dict[str, Any]],
        min_ocr_confidence: float = 1.0,
        warnings: Optional[List[str]] = None,
    ) -> None:
        self.segment_id = segment_id
        self.document_id = document_id
        self.page_numbers = page_numbers
        self.source_reference = source_reference
        self.combined_text = combined_text
        self.tables = tables
        self.min_ocr_confidence = min_ocr_confidence
        self.warnings = warnings or []


class DocumentBoundarySplitter:
    """Detects multi-invoice boundaries across 1,000+ page high-volume PDF documents."""

    # Regex patterns indicating a new invoice start
    NEW_INVOICE_HEADER_PATTERNS = [
        re.compile(r"\b(?:TAX\s+INVOICE|COMMERCIAL\s+INVOICE|PRO\s*FORMA\s+INVOICE|BILL\s+TO|INVOICE\s+SUMMARY)\b", re.IGNORECASE),
        re.compile(r"\bINVOICE\b", re.IGNORECASE),
    ]

    PAGE_RESET_PATTERNS = [
        re.compile(r"\bpage\s+1\s*(?:of|/)\s*\d+\b", re.IGNORECASE),
        re.compile(r"\bpage\s+1\b", re.IGNORECASE),
    ]

    INVOICE_NUMBER_PATTERN = re.compile(
        r"(?:invoice\s*(?:no|number|#|id)|inv\s*#)[:\s]+([A-Za-z0-9\-_]+)",
        re.IGNORECASE,
    )

    @classmethod
    def _extract_invoice_number_hint(cls, text: str) -> Optional[str]:
        """Extract invoice number string if detected on the page."""
        match = cls.INVOICE_NUMBER_PATTERN.search(text)
        if match:
            return match.group(1).strip()
        return None

    @classmethod
    def _is_new_invoice_start(
        cls,
        page: PageContent,
        current_inv_num: Optional[str],
        pages_in_current_segment: int,
    ) -> bool:
        """Determine whether the given page starts a new invoice."""
        if pages_in_current_segment == 0:
            return True

        # Safety cap: no single invoice segment exceeds 6 pages
        if pages_in_current_segment >= 6:
            return True

        text = page.raw_text[:600]  # Examine header region of page

        # 1. Page numbering reset (e.g. "Page 1 of 3")
        for pat in cls.PAGE_RESET_PATTERNS:
            if pat.search(text):
                return True

        # 2. Check if a new, different invoice number is explicitly stated
        page_inv_hint = cls._extract_invoice_number_hint(text)
        if page_inv_hint and current_inv_num and page_inv_hint != current_inv_num:
            return True

        # 3. Explicit top header like "TAX INVOICE" combined with "Bill To"
        has_invoice_title = any(pat.search(text) for pat in cls.NEW_INVOICE_HEADER_PATTERNS)
        has_bill_to = bool(re.search(r"\b(?:bill\s+to|invoice\s+to|sold\s+to)\b", text, re.IGNORECASE))
        if has_invoice_title and has_bill_to:
            return True

        return False

    @classmethod
    def split_pages_into_invoice_segments(
        cls,
        document_id: str,
        pages: List[PageContent],
    ) -> List[InvoiceSegment]:
        """Split a stream of pages (from 1 to 1,000+ pages) into coherent invoice segments."""
        if not pages:
            return []

        segments: List[InvoiceSegment] = []
        current_pages: List[PageContent] = []
        current_inv_num: Optional[str] = None

        for page in pages:
            if cls._is_new_invoice_start(page, current_inv_num, len(current_pages)):
                if current_pages:
                    segments.append(cls._compile_segment(document_id, current_pages))
                    current_pages = []

                current_inv_num = cls._extract_invoice_number_hint(page.raw_text[:600])

            current_pages.append(page)

        if current_pages:
            segments.append(cls._compile_segment(document_id, current_pages))

        logger.info(
            f"Document {document_id}: segmented {len(pages)} pages into {len(segments)} invoice segments."
        )
        return segments

    @staticmethod
    def _compile_segment(document_id: str, pages: List[PageContent]) -> InvoiceSegment:
        """Compile a list of contiguous pages into an InvoiceSegment."""
        page_nums = [p.page_number for p in pages]
        if len(page_nums) == 1:
            source_ref = f"page {page_nums[0]}"
        else:
            source_ref = f"pages {min(page_nums)}-{max(page_nums)}"

        combined_text_parts: List[str] = []
        all_tables: List[Dict[str, Any]] = []
        all_warnings: List[str] = []
        min_conf = 1.0

        for p in pages:
            combined_text_parts.append(f"--- Page {p.page_number} ---\n{p.raw_text}")
            all_tables.extend(p.tables)
            all_warnings.extend(p.warnings)
            if p.confidence_score < min_conf:
                min_conf = p.confidence_score

        return InvoiceSegment(
            segment_id=str(uuid.uuid4()),
            document_id=document_id,
            page_numbers=page_nums,
            source_reference=source_ref,
            combined_text="\n\n".join(combined_text_parts),
            tables=all_tables,
            min_ocr_confidence=min_conf,
            warnings=all_warnings,
        )


# ---------------------------------------------------------------------------
# Structured Spreadsheet Mapper
# ---------------------------------------------------------------------------

class SpreadsheetInvoiceMapper:
    """Semantic schema mapper for financial spreadsheets and CSV tables."""

    COLUMN_ROLE_PATTERNS = {
        "invoice_number": [
            r"invoice\s*(?:no|number|#|id)", r"inv\s*(?:no|#|id)", r"transaction\s*id",
            r"txn\s*id", r"bill\s*(?:no|id|#)", r"voucher\s*(?:no|#)", r"reference"
        ],
        "invoice_date": [
            r"invoice\s*date", r"inv\s*date", r"cleared\s*date", r"transaction\s*date",
            r"billing\s*date", r"payment\s*date", r"date"
        ],
        "supplier_name": [
            r"supplier\s*name", r"supplier", r"vendor\s*name", r"vendor", r"merchant",
            r"payee", r"issuer", r"biller"
        ],
        "customer_name": [
            r"customer\s*name", r"customer", r"client\s*name", r"client", r"billed\s*to",
            r"recipient", r"buyer", r"account\s*name"
        ],
        "total": [
            r"total\s*amount", r"disbursement\s*amount", r"invoice\s*total", r"grand\s*total",
            r"net\s*total", r"balance", r"total", r"amount", r"market\s*value"
        ],
        "subtotal": [
            r"subtotal", r"sub\s*total", r"taxable\s*amount", r"base\s*amount"
        ],
        "tax": [
            r"tax\s*amount", r"vat\s*amount", r"gst\s*amount", r"sales\s*tax", r"tax", r"vat", r"gst"
        ],
        "currency": [
            r"currency\s*code", r"currency", r"curr", r"ccy"
        ],
    }

    @classmethod
    def match_column_roles(cls, headers: List[str]) -> Dict[str, str]:
        """Map raw column headers to canonical invoice schema fields."""
        role_map: Dict[str, str] = {}
        used_roles: Set[str] = set()

        for header in headers:
            h_clean = header.strip().lower()
            matched_role = None

            for role, patterns in cls.COLUMN_ROLE_PATTERNS.items():
                if role in used_roles and role != "total":
                    continue
                for pat in patterns:
                    if re.search(rf"^{pat}$", h_clean) or re.search(rf"\b{pat}\b", h_clean):
                        matched_role = role
                        break
                if matched_role:
                    break

            if matched_role and matched_role not in used_roles:
                role_map[header] = matched_role
                used_roles.add(matched_role)

        return role_map

    @classmethod
    def extract_invoices_from_sheet(
        cls,
        document_id: str,
        sheet: SheetContent,
    ) -> List[InvoiceExtractionRecord]:
        """Convert tabular spreadsheet rows directly into structured invoice records."""
        if not sheet.records or not sheet.column_headers:
            return []

        role_map = cls.match_column_roles(sheet.column_headers)
        logger.debug(f"Sheet '{sheet.sheet_name}': mapped roles: {role_map}")

        # Check if table represents multi-row invoices (has at least invoice_number or total or supplier)
        has_invoice_signals = any(role in role_map.values() for role in ("invoice_number", "total", "supplier_name"))
        if not has_invoice_signals:
            return []

        records: List[InvoiceExtractionRecord] = []

        for idx, row in enumerate(sheet.records):
            inv_data: Dict[str, Any] = {
                "invoice_number": None,
                "invoice_date": None,
                "supplier_name": None,
                "customer_name": None,
                "subtotal": None,
                "tax": None,
                "total": None,
                "currency": None,
            }
            confidences: Dict[str, float] = {}
            evidence_snippets: Dict[str, str] = {}

            for col_name, raw_val in row.items():
                if col_name not in role_map or raw_val is None or str(raw_val).strip() == "":
                    continue

                role = role_map[col_name]
                evidence_snippets[role] = f"{col_name}: {raw_val}"

                if role in ("total", "subtotal", "tax"):
                    try:
                        clean_num = str(raw_val).replace("$", "").replace(",", "").replace("€", "").replace("£", "").strip()
                        inv_data[role] = float(clean_num)
                        confidences[role] = 1.0
                    except (ValueError, TypeError):
                        inv_data[role] = None
                        confidences[role] = 0.0
                elif role == "currency":
                    c_str = str(raw_val).strip().upper()
                    if len(c_str) == 3:
                        inv_data["currency"] = c_str
                        confidences["currency"] = 1.0
                elif role == "invoice_date":
                    inv_data["invoice_date"] = str(raw_val).strip()
                    confidences["invoice_date"] = 1.0
                else:
                    inv_data[role] = str(raw_val).strip()
                    confidences[role] = 1.0

            # If row has neither invoice_number nor total nor supplier, skip row
            if inv_data["total"] is None and inv_data["invoice_number"] is None and inv_data["supplier_name"] is None:
                continue

            # Check missing required fields
            missing = [
                field for field in ("invoice_number", "invoice_date", "total", "supplier_name")
                if inv_data.get(field) is None
            ]

            rec = InvoiceExtractionRecord(
                record_id=str(uuid.uuid4()),
                source_document_id=document_id,
                page_range_or_sheet=f"Sheet: {sheet.sheet_name} (Row {idx + 1})",
                invoice_number=inv_data["invoice_number"],
                invoice_date=inv_data["invoice_date"],
                supplier_name=inv_data["supplier_name"],
                customer_name=inv_data["customer_name"],
                subtotal=inv_data["subtotal"],
                tax=inv_data["tax"],
                total=inv_data["total"],
                currency=inv_data["currency"],
                line_items=[],
                field_confidences=confidences,
                raw_evidence_snippets=evidence_snippets,
                uncertain_fields=[],
                missing_required_fields=missing,
                extraction_warnings=[],
            )
            records.append(rec)

        return records
