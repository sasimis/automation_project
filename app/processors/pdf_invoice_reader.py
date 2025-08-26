from pathlib import Path
from typing import Dict
import re
import fitz  # PyMuPDF
from datetime import datetime
from decimal import Decimal, InvalidOperation
import pandas as pd


def _smart_decimal(val: str) -> Decimal:
    """Convert a string with possible currency symbols and commas to Decimal."""
    try:
        cleaned = val.replace("€", "").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, AttributeError):
        return Decimal("0.00")

def _to_iso(date_str: str) -> str:
    """Convert date string dd/mm/yyyy to ISO yyyy-mm-dd format."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date().isoformat()
    except Exception:
        return ""

def parse_pdf_invoice(path: Path) -> Dict[str, object]:
    """Parse PDF invoice that contains HTML content, filter out non-invoices."""
    invoice_pattern = re.compile(r"(TF-|IN-|INV-)\d{4}-\d+", re.IGNORECASE)
    is_invoice = bool(invoice_pattern.search(path.stem))

    try:
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text() + "\n"

        if is_invoice:
            invoice_number = re.search(r"(?:Αριθμός|Number|No\.?)\s*:\s*([A-Za-z]+-\d{4}-\d+)", text)
            date_match = re.search(r"(?:Ημερομηνία|Date)\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
            client_match = re.search(r"(?:Πελάτης|Client)\s*:\s*(.+?)(?:\n|Τηλ|ΑΦΜ|$)", text)
            net_match = re.search(r"(?:Καθαρή Αξία|Net)\s*:\s*([€\d\.,]+)", text)
            vat_match = re.search(r"(?:ΦΠΑ|VAT)\s*(?:24%)?\s*:\s*([€\d\.,]+)", text)
            if not vat_match:
                vat_match = re.search(r"(?:ΦΠΑ|VAT)\s*(?:24%)?\s*[:\-]?\s*([€\d\.,]+)", text)
            total_match = re.search(r"(?:ΣΥΝΟΛΟ|Total)\s*:\s*([€\d\.,]+)", text)

            invoice_number = invoice_number.group(1) if invoice_number else path.stem
            date_iso = _to_iso(date_match.group(1)) if date_match else ""
            client = client_match.group(1).strip() if client_match else ""
            net_amount = _smart_decimal(net_match.group(1)) if net_match else Decimal("0.00")
            vat_amount = _smart_decimal(vat_match.group(1)) if vat_match else Decimal("0.00")
            total_amount = _smart_decimal(total_match.group(1)) if total_match else Decimal("0.00")
            if vat_amount == Decimal("0.00") and net_amount > Decimal("0.00") and total_amount > Decimal("0.00"):
                vat_amount = total_amount - net_amount
            vat_field = vat_amount
            vat_percent = "24%" if "24%" in text else ""
        else:
            invoice_number = ""
            date_iso = ""
            client = ""
            net_amount = ""
            vat_field = ""
            total_amount = ""
            vat_percent = ""

        return {
            "type": "INVOICE",
            "source": path.name,
            "source_path": str(path),
            "date": date_iso,
            "client_name": client,
            "email": "",
            "phone": "",
            "company": client,
            "service_interest": "",
            "amount": float(net_amount) if net_amount != "" else "",
            "vat": float(vat_field) if vat_field != "" else "",
            "total_amount": float(total_amount) if total_amount != "" else "",
            "invoice_number": invoice_number,
            "priority": "",
            "message": text[:2000],
            "is_html": False,
            "html_content": text,
            "vat_percent": vat_percent
        }

    except Exception as e:
        return {
            "type": "INVOICE",
            "source": path.name,
            "source_path": str(path),
            "date": "",
            "client_name": "",
            "email": "",
            "phone": "",
            "company": "",
            "service_interest": "",
            "amount": "",
            "vat": "",
            "total_amount": "",
            "invoice_number": "",
            "priority": "",
            "message": f"PDF PARSE ERROR: {str(e)[:200]}",
            "is_html": False,
        }

def parse_pdf_invoices_dir(invoices_dir: str):
    """Parse all PDF invoices in a directory and return a DataFrame."""
    p = Path(invoices_dir)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame()
    pdf_files = sorted(p.glob("*.pdf"))
    rows = []
    for pdf_file in pdf_files:
        try:
            rows.append(parse_pdf_invoice(pdf_file))
        except Exception as e:
            rows.append({
                "type": "INVOICE",
                "source": pdf_file.name,
                "source_path": str(pdf_file),
                "date": "",
                "client_name": "",
                "email": "",
                "phone": "",
                "company": "",
                "service_interest": "",
                "amount": "",
                "vat": "",
                "total_amount": "",
                "invoice_number": "",
                "priority": "",
                "message": f"PDF PARSE ERROR: {str(e)[:200]}",
                "is_html": False,
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)
