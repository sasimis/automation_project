# app/processors/pdf_invoice_reader.py
from __future__ import annotations
import re
from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dateparse
import fitz  # PyMuPDF
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Reuse the same label sets and helpers from invoice_reader.py
LABELS = {
    "number": ["Αριθμός", "Αριθμος", "Αρ. Τιμολογίου", "Αριθμός Τιμολογίου", "Invoice No", "Invoice #", "Number"],
    "date":   ["Ημερομηνία", "Ημ/νία", "Date", "Invoice Date", "Issue Date"],
    "client": ["Πελάτης", "Επωνυμία", "Customer", "Client"],
    "net":    ["Καθαρή Αξία", "Καθαρή Αξια", "Net", "Subtotal"],
    "vat":    ["ΦΠΑ", "VAT", "Tax"],
    "total":  ["ΣΥΝΟΛΟ", "Σύνολο", "Συνολικό Ποσό", "Total", "Grand Total"],
}

CURRENCY_SIGNS = "€$£"
DEC2 = Decimal("0.01")

def extract_html_from_pdf(pdf_path: Path) -> str:
    """Extract HTML content from PDF if it exists, otherwise extract text"""
    doc = fitz.open(pdf_path)
    html_content = ""
    
    for page in doc:
        # Try to get HTML content if PDF contains it
        text = page.get_text("html")
        if "<html" in text.lower():
            html_content += text
        else:
            # Fallback to regular text extraction
            html_content += f"<pre>{page.get_text('text')}</pre>"
    
    return html_content

def _normalize_numeric_string(s: str) -> str:
    """Keep only digits, dot, comma, sign. Remove currency and spaces."""
    if not s:
        return ""
    s = s.replace("\xa0", " ")
    s = re.sub(fr"[{re.escape(CURRENCY_SIGNS)}\s]", "", s)
    s = re.sub(r"[^0-9,.\-+]", "", s)
    return s

def _smart_decimal(s: str) -> Decimal:
    """Robustly parse numbers in either US (1,234.56) or EU (1.234,56) formats."""
    if not s:
        return Decimal("0.00")

    raw = _normalize_numeric_string(s)
    if not raw:
        return Decimal("0.00")

    last_dot = raw.rfind(".")
    last_com = raw.rfind(",")

    if last_dot != -1 and last_com != -1:
        if last_dot > last_com:
            num = raw.replace(",", "")
        else:
            num = raw.replace(".", "").replace(",", ".")
    elif last_com != -1:
        if len(raw) - last_com - 1 in (1, 2):
            num = raw.replace(".", "").replace(",", ".")
        else:
            num = raw.replace(",", "")
    else:
        num = raw.replace(",", "")

    try:
        return Decimal(num).quantize(DEC2, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0.00")

def _vat_to_rate_or_amount(s: str) -> Tuple[Decimal | None, Decimal | None]:
    """Return (rate_percent, absolute_amount) where one of them is not None."""
    if not s:
        return (None, None)

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", s)
    if m:
        rate = _smart_decimal(m.group(1))
        return (rate, None)

    amt = _smart_decimal(s)
    return (None, amt)

def _label_value(soup: BeautifulSoup, labels: List[str]) -> str:
    """Find value following a label in HTML structure."""
    for lab in labels:
        lab_re = re.compile(rf"\b{re.escape(lab)}\b[:：]?", re.IGNORECASE)

        strong = soup.find(lambda t: t and t.name in ("strong", "b") and lab_re.search(t.get_text(" ", strip=True)))
        if strong:
            parts: List[str] = []
            for sib in strong.next_siblings:
                if getattr(sib, "name", None) in ("strong", "b"):
                    break
                if getattr(sib, "name", None) == "br":
                    if parts:
                        break
                    continue
                txt = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if txt:
                    parts.append(txt)
            if parts:
                return " ".join(parts).strip()

            td = strong.find_parent("td")
            if td:
                nxt = td.find_next_sibling("td")
                if nxt:
                    txt = nxt.get_text(" ", strip=True)
                    if txt:
                        return txt

        td_label = soup.find("td", string=lab_re)
        if td_label:
            nxt = td_label.find_next_sibling("td")
            if nxt:
                txt = nxt.get_text(" ", strip=True)
                if txt:
                    return txt

        node = soup.find(string=lab_re)
        if node and node.parent:
            after = node.replace("\n", " ")
            tail = lab_re.sub("", after, count=1).strip(" :：\t-")
            if tail:
                return tail.strip()
            nxt = node.parent.find_next(string=True)
            if nxt:
                return str(nxt).strip()

    return ""

def _find_date_anywhere(text: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2}|\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b)", text)
    return m.group(1) if m else ""

def _to_iso(d: str) -> str:
    try:
        return dateparse.parse(d, dayfirst=True).date().isoformat()
    except Exception:
        return ""

