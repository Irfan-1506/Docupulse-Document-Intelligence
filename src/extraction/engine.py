"""Enterprise AI extraction engine with modular provider strategy, telemetry, and anti-hallucination guard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.ingestion.schemas import DocumentProcessingResult
from src.extraction.chunker import (
    DocumentBoundarySplitter,
    InvoiceSegment,
    SpreadsheetInvoiceMapper,
)
from src.extraction.grounding import GroundingVerifier
from src.extraction.prompts import (
    DOCUMENT_SEGMENT_USER_PROMPT,
    SPREADSHEET_ROW_USER_PROMPT,
    SYSTEM_EXTRACTION_PROMPT,
)
from src.extraction.schemas import (
    DocumentExtractionResponse,
    ExtractionConfig,
    ExtractionProvider,
    InvoiceExtractionRecord,
    LineItem,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deterministic Heuristic Extractor (Fast, Offline, Zero-Cost)
# ---------------------------------------------------------------------------

class HeuristicExtractor:
    """Deterministic rule- and regex-based extraction engine for local/offline testing."""

    CURRENCY_SYMBOLS: Dict[str, str] = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "৳": "BDT",
        "tk": "BDT",
    }

    ISO_CURRENCIES = {"USD", "EUR", "GBP", "BDT", "CAD", "AUD", "JPY", "CHF", "CNY", "INR"}

    @classmethod
    def _clean_number(cls, raw: str) -> Optional[float]:
        """Convert financial string (e.g. '45,250.00' or '1200') to float."""
        if not raw:
            return None
        # Reject garbled or corrupt strings containing question marks or asterisks
        if any(c in raw for c in ("?", "*", "!", "~")):
            return None
        cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _detect_currency(cls, text: str) -> Optional[str]:
        """Identify ISO-4217 3-letter currency code from symbols or text."""
        # 1. Search for explicit 3-letter ISO code
        for iso in cls.ISO_CURRENCIES:
            if re.search(rf"\b{iso}\b", text, re.IGNORECASE):
                return iso

        # 2. Search for currency symbols
        for sym, code in cls.CURRENCY_SYMBOLS.items():
            if sym in text:
                return code

        return None

    @classmethod
    def _normalize_date(cls, raw_date: str) -> Optional[str]:
        """Attempt to normalize date to YYYY-MM-DD format."""
        if not raw_date:
            return None

        clean = raw_date.strip()

        # Format: YYYY-MM-DD
        if re.match(r"^\d{4}-\d{2}-\d{2}$", clean):
            return clean

        # Try standard common financial date formats
        formats = [
            "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
            "%d-%m-%Y", "%m-%d-%Y",
            "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(clean, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # If unambiguous format could not be verified, return clean raw string
        return clean

    @classmethod
    def extract_from_segment(
        cls,
        segment: InvoiceSegment,
        config: ExtractionConfig,
    ) -> InvoiceExtractionRecord:
        """Extract invoice record deterministically from an InvoiceSegment."""
        text = segment.combined_text
        record_id = str(uuid.uuid4())
        confidences: Dict[str, float] = {}
        snippets: Dict[str, str] = {}
        warnings: List[str] = list(segment.warnings)
        uncertain: List[str] = []

        # 1. Currency
        currency = cls._detect_currency(text)
        if currency:
            confidences["currency"] = 0.95
            snippets["currency"] = f"Detected currency: {currency}"

        # 2. Invoice Number
        inv_match = re.search(
            r"(?:invoice\s*(?:no|number|#|id)|inv\s*#)[:\s]+([A-Za-z0-9\-_]+)",
            text,
            re.IGNORECASE,
        )
        invoice_number: Optional[str] = None
        if inv_match:
            invoice_number = inv_match.group(1).strip()
            confidences["invoice_number"] = 0.95
            snippets["invoice_number"] = inv_match.group(0)

        # 3. Invoice Date
        date_match = re.search(
            r"(?:invoice\s*date|billing\s*date|date)[:\s]+([A-Za-z0-9,/\-\s]+?)(?=\n|$|\s{2,})",
            text,
            re.IGNORECASE,
        )
        invoice_date: Optional[str] = None
        if date_match:
            raw_dt = date_match.group(1).strip()
            invoice_date = cls._normalize_date(raw_dt)
            confidences["invoice_date"] = 0.90
            snippets["invoice_date"] = date_match.group(0)

        # 4. Supplier Name
        supplier_name: Optional[str] = None
        supp_match = re.search(
            r"(?:vendor|supplier|from|billed\s*by|issuer)[:\s]+([A-Za-z0-9&.,\s\-]+?)(?=\n|$|\s{2,})",
            text,
            re.IGNORECASE,
        )
        if supp_match:
            supplier_name = supp_match.group(1).strip()
            confidences["supplier_name"] = 0.90
            snippets["supplier_name"] = supp_match.group(0)
        else:
            # Heuristic: inspect top lines of page 1 before the title
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for line in lines[:4]:
                if any(kw in line.upper() for kw in ("CORP", "INC", "LTD", "LLC", "HOLDINGS", "SOLUTIONS", "GMBH")):
                    supplier_name = line
                    confidences["supplier_name"] = 0.85
                    snippets["supplier_name"] = line
                    break

        # 5. Customer Name
        customer_name: Optional[str] = None
        cust_match = re.search(
            r"(?:bill\s*to|customer|client|sold\s*to|to)[:\s]+([A-Za-z0-9&.,\s\-]+?)(?=\n|$|\s{2,})",
            text,
            re.IGNORECASE,
        )
        if cust_match:
            customer_name = cust_match.group(1).strip()
            confidences["customer_name"] = 0.90
            snippets["customer_name"] = cust_match.group(0)

        # 6. Check for Garbled / OCR corrupted digits in amounts
        for label in ("total", "subtotal", "tax"):
            label_match = re.search(rf"\b{label}\b[^\n\d]*([^\n]+)", text, re.IGNORECASE)
            if label_match:
                val_snippet = label_match.group(1).strip()
                if any(c in val_snippet for c in ("?", "*", "!", "~")):
                    warn = f"Garbled OCR numerical text detected in {label} field: '{val_snippet}'; treated as ambiguous."
                    if warn not in warnings:
                        warnings.append(warn)
                    if label not in uncertain:
                        uncertain.append(label)

        # 7. Subtotal (Zero Fabrication: MUST NOT be computed if missing!)
        subtotal: Optional[float] = None
        if "subtotal" not in uncertain:
            subtotal_matches = list(re.finditer(
                r"\b(?:subtotal|sub\s*total|base\s*amount)\b\s*[:=\-]?\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)(?![\?\*!~a-zA-Z])",
                text,
                re.IGNORECASE,
            ))
            if subtotal_matches:
                subtotal = cls._clean_number(subtotal_matches[-1].group(1))
                if subtotal is not None:
                    confidences["subtotal"] = 0.95
                    snippets["subtotal"] = subtotal_matches[-1].group(0)

        # 8. Tax (Zero Fabrication: MUST NOT be computed if missing!)
        tax: Optional[float] = None
        if "tax" not in uncertain:
            tax_match = re.search(
                r"\b(?:tax\s*amount|sales\s*tax|vat\s*amount|gst\s*amount|tax|vat|gst)\b(?:\s*\(\d+(?:\.\d+)?%\))?\s*[:=\-]?\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)(?![\?\*!~a-zA-Z])",
                text,
                re.IGNORECASE,
            )
            if tax_match:
                tax = cls._clean_number(tax_match.group(1))
                if tax is not None:
                    confidences["tax"] = 0.95
                    snippets["tax"] = tax_match.group(0)

        # 9. Total
        total: Optional[float] = None
        if "total" not in uncertain:
            # Check for explicit summary line (e.g. at line start: "Total: $1,100.00" or "Grand Total:")
            summary_match = re.search(
                r"(?:^|\n)\s*(?:total\s*due|grand\s*total|invoice\s*total|total\s*payable|balance\s*due|total)\b\s*[:=\-]?\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)(?![\?\*!~a-zA-Z])",
                text,
                re.IGNORECASE,
            )
            if summary_match:
                total = cls._clean_number(summary_match.group(1))
                if total is not None:
                    confidences["total"] = 0.95
                    snippets["total"] = summary_match.group(0).strip()
            else:
                total_matches = list(re.finditer(
                    r"\b(?:total\s*due|total\s*amount|grand\s*total|invoice\s*total|total\s*payable|balance\s*due|total)\b\s*[:=\-]?\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)(?![\?\*!~a-zA-Z])",
                    text,
                    re.IGNORECASE,
                ))
                if total_matches:
                    total = cls._clean_number(total_matches[-1].group(1))
                    if total is not None:
                        confidences["total"] = 0.95
                        snippets["total"] = total_matches[-1].group(0)
        else:
            confidences["total"] = 0.0

        # 10. Line Items from Structured Tables or tabular lines
        line_items: List[LineItem] = []
        if segment.tables:
            for tab in segment.tables:
                rows = tab.get("rows", [])
                headers = [h.lower() for h in tab.get("headers", [])]

                desc_key = next((k for k in ("description", "item", "asset category", "service", "name") if any(k in h for h in headers)), None)
                qty_key = next((k for k in ("qty", "quantity", "shares", "units") if any(k in h for h in headers)), None)
                price_key = next((k for k in ("unit price", "price", "avg cost", "rate") if any(k in h for h in headers)), None)
                total_key = next((k for k in ("total", "valuation (usd)", "amount", "line total", "market value") if any(k in h for h in headers)), None)

                for r in rows:
                    desc_val = None
                    qty_val = None
                    price_val = None
                    line_total_val = None

                    for col_name, val in r.items():
                        c_lower = col_name.lower()
                        if desc_key and desc_key in c_lower:
                            desc_val = str(val).strip()
                        elif qty_key and qty_key in c_lower:
                            qty_val = cls._clean_number(str(val))
                        elif price_key and price_key in c_lower:
                            price_val = cls._clean_number(str(val))
                        elif total_key and total_key in c_lower:
                            line_total_val = cls._clean_number(str(val))

                    if desc_val or line_total_val is not None:
                        line_items.append(
                            LineItem(
                                description=desc_val,
                                quantity=qty_val,
                                unit_price=price_val,
                                tax_amount=None,
                                line_total=line_total_val,
                                confidence=0.95 if desc_val and line_total_val is not None else 0.70,
                                raw_evidence=str(r),
                            )
                        )

        # Fallback: extract line items from structured text bullet lines
        if not line_items:
            line_pattern = re.compile(
                r"(?:^|\n)\s*[-•*]?\s*(?:Item:)?\s*([^\n\|]+?)\s*\|\s*Qty:\s*(\d+(?:\.\d+)?)\s*\|\s*(?:Unit|Price):\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9,]+(?:\.[0-9]{2})?)\s*\|\s*(?:Amount|Total):\s*(?:[A-Z]{3}|[\$€£])?\s*([0-9,]+(?:\.[0-9]{2})?)",
                re.IGNORECASE,
            )
            for m in line_pattern.finditer(text):
                desc = m.group(1).strip()
                q = float(m.group(2))
                u = cls._clean_number(m.group(3)) or 0.0
                tot = cls._clean_number(m.group(4)) or 0.0
                line_items.append(
                    LineItem(
                        description=desc,
                        quantity=q,
                        unit_price=u,
                        tax_amount=None,
                        line_total=tot,
                        confidence=0.95,
                        raw_evidence=m.group(0).strip(),
                    )
                )

        # Check missing required fields
        missing = [
            req for req in config.required_fields
            if locals().get(req) is None and getattr(segment, req, None) is None
        ]

        return InvoiceExtractionRecord(
            record_id=record_id,
            source_document_id=segment.document_id,
            page_range_or_sheet=segment.source_reference,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            supplier_name=supplier_name,
            customer_name=customer_name,
            subtotal=subtotal,
            tax=tax,
            total=total,
            currency=currency,
            line_items=line_items,
            field_confidences=confidences,
            raw_evidence_snippets=snippets,
            uncertain_fields=uncertain,
            missing_required_fields=missing,
            extraction_warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Main Orchestrator: AIExtractionEngine
# ---------------------------------------------------------------------------

class AIExtractionEngine:
    """Enterprise AI information extraction engine.

    Features:
    - Modular LLM Provider Strategy (Google Gemini Structured Outputs, OpenAI/Ollama, Heuristic Fallback).
    - Asynchronous page-level parallelism with bounded concurrency.
    - Zero fabrication anti-hallucination post-verification.
    - Direct semantic spreadsheet invoice extraction.
    """

    def __init__(self, config: Optional[ExtractionConfig] = None) -> None:
        self.config = config or ExtractionConfig()
        self.grounding_verifier = GroundingVerifier(config=self.config)
        self._init_llm_client()

    def _init_llm_client(self) -> None:
        """Initialize LLM provider client based on environment and config."""
        env_provider = os.getenv("EXTRACTION_PROVIDER", "").lower().strip()
        if env_provider in ("gemini", "openai", "ollama", "heuristic_fallback"):
            self.config.provider = ExtractionProvider(env_provider)

        env_model = os.getenv("GEMINI_MODEL", "").strip()
        if env_model:
            self.config.model_name = env_model

        self._gemini_client = None
        if self.config.provider == ExtractionProvider.GEMINI:
            gemini_key = os.getenv("GEMINI_API_KEY")
            if gemini_key:
                try:
                    from google import genai
                    self._gemini_client = genai.Client(api_key=gemini_key)
                    logger.info(f"Initialized Google Gemini client with model '{self.config.model_name}'.")
                except Exception as exc:
                    logger.warning(f"Could not initialize google-genai client: {exc}. Will use heuristic fallback.")
            else:
                logger.info("No GEMINI_API_KEY detected in environment; fallback will be used.")

    async def _extract_segment_with_gemini(
        self,
        segment: InvoiceSegment,
    ) -> Tuple[InvoiceExtractionRecord, bool]:
        """Invoke Gemini with Structured Outputs and temperature=0.0.

        Returns (record, fallback_invoked).
        """
        from google.genai import types

        user_prompt = DOCUMENT_SEGMENT_USER_PROMPT.format(
            document_id=segment.document_id,
            page_reference=segment.source_reference,
            document_text=segment.combined_text,
            structured_tables=json.dumps(segment.tables, indent=2) if segment.tables else "None",
        )

        try:
            # Execute Gemini call in threadpool with a 30s timeout
            loop = asyncio.get_running_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._gemini_client.models.generate_content(
                        model=self.config.model_name,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_EXTRACTION_PROMPT,
                            response_mime_type="application/json",
                            response_schema=InvoiceExtractionRecord,
                            temperature=self.config.temperature,
                        ),
                    ),
                ),
                timeout=30.0,
            )

            rec_dict = json.loads(response.text)
            rec_dict["record_id"] = str(uuid.uuid4())
            rec_dict["source_document_id"] = segment.document_id
            rec_dict["page_range_or_sheet"] = segment.source_reference
            return InvoiceExtractionRecord(**rec_dict), False

        except Exception as exc:
            logger.warning(f"Gemini API extraction failed: {exc}. Falling back to heuristic extractor.")
            rec = HeuristicExtractor.extract_from_segment(segment, self.config)
            rec.extraction_warnings.append(f"AI Provider fallback invoked: {str(exc)}")
            return rec, True

    async def _extract_segment_async(
        self,
        segment: InvoiceSegment,
        semaphore: asyncio.Semaphore,
    ) -> Tuple[InvoiceExtractionRecord, str, bool]:
        """Extract an individual segment protected by an async semaphore.

        Returns (verified_record, actual_provider, fallback_invoked).
        """
        async with semaphore:
            actual_provider = self.config.provider.value
            fallback_invoked = False

            if self.config.provider == ExtractionProvider.GEMINI and self._gemini_client:
                record, fallback_invoked = await self._extract_segment_with_gemini(segment)
                if fallback_invoked:
                    actual_provider = ExtractionProvider.HEURISTIC_FALLBACK.value
            else:
                record = HeuristicExtractor.extract_from_segment(segment, self.config)
                if self.config.provider == ExtractionProvider.GEMINI:
                    fallback_invoked = True
                    actual_provider = ExtractionProvider.HEURISTIC_FALLBACK.value
                    record.extraction_warnings.append("Gemini requested but client unavailable; heuristic fallback invoked.")

            # Apply Anti-Hallucination Grounding Guard
            verified_record = self.grounding_verifier.verify_and_harden_record(
                record=record,
                source_text=segment.combined_text,
                ocr_confidence=segment.min_ocr_confidence,
                ocr_warnings=segment.warnings,
            )
            return verified_record, actual_provider, fallback_invoked

    async def extract_document_async(
        self,
        doc_result: DocumentProcessingResult,
    ) -> DocumentExtractionResponse:
        """Asynchronously extract all invoice records from an ingested DocumentProcessingResult."""
        t0 = time.perf_counter()
        records: List[InvoiceExtractionRecord] = []
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        any_fallback = False
        providers_used: set[str] = set()

        # Branch 1: Spreadsheets / Tabular Documents
        if doc_result.sheets:
            for sheet in doc_result.sheets:
                sheet_records = SpreadsheetInvoiceMapper.extract_invoices_from_sheet(
                    document_id=doc_result.document_id,
                    sheet=sheet,
                )
                for rec in sheet_records:
                    v_rec = self.grounding_verifier.verify_and_harden_record(
                        record=rec,
                        source_text=sheet.markdown_preview,
                    )
                    records.append(v_rec)
            providers_used.add("spreadsheet_mapper")

        # Branch 2: Multi-Page Documents (PDFs, Images, Scans)
        elif doc_result.pages:
            segments = DocumentBoundarySplitter.split_pages_into_invoice_segments(
                document_id=doc_result.document_id,
                pages=doc_result.pages,
            )
            tasks = [self._extract_segment_async(seg, semaphore) for seg in segments]
            extracted_tuples = await asyncio.gather(*tasks)

            for rec, p_used, f_invoked in extracted_tuples:
                records.append(rec)
                providers_used.add(p_used)
                if f_invoked:
                    any_fallback = True

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Accurate model provenance
        if ExtractionProvider.GEMINI.value in providers_used and not any_fallback:
            model_used = f"gemini:{self.config.model_name}"
        elif any_fallback:
            model_used = f"heuristic_fallback (fallback from {self.config.provider.value})"
        elif "spreadsheet_mapper" in providers_used:
            model_used = "spreadsheet_mapper:deterministic_v4.3"
        else:
            model_used = f"{self.config.provider.value}:deterministic_v4.3"

        return DocumentExtractionResponse(
            document_id=doc_result.document_id,
            file_name=doc_result.file_name,
            total_invoices_detected=len(records),
            records=list(records),
            model_used=model_used,
            processing_time_ms=round(elapsed_ms, 2),
            telemetry={
                "requested_provider": self.config.provider.value,
                "actual_providers": list(providers_used),
                "fallback_invoked": any_fallback,
                "concurrency": self.config.max_concurrency,
                "verified_grounding": self.config.enable_grounding_verification,
            },
        )

    def extract_document(
        self,
        doc_result: DocumentProcessingResult,
    ) -> DocumentExtractionResponse:
        """Synchronous wrapper for document extraction."""
        return asyncio.run(self.extract_document_async(doc_result))
