"""Financial record inspection, human-in-the-loop manual corrections, and re-validation."""

from __future__ import annotations

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.schemas import AuditLogEntry, ManualCorrectionRequest, RecordDetailResponse
from src.api.store import DocumentStore, get_global_store
from src.validation.engine import ValidationEngine
from src.validation.schemas import ValidationSeverity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/records", tags=["records"])


def get_store() -> DocumentStore:
    return get_global_store()


@router.get(
    "/{record_id}",
    response_model=RecordDetailResponse,
    summary="Retrieve record details, validation report, and audit history",
)
def get_record_detail(
    record_id: str,
    store: DocumentStore = Depends(get_store),
) -> RecordDetailResponse:
    """Fetch an individual extracted financial record with validation diagnostics and audit logs."""
    detail = store.get_record(record_id)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice record with ID '{record_id}' not found.",
        )
    return detail


@router.patch(
    "/{record_id}/correct",
    response_model=RecordDetailResponse,
    summary="Apply human corrections, generate audit entries, and re-evaluate validation rules",
)
def correct_record(
    record_id: str,
    payload: ManualCorrectionRequest,
    store: DocumentStore = Depends(get_store),
) -> RecordDetailResponse:
    """Apply manual reviewer overrides to an invoice record.

    Automatically triggers:
      - Field mutation audit log creation
      - Immediate financial rules and math re-validation
      - Optional mark_as_verified flag to clear human review requirements
    """
    detail = store.get_record(record_id)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice record with ID '{record_id}' not found.",
        )

    current_record = detail.record
    audit_entries: List[AuditLogEntry] = []

    # Map of editable fields
    fields_to_check = [
        ("invoice_number", payload.invoice_number),
        ("invoice_date", payload.invoice_date),
        ("supplier_name", payload.supplier_name),
        ("customer_name", payload.customer_name),
        ("subtotal", payload.subtotal),
        ("tax", payload.tax),
        ("total", payload.total),
        ("currency", payload.currency),
    ]

    updated_dict = current_record.model_dump()

    for field_name, new_val in fields_to_check:
        if new_val is not None:
            old_val = updated_dict.get(field_name)
            if old_val != new_val:
                updated_dict[field_name] = new_val
                audit_entries.append(
                    AuditLogEntry(
                        entry_id=str(uuid.uuid4()),
                        record_id=record_id,
                        actor=payload.corrected_by,
                        field_changed=field_name,
                        old_value=old_val,
                        new_value=new_val,
                        notes=payload.correction_notes,
                    )
                )

    if payload.line_items is not None:
        old_items = updated_dict.get("line_items", [])
        new_items_dump = [item.model_dump() for item in payload.line_items]
        if old_items != new_items_dump:
            updated_dict["line_items"] = new_items_dump
            audit_entries.append(
                AuditLogEntry(
                    entry_id=str(uuid.uuid4()),
                    record_id=record_id,
                    actor=payload.corrected_by,
                    field_changed="line_items",
                    old_value=f"{len(old_items)} line item(s)",
                    new_value=f"{len(payload.line_items)} line item(s)",
                    notes=payload.correction_notes,
                )
            )

    # Reconstruct updated record
    from src.extraction.schemas import InvoiceExtractionRecord
    updated_record = InvoiceExtractionRecord(**updated_dict)

    # Re-validate with financial engine
    val_engine = ValidationEngine()
    other_candidates = [r for r in store.get_all_records() if r.record_id != record_id]
    new_val = val_engine.validate_record(updated_record, candidate_pool=other_candidates)

    # Handle manual verification override
    if payload.mark_as_verified:
        new_val.requires_manual_review = False
        new_val.validation_status = "MANUALLY_VERIFIED"
        audit_entries.append(
            AuditLogEntry(
                entry_id=str(uuid.uuid4()),
                record_id=record_id,
                actor=payload.corrected_by,
                field_changed="status",
                old_value=detail.validation.validation_status,
                new_value="MANUALLY_VERIFIED",
                notes=payload.correction_notes or "Marked as verified by reviewer",
            )
        )
    elif not any(i.severity == ValidationSeverity.ERROR for i in new_val.issues) and not any(i.severity == ValidationSeverity.WARNING for i in new_val.issues):
        new_val.requires_manual_review = False
        new_val.validation_status = "AUTO_VERIFIED"

    # Persist in store
    saved_detail = store.update_record(
        record_id=record_id,
        updated_record=updated_record,
        updated_validation=new_val,
        audit_entries=audit_entries,
    )

    if not saved_detail:
        raise HTTPException(
            status_code=500,
            detail="Failed to persist updated record.",
        )

    return saved_detail