def _infer_and_fix_amounts(
    net: Decimal,
    vat_rate: Decimal | None,
    vat_amount: Decimal | None,
    total: Decimal,
) -> Tuple[Decimal, Decimal, Decimal]:
    """Ensure we return coherent (net, vat_amount, total)."""
    net = net or Decimal("0.00")
    total = total or Decimal("0.00")
    vat_amount = vat_amount or Decimal("0.00")

    if vat_rate is not None and vat_rate >= Decimal("0"):
        vat_amount = (net * (vat_rate / Decimal("100"))).quantize(DEC2, rounding=ROUND_HALF_UP)
        if total == Decimal("0.00"):
            total = (net + vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)
    else:
        if vat_amount > Decimal("0.00") and total == Decimal("0.00"):
            total = (net + vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)
        elif vat_amount == Decimal("0.00") and total > Decimal("0.00") and net > Decimal("0.00"):
            vat_amount = (total - net).quantize(DEC2, rounding=ROUND_HALF_UP)
        elif vat_amount > Decimal("0.00") and total > Decimal("0.00") and net == Decimal("0.00"):
            net = (total - vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)

    net = net.quantize(DEC2, rounding=ROUND_HALF_UP)
    vat_amount = max(vat_amount, Decimal("0.00")).quantize(DEC2, rounding=ROUND_HALF_UP)
    total = total.quantize(DEC2, rounding=ROUND_HALF_UP)
    return net, vat_amount, total

def parse_pdf_invoice(path: Path) -> Dict[str, object]:
    """Parse PDF invoice that contains HTML content"""
    try:
        # Extract text content from PDF
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        
        # Improved parsing patterns
        invoice_number = re.search(r"(?:Αριθμός|Number|No\.?)\s*:\s*([A-Za-z]+-\d{4}-\d+)", text)
        date_match = re.search(r"(?:Ημερομηνία|Date)\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
        client_match = re.search(r"(?:Πελάτης|Client)\s*:\s*(.+?)(?:\n|Τηλ|ΑΦΜ|$)", text)
        
        # Money values with better pattern matching
        net_match = re.search(r"(?:Καθαρή Αξία|Net)\s*:\s*([€\d\.,]+)", text)
        
        # Improved VAT extraction - looks for both rate and amount
        vat_match = re.search(
            r"(?:ΦΠΑ|VAT)\s*(?:24%)?\s*:\s*([€\d\.,]+)",  # Looks for amount after VAT label
            text
        )
        
        # Alternative pattern if above fails - looks for "ΦΠΑ 24%: 204,00" format
        if not vat_match:
            vat_match = re.search(
                r"(?:ΦΠΑ|VAT)\s*(?:24%)?\s*[:\-]?\s*([€\d\.,]+)",  # More flexible pattern
                text
            )
        
        total_match = re.search(r"(?:ΣΥΝΟΛΟ|Total)\s*:\s*([€\d\.,]+)", text)

        # Extract values with fallbacks
        invoice_number = invoice_number.group(1) if invoice_number else path.stem
        date_iso = _to_iso(date_match.group(1)) if date_match else ""
        client = client_match.group(1).strip() if client_match else ""
        
        # Convert money values
        net_amount = _smart_decimal(net_match.group(1)) if net_match else Decimal("0.00")
        vat_amount = _smart_decimal(vat_match.group(1)) if vat_match else Decimal("0.00")
        total_amount = _smart_decimal(total_match.group(1)) if total_match else Decimal("0.00")
        
        # Calculate VAT if we have net and total but missing VAT
        if vat_amount == Decimal("0.00") and net_amount > Decimal("0.00") and total_amount > Decimal("0.00"):
            vat_amount = total_amount - net_amount

        # Store the actual VAT amount, not the rate
        vat_field = vat_amount

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
            "amount": float(net_amount),
            "vat": float(vat_field),  # This now stores the actual VAT amount (€204.00)
            "total_amount": float(total_amount),
            "invoice_number": invoice_number,
            "priority": "",
            "message": text[:2000],  # Show first 2000 chars as preview
            "is_html": False,
            "html_content": text,
            "vat_percent": "24%" if "24%" in text else ""  # Add VAT rate as separate field if needed
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
            "amount": 0.0,
            "vat": 0.0,
            "total_amount": 0.0,
            "invoice_number": path.stem.replace("invoice_", ""),
            "priority": "",
            "message": f"PDF PARSE ERROR: {str(e)[:200]}",
            "is_html": False,
        }

def parse_pdf_invoices_dir(pdf_invoices_dir: str) -> pd.DataFrame:
    """Parse all PDF invoices in directory"""
    p = Path(pdf_invoices_dir)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame()

    rows: List[Dict[str, object]] = []
    for f in sorted(p.glob("*.pdf")):
        try:
            rows.append(parse_pdf_invoice(f))
        except Exception as e:
            rows.append({
                "type": "INVOICE",
                "source": f.name,
                "source_path": str(f),
                "date": "",
                "client_name": "",
                "email": "",
                "phone": "",
                "company": "",
                "service_interest": "",
                "amount": 0.0,
                "vat": 0.0,
                "total_amount": 0.0,
                "invoice_number": f.stem.replace("invoice_", ""),
                "priority": "",
                "message": f"PDF PARSE ERROR: {str(e)[:200]}",
                "is_html": False,
            })
    
    return pd.DataFrame(rows)

def format_money(amount: float | Decimal) -> str:
    """Format as Euro with two decimals and thousands separators"""
    if isinstance(amount, Decimal):
        amount = float(amount)
    return f"€{amount:,.2f}"