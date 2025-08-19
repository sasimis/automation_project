# app/processors/invoice_reader.py
from __future__ import annotations
import re
from pathlib import Path
from typing import Dict, List, Tuple
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dateparse


# ---- Label sets used in your invoices (GR + EN) ----
LABELS = {
    "number": ["Αριθμός", "Αριθμος", "Αρ. Τιμολογίου", "Αριθμός Τιμολογίου", "Invoice No", "Invoice #", "Number"],
    "date":   ["Ημερομηνία", "Ημ/νία", "Date", "Invoice Date", "Issue Date"],
    "client": ["Πελάτης", "Επωνυμία", "Customer", "Client"],
    "net":    ["Καθαρή Αξία", "Καθαρή Αξια", "Net", "Subtotal"],
    "vat":    ["ΦΠΑ", "VAT", "Tax"],
    "total":  ["ΣΥΝΟΛΟ", "Σύνολο", "Συνολικό Ποσό", "Total", "Grand Total"],
}

CURRENCY_SIGNS = "€$£"  # extend if you need more

DEC2 = Decimal("0.01")


# ---------- number parsing helpers ----------
def _normalize_numeric_string(s: str) -> str:
    """
    Keep only digits, dot, comma, sign. Remove currency and spaces.
    """
    if not s:
        return ""
    # remove currency signs and spaces/non-breaking spaces
    s = s.replace("\xa0", " ")
    s = re.sub(fr"[{re.escape(CURRENCY_SIGNS)}\s]", "", s)
    # keep only digits . , + -
    s = re.sub(r"[^0-9,.\-+]", "", s)
    return s

def eu_to_float(x):
    """Convert strings like '€1.054,00' | '5.208,00' | '1,234.56' -> float; invalid -> <NA>."""
    if x is None or x == "":
        return pd.NA
    s = str(x)
    raw = re.sub(r"[^\d,.\-+]", "", s)
    if raw.count(",") >= 1 and raw.count(".") >= 1:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")
    elif raw.count(".") >= 1 and raw.count(",") == 0:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except Exception:
        return pd.NA
        


