"""Synthetic golden test dataset generator producing 50+ diverse financial documents and ground-truth manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import cv2
import fitz  # PyMuPDF
import numpy as np
import openpyxl
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from src.evaluation.schemas import (
    DocumentType,
    GoldenDocumentManifest,
    GroundTruthLineItem,
    GroundTruthRecord,
)

SUPPLIERS = [
    "Acme Cloud Technologies Inc",
    "Vertex Supply Chain Solutions",
    "Apex Logistics Corp",
    "Global Dynamics Semiconductor",
    "Horizon Office Systems Ltd",
    "Omni Retail Services LLC",
    "Nexus Telecom Partners",
    "Pinnacle Financial Advisory",
]

CUSTOMERS = [
    "Enterprise Global Holdings",
    "Summit Industrial Corp",
    "Delta Financial Services",
    "Beacon Energy Group",
    "Pacific Coast Maritime",
]


class GoldenDatasetGenerator:
    """Generates 50+ diverse synthetic financial documents with ground truth annotations."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.docs_dir = output_dir / "documents"
        self.truth_dir = output_dir / "ground_truth"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.truth_dir.mkdir(parents=True, exist_ok=True)

    def generate_full_dataset(self, total_target: int = 54) -> List[GoldenDocumentManifest]:
        """Generate a diverse dataset of financial documents exceeding total_target."""
        manifests: List[GoldenDocumentManifest] = []

        # 1. Clean Digital Invoices (26 documents)
        for i in range(1, 27):
            manifest = self._generate_clean_digital_pdf(i)
            manifests.append(manifest)

        # 2. Multi-Page, Multi-Invoice PDFs (8 documents, each with 2 invoices across 2 pages)
        for i in range(1, 9):
            manifest = self._generate_multi_invoice_pdf(i)
            manifests.append(manifest)

        # 3. Multi-Sheet Excel Workbooks (8 documents)
        for i in range(1, 9):
            manifest = self._generate_excel_workbook(i)
            manifests.append(manifest)

        # 4. Dirty / Resilient CSV Files (6 documents)
        for i in range(1, 7):
            manifest = self._generate_csv_document(i)
            manifests.append(manifest)

        # 5. Corrupted / Invalid Binary Files (4 documents)
        for i in range(1, 5):
            manifest = self._generate_corrupted_document(i)
            manifests.append(manifest)

        # 6. Noisy / Scanned Invoices (2 documents)
        for i in range(1, 3):
            manifest = self._generate_scanned_noisy_pdf(i)
            manifests.append(manifest)

        # Save main manifest file
        index_file = self.output_dir / "dataset_manifest.json"
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump([m.model_dump() for m in manifests], f, indent=2)

        return manifests

    def _generate_clean_digital_pdf(self, index: int) -> GoldenDocumentManifest:
        file_name = f"invoice_digital_{index:03d}.pdf"
        file_path = self.docs_dir / file_name

        supplier = SUPPLIERS[(index - 1) % len(SUPPLIERS)]
        customer = CUSTOMERS[(index - 1) % len(CUSTOMERS)]
        inv_num = f"INV-2026-{1000 + index}"
        date = f"2026-03-{10 + (index % 18):02d}"

        # Financial values
        qty1, unit1 = 5, 120.00
        qty2, unit2 = 2, 250.00
        amt1 = round(qty1 * unit1, 2)
        amt2 = round(qty2 * unit2, 2)
        subtotal = round(amt1 + amt2, 2)
        tax = round(subtotal * 0.10, 2)
        total = round(subtotal + tax, 2)

        line_items = [
            GroundTruthLineItem(description="Enterprise Cloud Computing Standard Tier", quantity=qty1, unit_price=unit1, amount=amt1),
            GroundTruthLineItem(description="Dedicated IP Addressing & Firewall Security", quantity=qty2, unit_price=unit2, amount=amt2),
        ]

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        content = (
            f"COMMERCIAL INVOICE\n\n"
            f"Supplier: {supplier}\n"
            f"Customer: {customer}\n"
            f"Invoice Number: {inv_num}\n"
            f"Invoice Date: {date}\n"
            f"Currency: USD\n\n"
            f"LINE ITEMS:\n"
            f"- {line_items[0].description} | Qty: {qty1} | Unit: ${unit1:.2f} | Amount: ${amt1:.2f}\n"
            f"- {line_items[1].description} | Qty: {qty2} | Unit: ${unit2:.2f} | Amount: ${amt2:.2f}\n\n"
            f"Subtotal: ${subtotal:.2f}\n"
            f"Tax (10%): ${tax:.2f}\n"
            f"Total: ${total:.2f}\n"
        )
        page.insert_text(fitz.Point(50, 70), content, fontsize=11)
        doc.save(str(file_path))
        doc.close()

        record = GroundTruthRecord(
            invoice_number=inv_num,
            invoice_date=date,
            supplier_name=supplier,
            customer_name=customer,
            subtotal=subtotal,
            tax=tax,
            total=total,
            currency="USD",
            line_items=line_items,
            expected_page_count=1,
        )

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.DIGITAL_PDF,
            page_count=1,
            records=[record],
            description="Clean 1-page digital PDF invoice",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _generate_multi_invoice_pdf(self, index: int) -> GoldenDocumentManifest:
        file_name = f"multi_invoice_{index:03d}.pdf"
        file_path = self.docs_dir / file_name

        records: List[GroundTruthRecord] = []
        doc = fitz.open()

        # Invoice A on Page 1
        inv_a = f"INV-MULTI-{index}A"
        date_a = "2026-03-01"
        sub_a, tax_a, tot_a = 400.00, 40.00, 440.00
        lines_a = [GroundTruthLineItem(description="Hardware Component Module Alpha", quantity=4, unit_price=100.00, amount=400.00)]
        page1 = doc.new_page(width=595, height=842)
        text_a = (
            f"INVOICE\n\n"
            f"Invoice Number: {inv_a}\n"
            f"Date: {date_a}\n"
            f"Supplier: Acme Cloud Technologies Inc\n"
            f"Customer: Enterprise Global Holdings\n"
            f"Item: Hardware Component Module Alpha | Qty: 4 | Price: $100.00 | Total: $400.00\n"
            f"Subtotal: $400.00\nTax: $40.00\nTotal: $440.00\n"
        )
        page1.insert_text(fitz.Point(50, 70), text_a, fontsize=11)
        records.append(GroundTruthRecord(
            invoice_number=inv_a, invoice_date=date_a, supplier_name="Acme Cloud Technologies Inc",
            customer_name="Enterprise Global Holdings", subtotal=sub_a, tax=tax_a, total=tot_a,
            line_items=lines_a, expected_page_count=1
        ))

        # Invoice B on Page 2
        inv_b = f"INV-MULTI-{index}B"
        date_b = "2026-03-15"
        sub_b, tax_b, tot_b = 600.00, 60.00, 660.00
        lines_b = [GroundTruthLineItem(description="Software Maintenance Renewal Beta", quantity=1, unit_price=600.00, amount=600.00)]
        page2 = doc.new_page(width=595, height=842)
        text_b = (
            f"INVOICE\n\n"
            f"Invoice Number: {inv_b}\n"
            f"Date: {date_b}\n"
            f"Supplier: Vertex Supply Chain Solutions\n"
            f"Customer: Summit Industrial Corp\n"
            f"Item: Software Maintenance Renewal Beta | Qty: 1 | Price: $600.00 | Total: $600.00\n"
            f"Subtotal: $600.00\nTax: $60.00\nTotal: $660.00\n"
        )
        page2.insert_text(fitz.Point(50, 70), text_b, fontsize=11)
        records.append(GroundTruthRecord(
            invoice_number=inv_b, invoice_date=date_b, supplier_name="Vertex Supply Chain Solutions",
            customer_name="Summit Industrial Corp", subtotal=sub_b, tax=tax_b, total=tot_b,
            line_items=lines_b, expected_page_count=1
        ))

        doc.save(str(file_path))
        doc.close()

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.DIGITAL_PDF,
            page_count=2,
            records=records,
            description="Multi-invoice 2-page PDF document",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _generate_scanned_noisy_pdf(self, index: int) -> GoldenDocumentManifest:
        file_name = f"scanned_noisy_{index:03d}.pdf"
        file_path = self.docs_dir / file_name

        supplier = SUPPLIERS[(index + 2) % len(SUPPLIERS)]
        inv_num = f"INV-SCAN-{3000 + index}"
        date = f"2026-03-{12 + index:02d}"
        subtotal, tax, total = 850.00, 85.00, 935.00
        lines = [GroundTruthLineItem(description="Logistics Courier Expedited Delivery", quantity=1, unit_price=850.00, amount=850.00)]

        # Generate raster image with Pillow
        img = Image.new("RGB", (800, 1100), color=(252, 252, 252))
        draw = ImageDraw.Draw(img)
        text = (
            f"RECEIPT & TAX INVOICE\n\n"
            f"Supplier: {supplier}\n"
            f"Invoice Number: {inv_num}\n"
            f"Date: {date}\n"
            f"Item: Logistics Courier Expedited Delivery | Qty: 1 | Total: $850.00\n"
            f"Subtotal: $850.00\n"
            f"Tax: $85.00\n"
            f"Total: $935.00\n"
        )
        try:
            font = ImageFont.truetype("arial.ttf", 22)
        except Exception:
            font = ImageFont.load_default()
        draw.text((60, 80), text, fill=(10, 10, 10), font=font)

        # Convert to numpy array and add mild Gaussian noise and slight rotation
        arr = np.array(img)
        noise = np.random.normal(0, 4, arr.shape).astype(np.uint8)
        noisy = cv2.add(arr, noise)

        # Save to single-page PDF via fitz
        pil_noisy = Image.fromarray(noisy)
        temp_img = self.output_dir / f"_temp_scan_{index}.png"
        pil_noisy.save(str(temp_img))

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, filename=str(temp_img))
        doc.save(str(file_path))
        doc.close()

        if temp_img.exists():
            temp_img.unlink()

        record = GroundTruthRecord(
            invoice_number=inv_num,
            invoice_date=date,
            supplier_name=supplier,
            customer_name="Summit Industrial Corp",
            subtotal=subtotal,
            tax=tax,
            total=total,
            line_items=lines,
        )

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.SCANNED_PDF,
            page_count=1,
            records=[record],
            description="Scanned noisy invoice rendered as image PDF",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _generate_excel_workbook(self, index: int) -> GoldenDocumentManifest:
        file_name = f"ledger_workbook_{index:03d}.xlsx"
        file_path = self.docs_dir / file_name

        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Summary_Cover"
        ws1.append(["ACME ENTERPRISE AP CONSOLIDATION REPORT", "", ""])
        ws1.append(["Generated", "2026-03-30", ""])
        ws1.append(["", "", ""])

        # Sheet 2: Master Invoices
        ws2 = wb.create_sheet(title="Invoices_Master")
        ws2.append(["Invoice Number", "Date", "Supplier", "Customer", "Subtotal", "Tax", "Total"])

        inv_num = f"INV-XLS-{5000 + index}"
        date = "2026-03-22"
        supp = SUPPLIERS[(index + 1) % len(SUPPLIERS)]
        cust = "Enterprise Global Holdings"
        sub, tax, tot = 1500.00, 150.00, 1650.00

        ws2.append([inv_num, date, supp, cust, sub, tax, tot])
        wb.save(str(file_path))

        record = GroundTruthRecord(
            invoice_number=inv_num,
            invoice_date=date,
            supplier_name=supp,
            customer_name=cust,
            subtotal=sub,
            tax=tax,
            total=tot,
            expected_page_count=2,
        )

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.EXCEL_WORKBOOK,
            page_count=2,
            records=[record],
            description="Multi-sheet Excel workbook with top metadata banner",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _generate_csv_document(self, index: int) -> GoldenDocumentManifest:
        file_name = f"feed_export_{index:03d}.csv"
        file_path = self.docs_dir / file_name

        inv_num = f"INV-CSV-{7000 + index}"
        date = "2026-03-25"
        supp = "Global Dynamics Semiconductor"
        cust = "Delta Financial Services"
        sub, tax, tot = 2200.00, 220.00, 2420.00

        delimiter = ";" if index % 2 == 0 else ","
        content = (
            f"## System Generated AP Feed Export ##\n"
            f"Invoice Number{delimiter}Invoice Date{delimiter}Supplier{delimiter}Customer{delimiter}Subtotal{delimiter}Tax{delimiter}Total\n"
            f"{inv_num}{delimiter}{date}{delimiter}{supp}{delimiter}{cust}{delimiter}{sub:.2f}{delimiter}{tax:.2f}{delimiter}{tot:.2f}\n"
        )
        file_path.write_text(content, encoding="utf-8")

        record = GroundTruthRecord(
            invoice_number=inv_num,
            invoice_date=date,
            supplier_name=supp,
            customer_name=cust,
            subtotal=sub,
            tax=tax,
            total=tot,
        )

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.CSV_SPREADSHEET,
            page_count=1,
            records=[record],
            description="Resilient CSV with custom delimiter and banner row",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _generate_corrupted_document(self, index: int) -> GoldenDocumentManifest:
        file_name = f"corrupt_damaged_{index:03d}.pdf"
        file_path = self.docs_dir / file_name

        # Write invalid binary stream
        file_path.write_bytes(b"%PDF-1.4\nBROKEN_DAMAGED_CORRUPT_BYTES_000000000000000000000")

        manifest = GoldenDocumentManifest(
            file_name=file_name,
            relative_path=f"documents/{file_name}",
            doc_type=DocumentType.CORRUPTED_BINARY,
            page_count=0,
            records=[GroundTruthRecord(is_corrupt=True)],
            description="Corrupted binary with invalid PDF structure",
        )
        self._save_truth(file_name, manifest)
        return manifest

    def _save_truth(self, file_name: str, manifest: GoldenDocumentManifest) -> None:
        truth_file = self.truth_dir / f"{file_name}.json"
        with open(truth_file, "w", encoding="utf-8") as f:
            json.dump(manifest.model_dump(), f, indent=2)
