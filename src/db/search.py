"""Hybrid keyword (full-text) and semantic vector search engine with Reciprocal Rank Fusion."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import RecordDetailResponse, SearchQueryParams, SearchResponse
from src.db.models import (
    AuditLogModel,
    DocumentEmbeddingModel,
    DocumentModel,
    DocumentPageModel,
    ExtractedRecordModel,
    ValidationResultModel,
)
from src.extraction.schemas import InvoiceExtractionRecord, LineItem
from src.validation.schemas import RecordValidationResult, ValidationIssue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Embedding Generator Strategy
# ---------------------------------------------------------------------------

class BaseEmbeddingGenerator(ABC):
    """Abstract interface for text embedding models."""

    @abstractmethod
    def embed_text(self, text: str) -> List[float]:
        """Generate a normalized embedding vector for input text."""
        pass


class DeterministicEmbeddingGenerator(BaseEmbeddingGenerator):
    """Fast, deterministic pseudo-semantic embedding generator for offline testing and CI/CD.

    Projects token n-grams and semantic concepts onto a normalized N-dimensional hypersphere.
    """

    def __init__(self, dimension: int = 1536) -> None:
        self.dimension = dimension

    def embed_text(self, text: str) -> List[float]:
        if not text:
            return [0.0] * self.dimension

        vec = np.zeros(self.dimension, dtype=np.float32)
        words = re.findall(r"\w+", text.lower())

        for idx, word in enumerate(words):
            # Generate deterministic hashes for word and subwords
            h = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
            pos = h % self.dimension
            weight = 1.0 / (1.0 + 0.05 * idx)
            vec[pos] += weight

            # Bigram feature
            if idx > 0:
                bi = f"{words[idx-1]}_{word}"
                h_bi = int(hashlib.md5(bi.encode("utf-8")).hexdigest(), 16)
                pos_bi = h_bi % self.dimension
                vec[pos_bi] += weight * 1.5

        # L2 normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()


class GeminiEmbeddingGenerator(BaseEmbeddingGenerator):
    """Production embedding generator leveraging Google Gemini embedding models."""

    def __init__(self, model_name: str = "text-embedding-004", dimension: int = 768) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._fallback = DeterministicEmbeddingGenerator(dimension=dimension)
        self._client = None

        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
            except Exception as exc:
                logger.warning(f"Failed to initialize Gemini embedding client: {exc}")

    def embed_text(self, text: str) -> List[float]:
        if not self._client:
            return self._fallback.embed_text(text)
        try:
            resp = self._client.models.embed_content(
                model=self.model_name,
                contents=text,
            )
            embedding = resp.embedding.values
            return list(embedding)
        except Exception as exc:
            logger.warning(f"Gemini embed_content call failed: {exc}. Using fallback.")
            return self._fallback.embed_text(text)


def get_default_embedder() -> BaseEmbeddingGenerator:
    """Retrieve embedding generator based on environment."""
    if os.getenv("GEMINI_API_KEY"):
        return GeminiEmbeddingGenerator()
    return DeterministicEmbeddingGenerator(dimension=1536)


# ---------------------------------------------------------------------------
# Hybrid Search Engine
# ---------------------------------------------------------------------------

class HybridSearchEngine:
    """Enterprise hybrid search engine combining:
      1. PostgreSQL full-text search (tsvector/GIN) or keyword substring matching
      2. Semantic vector cosine similarity (pgvector <=> or in-memory fallback)
      3. Reciprocal Rank Fusion (RRF) reranking
      4. Structured faceted filtering (vendor, date, total, validation status)
    """

    def __init__(
        self,
        embedder: Optional[BaseEmbeddingGenerator] = None,
        rrf_k: int = 60,
        keyword_weight: float = 0.5,
        vector_weight: float = 0.5,
    ) -> None:
        self.embedder = embedder or get_default_embedder()
        self.rrf_k = rrf_k
        self.keyword_weight = keyword_weight
        self.vector_weight = vector_weight

    async def search(
        self,
        session: AsyncSession,
        params: SearchQueryParams,
    ) -> SearchResponse:
        """Execute hybrid keyword + vector search with faceted filters."""
        # 1. Base query joins extracted records with document, validation, and audit logs
        stmt = (
            select(
                ExtractedRecordModel,
                DocumentModel,
                ValidationResultModel,
            )
            .join(DocumentModel, ExtractedRecordModel.document_id == DocumentModel.id)
            .outerjoin(ValidationResultModel, ExtractedRecordModel.record_id == ValidationResultModel.record_id)
        )

        # 2. Apply structured faceted filters
        conditions = []

        if params.supplier:
            conditions.append(ExtractedRecordModel.supplier_name.ilike(f"%{params.supplier}%"))
        if params.customer:
            conditions.append(ExtractedRecordModel.customer_name.ilike(f"%{params.customer}%"))
        if params.invoice_number:
            conditions.append(ExtractedRecordModel.invoice_number.ilike(f"%{params.invoice_number}%"))
        if params.currency:
            conditions.append(ExtractedRecordModel.currency == params.currency.upper())

        if params.min_total is not None:
            conditions.append(ExtractedRecordModel.total >= params.min_total)
        if params.max_total is not None:
            conditions.append(ExtractedRecordModel.total <= params.max_total)

        if params.date_from:
            try:
                from datetime import datetime
                d_from = datetime.strptime(params.date_from, "%Y-%m-%d").date()
                conditions.append(ExtractedRecordModel.invoice_date >= d_from)
            except ValueError:
                pass

        if params.date_to:
            try:
                from datetime import datetime
                d_to = datetime.strptime(params.date_to, "%Y-%m-%d").date()
                conditions.append(ExtractedRecordModel.invoice_date <= d_to)
            except ValueError:
                pass

        if params.validation_status:
            conditions.append(ValidationResultModel.validation_status == params.validation_status)
        if params.requires_manual_review is not None:
            conditions.append(ValidationResultModel.requires_manual_review == params.requires_manual_review)
        if params.file_type:
            conditions.append(DocumentModel.file_type.ilike(params.file_type))

        if conditions:
            stmt = stmt.where(and_(*conditions))

        result = await session.execute(stmt)
        candidates = result.all()

        if not candidates:
            return SearchResponse(
                total=0,
                page=params.page,
                page_size=params.page_size,
                total_pages=1,
                items=[],
            )

        # 3. If query string is absent, return standard sorted records
        if not params.query or not params.query.strip():
            # Sort by created_at desc
            sorted_candidates = sorted(
                candidates,
                key=lambda row: row[0].created_at or 0,
                reverse=True,
            )
            return self._build_paginated_response(sorted_candidates, params)

        query_text = params.query.strip()

        # 4. Keyword Scoring (BM25 / Token overlap ranking)
        keyword_scores: Dict[str, float] = {}
        tokens = [t.lower() for t in re.findall(r"\w+", query_text)]

        for rec_model, doc_model, val_model in candidates:
            rec_id = rec_model.record_id
            rec_text = (
                f"{rec_model.invoice_number or ''} {rec_model.supplier_name or ''} "
                f"{rec_model.customer_name or ''} {rec_model.currency or ''} {rec_model.total or ''} "
                f"{doc_model.file_name or ''} {doc_model.full_merged_text or ''}"
            ).lower()

            # Add line item text
            if rec_model.line_items:
                for item in rec_model.line_items:
                    rec_text += f" {item.get('description', '')} {item.get('sku_or_code', '')}".lower()

            score = 0.0
            for t in tokens:
                count = rec_text.count(t)
                if count > 0:
                    score += 1.0 + math.log(1.0 + count)

            # Exact match boost
            if query_text.lower() in rec_text:
                score += 5.0

            keyword_scores[rec_id] = score

        # 5. Semantic Vector Scoring
        query_vector = self.embedder.embed_text(query_text)
        vector_scores: Dict[str, float] = {}

        # Fetch embeddings for matching candidate documents
        doc_ids = list({doc_model.id for _, doc_model, _ in candidates})
        emb_stmt = select(DocumentEmbeddingModel).where(DocumentEmbeddingModel.document_id.in_(doc_ids))
        emb_res = await session.execute(emb_stmt)
        embeddings = emb_res.scalars().all()

        doc_to_max_sim: Dict[str, float] = {}
        for emb in embeddings:
            sim = self._cosine_similarity(query_vector, emb.embedding)
            if emb.document_id not in doc_to_max_sim or sim > doc_to_max_sim[emb.document_id]:
                doc_to_max_sim[emb.document_id] = sim

        for rec_model, doc_model, _ in candidates:
            # Document embedding similarity
            doc_sim = doc_to_max_sim.get(doc_model.id, 0.0)
            # Record text semantic similarity
            rec_summary = f"{rec_model.supplier_name or ''} {rec_model.customer_name or ''} total: {rec_model.total or ''}"
            rec_vec = self.embedder.embed_text(rec_summary)
            rec_sim = self._cosine_similarity(query_vector, rec_vec)
            vector_scores[rec_model.record_id] = max(doc_sim, rec_sim)

        # 6. Reciprocal Rank Fusion (RRF) Reranking
        # Sort candidates by keyword rank
        keyword_ranked = sorted(
            candidates,
            key=lambda row: keyword_scores.get(row[0].record_id, 0.0),
            reverse=True,
        )
        kw_ranks = {row[0].record_id: rank + 1 for rank, row in enumerate(keyword_ranked)}

        # Sort candidates by vector similarity rank
        vector_ranked = sorted(
            candidates,
            key=lambda row: vector_scores.get(row[0].record_id, 0.0),
            reverse=True,
        )
        vec_ranks = {row[0].record_id: rank + 1 for rank, row in enumerate(vector_ranked)}

        rrf_scores: Dict[str, float] = {}
        for rec_model, _, _ in candidates:
            rid = rec_model.record_id
            kw_r = kw_ranks.get(rid, len(candidates))
            vec_r = vec_ranks.get(rid, len(candidates))

            rrf = (self.keyword_weight / (self.rrf_k + kw_r)) + (self.vector_weight / (self.rrf_k + vec_r))
            rrf_scores[rid] = rrf

        # Filter out candidates with zero keyword score and low vector score (< 0.1)
        ranked_candidates = [
            row for row in candidates
            if keyword_scores.get(row[0].record_id, 0.0) > 0 or vector_scores.get(row[0].record_id, 0.0) > 0.15
        ]

        if not ranked_candidates and candidates:
            # Fallback to candidates sorted by RRF
            ranked_candidates = list(candidates)

        ranked_candidates.sort(
            key=lambda row: rrf_scores.get(row[0].record_id, 0.0),
            reverse=True,
        )

        return self._build_paginated_response(ranked_candidates, params)

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        """Compute cosine similarity between two vector lists."""
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        a = np.array(v1, dtype=np.float32)
        b = np.array(v2, dtype=np.float32)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _build_paginated_response(
        self,
        rows: List[Tuple[ExtractedRecordModel, DocumentModel, Optional[ValidationResultModel]]],
        params: SearchQueryParams,
    ) -> SearchResponse:
        """Construct paginated SearchResponse payload from query rows."""
        total = len(rows)
        page_size = max(1, params.page_size)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = max(1, min(params.page, total_pages)) if total > 0 else 1

        start_idx = (current_page - 1) * page_size
        end_idx = start_idx + page_size
        page_rows = rows[start_idx:end_idx]

        items: List[RecordDetailResponse] = []
        for rec_model, doc_model, val_model in page_rows:
            # Convert DB model to InvoiceExtractionRecord
            rec = InvoiceExtractionRecord(
                record_id=rec_model.record_id,
                source_document_id=rec_model.document_id,
                page_range_or_sheet=rec_model.page_range_or_sheet or "",
                invoice_number=rec_model.invoice_number,
                invoice_date=rec_model.invoice_date.isoformat() if rec_model.invoice_date else None,
                supplier_name=rec_model.supplier_name,
                customer_name=rec_model.customer_name,
                subtotal=float(rec_model.subtotal) if rec_model.subtotal is not None else None,
                tax=float(rec_model.tax) if rec_model.tax is not None else None,
                total=float(rec_model.total) if rec_model.total is not None else None,
                currency=rec_model.currency,
                line_items=[LineItem(**item) for item in (rec_model.line_items or [])],
                field_confidences=rec_model.field_confidences or {},
                uncertain_fields=rec_model.uncertain_fields or [],
                extraction_warnings=rec_model.extraction_warnings or [],
            )

            # Convert DB model to RecordValidationResult
            if val_model:
                val = RecordValidationResult(
                    record_id=rec_model.record_id,
                    source_document_id=rec_model.document_id,
                    invoice_number=rec_model.invoice_number,
                    is_valid=val_model.is_valid,
                    validation_status=val_model.validation_status, # type: ignore
                    requires_manual_review=val_model.requires_manual_review,
                    issues=[ValidationIssue(**iss) for iss in (val_model.issues or [])],
                    cross_verification=val_model.cross_verification_json, # type: ignore
                    computed_metrics=val_model.computed_metrics or {},
                )
            else:
                val = RecordValidationResult(
                    record_id=rec_model.record_id,
                    source_document_id=rec_model.document_id,
                    invoice_number=rec_model.invoice_number,
                    is_valid=True,
                    validation_status="AUTO_VERIFIED",
                    requires_manual_review=False,
                    issues=[],
                )

            # Audit logs
            audit_trail = [
                row
                for row in []  # Populated on demand or by repository
            ]

            items.append(
                RecordDetailResponse(
                    record=rec,
                    validation=val,
                    audit_trail=audit_trail,
                )
            )

        return SearchResponse(
            total=total,
            page=current_page,
            page_size=page_size,
            total_pages=total_pages,
            items=items,
        )