def _smart_decimal(s: str) -> Decimal:
    """
    Robustly parse numbers in either US (1,234.56) or EU (1.234,56) formats.
    Strategy:
      - Strip currency & junk.
      - If both ',' and '.' exist: the RIGHTMOST symbol is the decimal separator.
      - If only ',' exists and it appears in a 'decimal-ish' position, treat it as decimal sep.
      - Otherwise default '.' as decimal sep and remove ',' as thousand sep.
    """
    if not s:
        return Decimal("0.00")

    raw = _normalize_numeric_string(s)
    if not raw:
        return Decimal("0.00")

    # If both separators appear, pick the rightmost as decimal sep
    last_dot = raw.rfind(".")
    last_com = raw.rfind(",")

    if last_dot != -1 and last_com != -1:
        # both exist -> rightmost is decimal sep
        if last_dot > last_com:
            # dot is decimal: remove commas (thousands)
            num = raw.replace(",", "")
        else:
            # comma is decimal: remove dots (thousands), replace comma with dot
            num = raw.replace(".", "").replace(",", ".")
    elif last_com != -1:
        # only comma present -> likely decimal if there are <=2 digits after last comma
        # e.g. 123,45 -> decimal; 1,234 -> thousands (ambiguous but safer to treat as thousands)
        if len(raw) - last_com - 1 in (1, 2):
            num = raw.replace(".", "").replace(",", ".")
        else:
            num = raw.replace(",", "")
    else:
        # only dot or plain digits
        num = raw.replace(",", "")  # just in case

    try:
        return Decimal(num).quantize(DEC2, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0.00")


def _smart_float(s: str) -> float:
    return float(_smart_decimal(s))


def _vat_to_rate_or_amount(s: str) -> Tuple[Decimal | None, Decimal | None]:
    """
    Return (rate_percent, absolute_amount) where one of them is not None.
    - "24%" -> (Decimal('24.00'), None)
    - "€204.00" -> (None, Decimal('204.00'))
    - "ΦΠΑ 24%" -> detects rate
    - "ΦΠΑ: 204,00" -> detects absolute
    """
    if not s:
        return (None, None)

    # Look for a percentage anywhere in the text
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", s)
    if m:
        rate = _smart_decimal(m.group(1))
        return (rate, None)

    # Otherwise treat as amount
    amt = _smart_decimal(s)
    return (None, amt)


# ---------- html label helper ----------
def _label_value(soup: BeautifulSoup, labels: List[str]) -> str:
    """
    Find value following a label such as:
      <strong>Ημερομηνία:</strong> 21/01/2024
    or in tables: <td>Ημερομηνία</td><td>21/01/2024</td>
    Also tolerates variants without colon and bold tags.
    """
    for lab in labels:
        lab_re = re.compile(rf"\b{re.escape(lab)}\b[:：]?", re.IGNORECASE)

        # Case 1: bold/strong label, value in next siblings
        strong = soup.find(lambda t: t and t.name in ("strong", "b") and lab_re.search(t.get_text(" ", strip=True)))
        if strong:
            parts: List[str] = []
            for sib in strong.next_siblings:
                if getattr(sib, "name", None) in ("strong", "b"):
                    break
                if getattr(sib, "name", None) == "br":
                    if parts:  # stop at next line if we already captured something
                        break
                    continue
                txt = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if txt:
                    parts.append(txt)
            if parts:
                return " ".join(parts).strip()

            # Table variant: <td>Label</td><td>VALUE</td>
            td = strong.find_parent("td")
            if td:
                nxt = td.find_next_sibling("td")
                if nxt:
                    txt = nxt.get_text(" ", strip=True)
                    if txt:
                        return txt

        # Case 2: <td>Label</td><td>Value</td> without bold
        td_label = soup.find("td", string=lab_re)
        if td_label:
            nxt = td_label.find_next_sibling("td")
            if nxt:
                txt = nxt.get_text(" ", strip=True)
                if txt:
                    return txt

        # Case 3: plain text node "Label:" followed by text
        node = soup.find(string=lab_re)
        if node and node.parent:
            # value may be in the same tag or next sibling text
            after = node.replace("\n", " ")
            # try grabbing text after the matched label in the same node
            tail = lab_re.sub("", after, count=1).strip(" :：\t-")
            if tail:
                return tail.strip()
            # else fallback to next string
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


# ---------- core ----------
def _infer_and_fix_amounts(
    net: Decimal,
    vat_rate: Decimal | None,
    vat_amount: Decimal | None,
    total: Decimal,
) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Ensure we return coherent (net, vat_amount, total).
    Rules:
      - If vat_rate is given, compute VAT = round(net * rate/100). If total missing, total = net + VAT.
      - If only vat_amount is given:
           * if total missing, total = net + vat_amount
           * if net missing but total given, net = total - vat_amount
      - If only net & total given, compute vat_amount = total - net (>=0).
      - If only one number present, fill missing with 0 where needed.
    """
    # Normalize None -> Decimal("0.00")
    net = net or Decimal("0.00")
    total = total or Decimal("0.00")
    vat_amount = vat_amount or Decimal("0.00")

    if vat_rate is not None and vat_rate >= Decimal("0"):
        # Compute VAT from rate and net
        vat_amount = (net * (vat_rate / Decimal("100"))).quantize(DEC2, rounding=ROUND_HALF_UP)
        if total == Decimal("0.00"):
            total = (net + vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)
    else:
        # No rate, maybe explicit VAT amount
        if vat_amount > Decimal("0.00") and total == Decimal("0.00"):
            total = (net + vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)
        elif vat_amount == Decimal("0.00") and total > Decimal("0.00") and net > Decimal("0.00"):
            vat_amount = (total - net).quantize(DEC2, rounding=ROUND_HALF_UP)
        elif vat_amount > Decimal("0.00") and total > Decimal("0.00") and net == Decimal("0.00"):
            net = (total - vat_amount).quantize(DEC2, rounding=ROUND_HALF_UP)

    # Final guard: keep non-negative and 2 decimals
    net = net.quantize(DEC2, rounding=ROUND_HALF_UP)
    vat_amount = max(vat_amount, Decimal("0.00")).quantize(DEC2, rounding=ROUND_HALF_UP)
    total = total.quantize(DEC2, rounding=ROUND_HALF_UP)
    return net, vat_amount, total


def parse_invoice_html(path: Path) -> Dict[str, object]:
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Invoice number: label or code in filename/text (e.g., TF-2024-001)
    invoice_number = _label_value(soup, LABELS["number"])
    if not invoice_number:
        # Try to find in filename first, then in text
        m = re.search(r"(?:TF|IN|INV)-\d{4}-\d+", path.name)
        if not m:
            m = re.search(r"(?:TF|IN|INV)-\d{4}-\d+", text)
        if m:
            invoice_number = m.group(0)
        else:
            invoice_number = path.stem.replace("invoice_", "")

    # Date: labeled first, fallback to any date-like token
    date_raw = _label_value(soup, LABELS["date"]) or _find_date_anywhere(text)
    date_iso = _to_iso(date_raw)

    # Client/company
    client = _label_value(soup, LABELS["client"])

    # Money raw
    net_raw   = _label_value(soup, LABELS["net"])
    total_raw = _label_value(soup, LABELS["total"])
    vat_raw   = _label_value(soup, LABELS["vat"])

    # Parse numbers with smart decimal detector
    net_amount   = _smart_decimal(net_raw)
    total_amount = _smart_decimal(total_raw)
    vat_rate, vat_abs = _vat_to_rate_or_amount(vat_raw)

    # Fix/compute coherent amounts
    net_amount, vat_amount, total_amount = _infer_and_fix_amounts(
        net=net_amount,
        vat_rate=vat_rate,
        vat_amount=vat_abs,
        total=total_amount,
    )

    # Store VAT column as:
    # - the numeric rate (e.g., 24.00) when we saw a '%'
    # - otherwise the absolute VAT amount
    vat_field: Decimal
    if vat_rate is not None:
        vat_field = vat_rate.quantize(DEC2, rounding=ROUND_HALF_UP)  # store % when present
    else:
        vat_field = vat_amount  # store absolute amount when no %

    # Extract the main body of the invoice as text
    main_invoice_text = ""
    for elem in soup.find_all(["p", "div", "td"]):
        if elem.get_text(strip=True):
            main_invoice_text += elem.get_text(" ", strip=True) + "\n"

    # New code change: include raw HTML content
    html_content = html  # The raw HTML of the invoice

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
        "amount": net_amount,           # net/subtotal
        "vat": vat_field,               # % rate (e.g., 24.00) OR amount when no '%' was present
        "total_amount": total_amount,   # grand total
        "invoice_number": invoice_number,
        "priority": "",
        "message": main_invoice_text,  
        "is_html": True,
        "html_content": html_content,  # The raw HTML of the invoice
    }


def parse_invoices_dir(invoices_dir: str) -> pd.DataFrame:
    p = Path(invoices_dir)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame()

    rows: List[Dict[str, object]] = []
    for f in sorted(p.glob("*.html")):
        try:
            rows.append(parse_invoice_html(f))
        except Exception as e:
            # keep going on per-file failure
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
                "amount": Decimal("0.00"),
                "vat": Decimal("0.00"),
                "total_amount": Decimal("0.00"),
                "invoice_number": f.stem.replace("invoice_", ""),
                "priority": "",
                "message": f"PARSE ERROR: {e}",
                "is_html": True,
            })
    df = pd.DataFrame(rows)

    # Ensure Decimal columns remain Decimal (helpful for exact money math)
    for c in ("amount", "total_amount", "vat"):
        # They might contain either rate (%) or amount; leave as object/Decimal.
        pass

    return df


def format_money(amount: Decimal | float | int) -> str:
    """
    Format as Euro with two decimals and thousands separators, e.g. €5,208.00
    (Uses U.S.-style grouping for display; change if you prefer EU format.)
    """
    if isinstance(amount, Decimal):
        amount = float(amount)
    return f"€{amount:,.2f}"
