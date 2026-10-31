"""Seed script to populate SQLite database with realistic AP invoices and master ledger."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl

from src.db.session import init_db
from src.api.store import get_global_store
from src.api.worker import process_document_job
from src.api.schemas import JobStatusResponse, JobStage
import uuid
import time

def build_excel_ledgers():
    base_dir = Path("tests/fixtures/golden_set/documents")
    rows = [
        ["Invoice Number", "Invoice Date", "Supplier", "Customer", "Subtotal", "Tax", "Total", "Currency"],
        ["INV-2026-1001", "2026-03-11", "Acme Cloud Technologies Inc", "Enterprise Global Holdings", 1100.0, 110.0, 1210.0, "USD"],
        ["INV-2026-1002", "2026-03-12", "Vertex Supply Chain Solutions", "Enterprise Global Holdings", 1100.0, 110.0, 1210.0, "USD"],
        ["INV-2026-1003", "2026-03-13", "Apex Logistics Corp", "Enterprise Global Holdings", 1100.0, 110.0, 1210.0, "USD"],
        ["INV-2026-1004", "2026-03-14", "Global Dynamics Semiconductor", "Enterprise Global Holdings", 1100.0, 110.0, 1235.0, "USD"],
        ["INV-2026-1005", "2026-03-15", "Horizon Office Systems Ltd", "Enterprise Global Holdings", 1050.0, 110.0, 1160.0, "USD"],
        ["INV-XLS-5001", "2026-03-22", "Apex Logistics Corp", "Enterprise Global Holdings", 1500.0, 150.0, 1650.0, "USD"],
    ]

    for fname in ["ledger_workbook_001.xlsx", "accounting_master_ledger.xlsx"]:
        fpath = base_dir / fname
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoices_Master"
        for r in rows:
            ws.append(r)
        wb.save(fpath)
        print(f"Generated {fpath}")

    # Also generate CSV
    csv_path = base_dir / "accounting_master_ledger.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(",".join(str(c) for c in r) + "\n")
    print(f"Generated {csv_path}")

async def seed():
    build_excel_ledgers()
    store = get_global_store()
    
    files_to_seed = [
        "accounting_master_ledger.xlsx",
        "invoice_digital_001.pdf",
        "invoice_digital_002.pdf",
        "invoice_digital_003.pdf",
        "invoice_digital_004.pdf",
        "invoice_digital_005.pdf",
        "invoice_digital_006.pdf",
        "multi_invoice_001.pdf",
        "ledger_workbook_001.xlsx",
        "feed_export_001.csv",
    ]

    base_dir = Path("tests/fixtures/golden_set/documents")
    batch_id = str(uuid.uuid4())

    for fname in files_to_seed:
        fpath = base_dir / fname
        if not fpath.exists():
            print(f"File {fname} not found, skipping")
            continue

        job_id = str(uuid.uuid4())
        file_ext = fpath.suffix.lower()
        file_type = file_ext.lstrip(".").upper()

        job = JobStatusResponse(
            job_id=job_id,
            batch_id=batch_id,
            file_name=fname,
            file_type=file_type,
            stage=JobStage.QUEUED,
            progress_percent=0,
            pages_processed=0,
            total_pages=0,
        )
        options = {
            "force_ocr": False,
            "provider_override": "heuristic_fallback",
            "original_filename": fname,
        }
        store.save_job(job=job, file_path=fpath, options=options)
        print(f"Executing pipeline for {fname}...")
        res = await process_document_job(job_id=job_id, file_path=fpath, options=options, store=store)
        print(f"  -> Finished {fname}: stage={res.stage.value if res else 'None'}, doc_id={res.document_id if res else 'None'}")

if __name__ == "__main__":
    asyncio.run(seed())
