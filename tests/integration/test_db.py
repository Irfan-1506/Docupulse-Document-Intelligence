"""Integration tests for SQLAlchemy database models, repository CRUD, and hybrid vector search."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.schemas import AuditLogEntry, SearchQueryParams
from src.db.models import Base
from src.db.repository import SQLAlchemyRepository
from src.db.search import DeterministicEmbeddingGenerator, HybridSearchEngine
from src.db.session import init_db
from src.extraction.schemas import (
    DocumentExtractionResponse,
    InvoiceExtractionRecord,
    LineItem,
)
from src.ingestion.schemas import (
    DocumentProcessingResult,
    DocumentStatus,
    PageContent,
    SheetContent,
)
from src.validation.schemas import (
    RecordValidationResult,
    ValidationIssue,
    ValidationRuleCode,
    ValidationSeverity,
)


@pytest_asyncio.fixture
async def test_repo() -> AsyncGenerator[SQLAlchemyRepository, None]:
    """Provide an isolated in-memory SQLite database repository for testing."""
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    repo = SQLAlchemyRepository(engine=test_engine)
    yield repo
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_db_document_and_record_persistence(test_repo: SQLAlchemyRepository):
    """Verify persisting full hierarchy: document, pages, sheets, records, validation, and embeddings."""
    doc_id = "doc-alpha-100"
    pages = [
        PageContent(
            page_number=1,
            raw_text="Invoice from Datadog Inc for Cloud Monitoring Total: $3,200.00",
            extraction_method="digital_pdf",
            confidence_score=0.98,
        )
    ]
    sheets = [
        SheetContent(
            sheet_name="Summary",
            row_count=1,
            column_headers=["Vendor", "Total"],
            records=[{"Vendor": "Datadog", "Total": 3200.00}],
            markdown_preview="| Vendor | Total |\n|---|---|\n| Datadog | 3200.00 |",
        )
    ]
    ingestion = DocumentProcessingResult(
        document_id=doc_id,
        file_name="datadog_invoice.pdf",
        file_type="PDF",
        file_hash="hash_alpha_100",
        status=DocumentStatus.SUCCESS,
        total_pages_or_sheets=1,
        pages=pages,
        sheets=sheets,
        full_merged_text="Invoice from Datadog Inc for Cloud Monitoring Total: $3,200.00",
        processing_time_ms=120.5,
    )

    record_id = "rec-datadog-001"
    records = [
        InvoiceExtractionRecord(
            record_id=record_id,
            source_document_id=doc_id,
            page_range_or_sheet="page 1",
            invoice_number="DD-2026-99",
            invoice_date="2026-04-10",
            supplier_name="Datadog Inc",
            customer_name="Starlight Corp",
            subtotal=3000.00,
            tax=200.00,
            total=3200.00,
            currency="USD",
            line_items=[
                LineItem(
                    description="APM Host License",
                    quantity=10.0,
                    unit_price=300.0,
                    line_total=3000.0,
                )
            ],
        )
    ]
    extraction = DocumentExtractionResponse(
        document_id=doc_id,
        file_name="datadog_invoice.pdf",
        total_invoices_detected=1,
        records=records,
        model_used="test_engine",
        processing_time_ms=50.0,
    )

    validations = [
        RecordValidationResult(
            record_id=record_id,
            source_document_id=doc_id,
            invoice_number="DD-2026-99",
            is_valid=True,
            validation_status="AUTO_VERIFIED",
            requires_manual_review=False,
            issues=[],
            computed_metrics={"computed_total": 3200.00},
        )
    ]

    # Save to database
    saved_doc = await test_repo.save_document_results(
        document_id=doc_id,
        file_name="datadog_invoice.pdf",
        file_type="PDF",
        ingestion=ingestion,
        extraction=extraction,
        validations=validations,
    )

    assert saved_doc.document_id == doc_id
    assert saved_doc.file_name == "datadog_invoice.pdf"
    assert saved_doc.ingestion is not None
    assert len(saved_doc.ingestion.pages) == 1
    assert len(saved_doc.ingestion.sheets) == 1
    assert saved_doc.extraction is not None
    assert len(saved_doc.extraction.records) == 1
    assert len(saved_doc.validation) == 1

    # Verify retrieval by record ID
    rec_detail = await test_repo.get_record(record_id)
    assert rec_detail is not None
    assert rec_detail.record.supplier_name == "Datadog Inc"
    assert rec_detail.record.total == 3200.00
    assert rec_detail.validation.validation_status == "AUTO_VERIFIED"


@pytest.mark.asyncio
async def test_db_record_update_and_audit_log(test_repo: SQLAlchemyRepository):
    """Test manual reviewer corrections, audit log persistence, and status override."""
    doc_id = "doc-beta-200"
    rec_id = "rec-beta-002"

    ingestion = DocumentProcessingResult(
        document_id=doc_id,
        file_name="beta.pdf",
        file_type="PDF",
        file_hash="hash_beta_200",
        status=DocumentStatus.SUCCESS,
        total_pages_or_sheets=1,
        full_merged_text="Vendor: Beta Tech Total: 500.00",
        processing_time_ms=45.0,
    )
    records = [
        InvoiceExtractionRecord(
            record_id=rec_id,
            source_document_id=doc_id,
            page_range_or_sheet="page 1",
            invoice_number="BETA-01",
            supplier_name="Beta Tech",
            total=500.00,
            currency="USD",
        )
    ]
    extraction = DocumentExtractionResponse(
        document_id=doc_id,
        file_name="beta.pdf",
        total_invoices_detected=1,
        records=records,
        model_used="test",
        processing_time_ms=50.0,
    )
    validations = [
        RecordValidationResult(
            record_id=rec_id,
            source_document_id=doc_id,
            is_valid=False,
            validation_status="NEEDS_MANUAL_REVIEW",
            requires_manual_review=True,
            issues=[
                ValidationIssue(
                    rule_code=ValidationRuleCode.MISSING_REQUIRED_FIELD,
                    severity=ValidationSeverity.ERROR,
                    field_name="invoice_date",
                    message="Missing mandatory invoice_date",
                )
            ],
        )
    ]

    await test_repo.save_document_results(
        document_id=doc_id,
        file_name="beta.pdf",
        file_type="PDF",
        ingestion=ingestion,
        extraction=extraction,
        validations=validations,
    )

    # Perform manual correction
    updated_rec = records[0].model_copy()
    updated_rec.invoice_date = "2026-05-01"
    updated_rec.total = 550.00

    updated_val = RecordValidationResult(
        record_id=rec_id,
        source_document_id=doc_id,
        invoice_number="BETA-01",
        is_valid=True,
        validation_status="MANUALLY_VERIFIED",
        requires_manual_review=False,
        issues=[],
    )

    audit_entry = AuditLogEntry(
        entry_id="audit-001",
        record_id=rec_id,
        actor="senior_accountant_bob",
        field_changed="total",
        old_value="500.00",
        new_value="550.00",
        notes="Corrected total with tax included",
    )

    updated_detail = await test_repo.update_record(
        record_id=rec_id,
        updated_record=updated_rec,
        updated_validation=updated_val,
        audit_entries=[audit_entry],
    )

    assert updated_detail is not None
    assert updated_detail.record.total == 550.00
    assert updated_detail.record.invoice_date == "2026-05-01"
    assert updated_detail.validation.validation_status == "MANUALLY_VERIFIED"
    assert updated_detail.validation.requires_manual_review is False
    assert len(updated_detail.audit_trail) == 1
    assert updated_detail.audit_trail[0].actor == "senior_accountant_bob"
    assert updated_detail.audit_trail[0].field_changed == "total"


@pytest.mark.asyncio
async def test_hybrid_search_keyword_and_faceted_filters(test_repo: SQLAlchemyRepository):
    """Verify faceted filtering by supplier, total ranges, date ranges, and validation status."""
    # Seed 3 documents
    seed_data = [
        ("doc-1", "rec-1", "INV-101", "Acme Cloud Infrastructure", 5000.00, "2026-01-15", "AUTO_VERIFIED", False),
        ("doc-2", "rec-2", "INV-102", "Cisco Networking Solutions", 12000.00, "2026-02-20", "NEEDS_MANUAL_REVIEW", True),
        ("doc-3", "rec-3", "INV-103", "Acme Office Supplies", 450.00, "2026-03-05", "AUTO_VERIFIED", False),
    ]

    for doc_id, rec_id, inv_num, vendor, total, dt, status, rev in seed_data:
        ing = DocumentProcessingResult(
            document_id=doc_id,
            file_name=f"{doc_id}.pdf",
            file_type="PDF",
            file_hash=f"hash_{doc_id}",
            status=DocumentStatus.SUCCESS,
            total_pages_or_sheets=1,
            full_merged_text=f"Invoice {inv_num} from {vendor} amount ${total}",
            processing_time_ms=25.0,
        )
        recs = [
            InvoiceExtractionRecord(
                record_id=rec_id,
                source_document_id=doc_id,
                page_range_or_sheet="page 1",
                invoice_number=inv_num,
                invoice_date=dt,
                supplier_name=vendor,
                total=total,
                currency="USD",
            )
        ]
        ext = DocumentExtractionResponse(
            document_id=doc_id,
            file_name=f"{doc_id}.pdf",
            total_invoices_detected=1,
            records=recs,
            model_used="test",
            processing_time_ms=50.0,
        )
        vals = [
            RecordValidationResult(
                record_id=rec_id,
                source_document_id=doc_id,
                invoice_number=inv_num,
                is_valid=(not rev),
                validation_status=status, # type: ignore
                requires_manual_review=rev,
                issues=[],
            )
        ]
        await test_repo.save_document_results(doc_id, f"{doc_id}.pdf", "PDF", ing, ext, vals)

    # Filter 1: Supplier contains 'Acme'
    res1 = await test_repo.search_records(SearchQueryParams(supplier="Acme"))
    assert res1.total == 2
    assert all("Acme" in (r.record.supplier_name or "") for r in res1.items)

    # Filter 2: Numeric range total >= 1000 and <= 6000
    res2 = await test_repo.search_records(SearchQueryParams(min_total=1000.0, max_total=6000.0))
    assert res2.total == 1
    assert res2.items[0].record.invoice_number == "INV-101"

    # Filter 3: Review flag filter
    res3 = await test_repo.search_records(SearchQueryParams(requires_manual_review=True))
    assert res3.total == 1
    assert res3.items[0].record.supplier_name == "Cisco Networking Solutions"

    # Filter 4: Keyword full-text query
    res4 = await test_repo.search_records(SearchQueryParams(query="Networking Solutions"))
    assert res4.total >= 1
    assert res4.items[0].record.supplier_name == "Cisco Networking Solutions"


@pytest.mark.asyncio
async def test_semantic_vector_search_and_rrf(test_repo: SQLAlchemyRepository):
    """Test vector similarity and Reciprocal Rank Fusion ranking."""
    # Seed documents with distinctive semantic domains
    domains = [
        ("doc-sem-1", "rec-sem-1", "INV-S1", "Kubernetes Cloud Cluster Hosting", "Kubernetes cluster nodes container cloud devops deployment infrastructure compute"),
        ("doc-sem-2", "rec-sem-2", "INV-S2", "Ergonomic Chairs Furniture Corp", "Ergonomic executive mesh desk chair lumbar support office seating walnut conference table"),
    ]

    for doc_id, rec_id, inv_num, vendor, text_content in domains:
        ing = DocumentProcessingResult(
            document_id=doc_id,
            file_name=f"{doc_id}.pdf",
            file_type="PDF",
            file_hash=f"hash_{doc_id}",
            status=DocumentStatus.SUCCESS,
            total_pages_or_sheets=1,
            full_merged_text=text_content,
            processing_time_ms=30.0,
        )
        recs = [
            InvoiceExtractionRecord(
                record_id=rec_id,
                source_document_id=doc_id,
                page_range_or_sheet="page 1",
                invoice_number=inv_num,
                supplier_name=vendor,
                total=1500.00,
                currency="USD",
            )
        ]
        ext = DocumentExtractionResponse(
            document_id=doc_id,
            file_name=f"{doc_id}.pdf",
            total_invoices_detected=1,
            records=recs,
            model_used="test",
            processing_time_ms=50.0,
        )
        vals = [
            RecordValidationResult(
                record_id=rec_id,
                source_document_id=doc_id,
                is_valid=True,
                validation_status="AUTO_VERIFIED",
                requires_manual_review=False,
                issues=[],
            )
        ]
        await test_repo.save_document_results(doc_id, f"{doc_id}.pdf", "PDF", ing, ext, vals)

    # Semantic query for cloud infrastructure concepts
    search_res = await test_repo.search_records(SearchQueryParams(query="container cluster devops hosting"))
    assert search_res.total >= 1
    # Cloud cluster invoice must rank first
    top_hit = search_res.items[0]
    assert top_hit.record.supplier_name == "Kubernetes Cloud Cluster Hosting"
