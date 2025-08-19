# app/processors/invoices.py
from __future__ import annotations
import re
from pathlib import Path
from typing import List, Tuple, Optional

from bs4 import BeautifulSoup

try:
    import pdfplumber  # preferred
except Exception:
    pdfplumber = None

try:
    import fitz  # PyMuPDF fallback
except Exception:
    fitz = None


def _to_float(val: Optional[str]) -> float:
    if not val:
        return 0.0
    s = str(val).replace("€", "").strip()
    s = s.replace(".", "").replace(",", ".")  # 1.234,56 -> 1234.56
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else 0.0

def _grab(patterns: list[str], text: str) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            return m.group(1).strip()
    return None

def _parse_text(text: str) -> dict:
    number = _grab(
        [
            r"(?:invoice\s*(?:no\.?|number)|αρ\.?\s*τιμολ(?:ογίου)?)[:\s]*([A-Za-z0-9\-/_.]+)",
            r"(?:Τιμολόγιο|ΤΙΜΟΛΟΓΙΟ)[:\s#]*([A-Za-z0-9\-/_.]+)",
        ],
        text,
    )
    date = _grab([r"(\d{2}/\d{2}/\d{4})", r"(\d{4}-\d{2}-\d{2})"], text)
    client = _grab([r"(?:client|πελάτης|bill\s*to)[:\s]*([^\n]+)"], text)
    amount = _to_float(_grab([r"(?:subtotal|net|amount|total|σύνολο|καθαρό)[^\d]*([0-9\.,]+)"], text))
    vat = _to_float(_grab([r"(?:vat|φπα|tax)[^\d]*([0-9\.,]+)"], text)) or 24.0
    return {"number": number, "date": date, "client": client, "amount": amount, "vat": vat}

def _extract_pdf_text(path: Path) -> str:
    if pdfplumber:
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if fitz:
        doc = fitz.open(str(path))
        text = "\n".join(page.get_text() or "" for page in doc)
        doc.close()
        return text
    raise RuntimeError("No PDF extractor available. Install pdfplumber or PyMuPDF.")

def _parse_html_file(path: Path) -> dict:
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    data = _parse_text(text)
    data.update({"source": "invoice", "source_path": str(path), "file_type": "html"})
    return data

def _parse_pdf_file(path: Path) -> dict:
    text = _extract_pdf_text(path)
    data = _parse_text(text)
    data.update({"source": "invoice", "source_path": str(path), "file_type": "pdf"})
    return data

def parse_invoices_dir(invoices_dir: str) -> List[dict]:
    p = Path(invoices_dir)
    files = sorted(
        list(p.glob("*.html")) + list(p.glob("*.htm")) +
        list(p.glob("*.HTML")) + list(p.glob("*.HTM")) +
        list(p.glob("*.pdf"))  + list(p.glob("*.PDF"))
    )
    out: List[dict] = []
    for fp in files:
        try:
            rec = _parse_html_file(fp) if fp.suffix.lower() in {".html", ".htm"} else _parse_pdf_file(fp)
            # keep only records with something meaningful
            if any([rec.get("number"), rec.get("client"), rec.get("amount")]):
                out.append(rec)
        except Exception:
            # even on error, show at least minimal info
            out.append({"number": None, "date": None, "client": None, "amount": 0.0, "vat": 24.0,
                        "source": "invoice", "source_path": str(fp), "file_type": fp.suffix.lower().lstrip(".")})
    return out
