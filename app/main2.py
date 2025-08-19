from __future__ import annotations
import os
import base64
import re
import html
import logging
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import pandas as pd
import streamlit as st
from dateutil import parser as dateparse
from bs4 import BeautifulSoup
from html import unescape as html_unescape
from app.processors.invoice_reader import parse_invoices_dir as parse_invoices_from_html
from app.processors.invoice_reader import eu_to_float
from app.processors.pdf_invoice_reader import parse_pdf_invoices_dir
# ===================== Configuration =====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load CSS from external file
def load_css(css_file: str) -> None:
    try:
        with open(css_file, "r") as f:
            css = f"<style>{f.read()}</style>"
            st.markdown(css, unsafe_allow_html=True)
    except FileNotFoundError:
        logger.error(f"CSS file not found: {css_file}")
        st.error(f"CSS stylesheet missing. Please create {css_file}")

# ===================== Helper Functions =====================
def sanitize_field(val: str) -> str:
    """Clean and sanitize text fields"""
    if val is None:
        return ""
    s = html_unescape(str(val))
    s = re.sub(r"<\s*/?\s*(span|script|style)[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def connect_gsheet(sheet_id: str, creds_path: Optional[str] = None, creds_dict: Optional[dict] = None):
    """Connect to Google Sheets"""
    import gspread
    if creds_dict:
        return gspread.service_account_from_dict(creds_dict).open_by_key(sheet_id)
    elif creds_path:
        return gspread.service_account(filename=creds_path).open_by_key(sheet_id)
    return gspread.service_account().open_by_key(sheet_id)

def upsert_worksheet(sh, title: str, rows: int = 1000, cols: int = 20):
    """Get or create worksheet"""
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

# --- Money parsing helpers (EU/US aware) for EMAILS ---

def eu_to_float(x):
    """'€1.054,00' | '5.208,00' | '1,234.56' -> float; invalid -> <NA>."""
    if x is None or x == "" and  type(x) is str:
        
        return pd.NA
    s = str(x)
    raw = re.sub(r"[^\d,.\-+]", "", s)
    if raw.count(",") >= 1 and raw.count(".") >= 1:
        raw = raw.replace(".", "").replace(",", ".")     # 5.208,00 -> 5208.00
    elif raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")                      # 850,00 -> 850.00
    elif raw.count(".") >= 1 and raw.count(",") == 0:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")                   # 5.208 -> 5208
    try:
        return float(raw)
    except Exception:
        return pd.NA

def extract_money_from_text(text: str):
    """
    Look for net/amount, VAT (amount or %), and total in Greek/English emails.
    Returns dict with floats (or <NA>) and a flag if VAT is a rate.
    """
    pat = {
        "amount": r"(?:Καθαρή\s*Αξία|Net|Subtotal)\s*[:\-]?\s*([€\d\.,]+)",
        "vat":    r"(?:\bΦΠΑ\b[^:\n]*|VAT|Tax)\s*[:\-]?\s*([€\d\.,%]+)",
        "total":  r"(?:ΣΥΝΟΛΟ|Σύνολο|Συνολικό\s*Ποσό|Total|Grand\s*Total)\s*[:\-]?\s*([€\d\.,]+)",
    }

    def find1(key):
        m = re.search(pat[key], text, flags=re.IGNORECASE)
        return m.group(1).strip() if m else ""

    raw_amount = find1("amount")
    raw_vat    = find1("vat")
    raw_total  = find1("total")

    amount = eu_to_float(raw_amount)
    total  = eu_to_float(raw_total)

    # VAT can be % or € amount
    vat_amount = pd.NA
    vat_rate   = pd.NA
    if raw_vat:
        m_pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", raw_vat)
        if m_pct:
            vat_rate = eu_to_float(m_pct.group(1))
        else:
            vat_amount = eu_to_float(raw_vat)

    # reconcile missing value
    if pd.notna(vat_rate) and pd.notna(amount):
        vat_amount = round(amount * (vat_rate/100.0), 2)
        if pd.isna(total):
            total = round(amount + vat_amount, 2)
    elif pd.notna(vat_amount) and pd.notna(amount) and pd.isna(total):
        total = round(amount + vat_amount, 2)
    elif pd.notna(total) and pd.notna(amount) and pd.isna(vat_amount):
        vat_amount = round(total - amount, 2)

    return {
        "amount": amount,
        "vat": vat_amount if pd.notna(vat_amount) else vat_rate,  # prefer amount; else store % rate
        "total_amount": total,
        "vat_is_rate": pd.isna(vat_amount) and pd.notna(vat_rate),
    }


# ===================== Email Parsing =====================
GREEK_FIELD_PATTERNS = {
    "name": r"^\s*-\s*(?:Όνομα|Ονομα|Όνομα και Επώνυμο)\s*:\s*(.+)$",
    "email": r"^\s*-\s*Email\s*:\s*(.+)$",
    "phone": r"^\s*-\s*(?:Τηλέφωνο|Τηλ)\s*:\s*(.+)$",
    "company": r"^\s*-\s*(?:Εταιρεία|Company)\s*:\s*(.+)$",
    "position": r"^\s*-\s*(?:Θέση|Position)\s*:\s*(.+)$",
}

def _extract_content(msg) -> Tuple[str, bool]:
    """Extract content from email, preferring HTML if available"""
    html_content = plain_content = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True) or b""
            
            try:
                decoded = payload.decode(charset, errors="replace")
            except UnicodeDecodeError:
                decoded = payload.decode("latin-1", errors="replace")
                
            if ctype == "text/plain":
                plain_content = decoded.strip()
            elif ctype == "text/html":
                html_content = decoded.strip()
    else:
        ctype = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True) or b""
        try:
            decoded = payload.decode(charset, errors="replace")
        except UnicodeDecodeError:
            decoded = payload.decode("latin-1", errors="replace")
            
        if ctype == "text/plain":
            plain_content = decoded.strip()
        elif ctype == "text/html":
            html_content = decoded.strip()
    
    return (html_content, True) if html_content else (plain_content, False)

def parse_eml(path: Path) -> Dict[str, str]:
    """Parse .eml file into structured data"""
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    content, is_html = _extract_content(msg)
    lines = [l for l in content.split("\n") if l.strip()] if not is_html else []

    

    data = {
        "type": "EMAIL",
        "source": path.name,
        "source_path": str(path),
        "from_name": sanitize_field(msg.get("From")),
        "to": sanitize_field(msg.get("To")),
        "subject": sanitize_field(msg.get("Subject")),
        "date_raw": sanitize_field(msg.get("Date")),
        "name": "",
        "email": "",
        "phone": "",
        "company": "",
        "service_interest": "",
        "amount": "",
        "vat": "",
        "total_amount": "",
        "invoice_number": "",
        "priority": "",
        "message": content,
        "is_html": is_html
    }

    

    # Parse date
    try:
        dt = dateparse.parse(data["date_raw"])
        data["date"] = dt.date().isoformat() if dt else ""
    except Exception:
        data["date"] = ""

    # Extract labeled fields
    for line in lines:
        for key, pattern in GREEK_FIELD_PATTERNS.items():
            m = re.search(pattern, line, flags=re.IGNORECASE)
            if m and not data.get(key):
                data[key] = sanitize_field(m.group(1))

    # Detect service interest
    lower_body = content.lower()
    if re.search(r"\bcrm\b", lower_body):
        data["service_interest"] = "CRM"
    elif re.search(r"\berp\b", lower_body):
        data["service_interest"] = "ERP Integration"
    elif re.search(r"\bwebsite\b", lower_body):
        data["service_interest"] = "Website"
    
    # --- Add this block to extract invoice fields from email body ---
    invoice_patterns = {
        "amount": r"(?:Καθαρή Αξία|Net|Subtotal)[\s:]*([€\d\.,]+)",
        "vat": r"(?:ΦΠΑ 24%|VAT|Tax)[\s:]*([€\d\.,]+)",
        "total_amount": r"(?:Συνολικό Ποσό:|ΣΥΝΟΛΟ|Σύνολο|Total|Grand Total)[\s:]*([€\d\.,]+)",
        "invoice_number": r"(?:Αριθμός|Invoice No|Invoice #|Number)[\s:]*([\w\-\/]+)|\b(TF-|IN|INV)-\d{4}-\d+\b",
    }

    for key, pattern in invoice_patterns.items():
        if not data.get(key):
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                # For invoice_number, check both groups
                if key == "invoice_number":
                    data[key] = m.group(1) or m.group(2)
                else:
                    data[key] = m.group(1).strip()

    # Fallback: check subject and attachments for invoice_number
    if not data.get("invoice_number"):
        subj = data.get("subject", "")
        m = re.search(r"\b(TF-|IN|INV)-\d{4}-\d+\b", subj)
        if m:
            data["invoice_number"] = m.group(0)
        else:
            # Check for attachment or filename pattern
            m = re.search(r"\b(TF-|IN|INV)-\d{4}-\d+\b", path.name)
            if m:
                data["invoice_number"] = m.group(0)
    # -------------------------------------------------------------

    # Set client name from extracted name
    data["client_name"] = data["name"]

    # Extract VAT percent and amount
    vat_percent_match = re.search(r"(?:ΦΠΑ 24%|VAT|Tax)\s*([0-9]{1,2})%", content, re.IGNORECASE)
    vat_percent = vat_percent_match.group(1) if vat_percent_match else ""

    vat_amount_match = re.search(r"(?:ΦΠΑ 24%|VAT|Tax)[^€\d]{0,10}([€\d\.,]+)", content, re.IGNORECASE)
    vat_amount = vat_amount_match.group(1) if vat_amount_match else ""

    data["vat_percent"] = vat_percent
    data["vat"] = vat_amount

    return data


def parse_emails_dir(emails_dir: str) -> pd.DataFrame:
    """Parse all .eml files in directory"""
    p = Path(emails_dir)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame()
        
    emls = sorted(p.glob("*.eml"))
    rows = []
    
    with st.spinner(f"Parsing {len(emls)} emails..."):
        for eml in emls:
            try:
                rows.append(parse_eml(eml))
            except Exception as e:
                logger.error(f"Error parsing {eml}: {e}")
                rows.append({
                    "type": "EMAIL",
                    "source": eml.name,
                    "source_path": str(eml),
                    "from_name": "",
                    "to": "",
                    "subject": f"PARSE ERROR: {str(e)[:50]}",
                    "date_raw": "",
                    "date": "",
                    "name": "",
                    "email": "",
                    "phone": "",
                    "company": "",
                    "service_interest": "",
                    "amount": "",
                    "vat": "",
                    "total_amount": "",
                    "invoice_number": "",
                    "priority": "",
                    "message": "",
                    "is_html": False
                })
                
                
    if not rows:
        return pd.DataFrame()
        
    df = pd.DataFrame(rows)
    return df

# ===================== Form Parsing =====================
def parse_form_file(path: Path) -> Dict[str, str]:
    """Parse HTML form file into structured data"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        form = soup.find('form')
        
        if not form:
            raise ValueError("No form found in HTML file")
        
        # Extract all form fields
        form_data = {}
        for element in form.find_all(['input', 'textarea', 'select']):
            name = element.get('name')
            if not name:
                continue
                
            if element.name == 'input':
                # Handle different input types
                if element.get('type') == 'checkbox':
                    value = "Checked" if element.get('checked') else "Not Checked"
                elif element.get('type') == 'radio':
                    value = "Selected" if element.get('checked') else "Not Selected"
                else:
                    value = element.get('value', '')
            elif element.name == 'textarea':
                value = element.get_text(strip=True)
            elif element.name == 'select':
                selected = element.find('option', selected=True)
                value = selected.get('value', selected.text) if selected else ''
                
            form_data[name] = value
        
        # Create base data structure
        data = {
            "type": "FORM",
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
            "message": "",
            "is_html": False
        }
        
        # Map form fields to our data structure
        field_mapping = {
            "client_name": ["full_name", "name", "contact_name"],
            "email": ["email"],
            "phone": ["phone", "telephone", "mobile"],
            "company": ["company", "organization"],
            "service_interest": ["service", "interest", "service_interest"],
            "priority": ["priority", "urgency"],
            "message": ["message", "comments", "notes"]
        }
        
        # Extract values based on field mapping
        for key, possible_names in field_mapping.items():
            for name in possible_names:
                if name in form_data:
                    data[key] = sanitize_field(form_data[name])
                    break
        
        # Extract submission date
        date_fields = ["submission_date", "date", "timestamp"]
        for field in date_fields:
            if field in form_data:
                try:
                    dt = dateparse.parse(form_data[field])
                    data["date"] = dt.date().isoformat()
                    break
                except Exception:
                    pass
        
        # Create message from all fields if not provided
        if not data["message"]:
            data["message"] = "\n".join([f"{k}: {v}" for k, v in form_data.items()])
        
        return data
        
    except Exception as e:
        logger.error(f"Error parsing form {path}: {e}")
        return {
            "type": "FORM",
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
            "message": f"FORM PARSE ERROR: {str(e)[:50]}",
            "is_html": False
        }

def parse_forms_dir(forms_dir: str) -> pd.DataFrame:
    """Parse all form submission files in directory"""
    p = Path(forms_dir)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame()
        
    form_files = list(p.glob("*.html"))
    rows = []
    
    with st.spinner(f"Parsing {len(form_files)} forms..."):
        for form_file in form_files:
            try:
                rows.append(parse_form_file(form_file))
            except Exception as e:
                logger.error(f"Error parsing {form_file}: {e}")
                rows.append({
                    "type": "FORM",
                    "source": form_file.name,
                    "source_path": str(form_file),
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
                    "message": f"FORM PARSE ERROR: {str(e)[:50]}",
                    "is_html": False
                })
                
    if not rows:
        return pd.DataFrame()
        
    return pd.DataFrame(rows)




# ===================== Content Formatting =====================
def format_content(content: str, is_html: bool) -> str:
    """Format content for beautiful display"""
    if is_html:
        # Basic sanitization
        content = re.sub(r"<head.*?</head>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<script.*?</script>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<style.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<img[^>]*>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s{2,}", " ", content)
        content = html_unescape(content)
        content = content.strip()
     
        # Wrap in our container
        return f'<div class="email-content">{content}</div>'
    else:
        # Convert plain text to formatted HTML
        content = html.escape(content)
        
        # Convert URLs to links
        content = re.sub(
            r"(https?://[^\s]+)", 
            r'<a href="\1" target="_blank">\1</a>', 
            content
        )
        
        # Convert phone numbers
        content = re.sub(
            r"(\+?\d[\d\s\-\(\)]{7,}\d)", 
            r'<a href="tel:\1" class="phone-number">\1</a>', 
            content
        )
        
        # Create paragraphs
        content = re.sub(r"\n{2,}", "</p><p>", content)
        
        # Convert newlines to breaks
        content = re.sub(r"\n", "<br>", content)
        
        return f'<div class="email-content"><p>{content}</p></div>'

# ===================== Main App =====================
def main():
    # Load CSS from external file
    load_css("styles.css")
    
    # Page configuration
    st.set_page_config(
        page_title="Custom Automation", 
        page_icon="📊", 
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.title("📊 Custom Automation Project")
    
    # ===================== Sidebar =====================
    with st.sidebar:
        st.header("Data Sources")
        default_emails_dir = os.environ.get("EMAILS_DIR", "./data/emails")
        emails_dir = st.text_input("Emails folder", value=default_emails_dir)
        
        default_forms_dir = os.environ.get("FORMS_DIR", "./data/forms")
        forms_dir = st.text_input("Forms folder", value=default_forms_dir)
        
        default_invoices_dir = os.environ.get("INVOICES_DIR", "./data/invoices")
        invoices_dir = st.text_input("Invoices folder", value=default_invoices_dir)
        
        st.markdown("---")
        st.header("Google Sheets")
        sheet_id = st.text_input("Google Sheet ID", value=os.environ.get("GOOGLE_SHEET_ID", ""))
        tab_name = st.text_input("Worksheet name", value=os.environ.get("GOOGLE_WORKSHEET_NAME", "Leads"))

        st.markdown("---")
        st.subheader("Google Auth")
        auth_mode = st.radio("Authentication mode", ["Env var / default", "Use file path", "Upload JSON"], index=0)
        creds_path = None
        creds_dict = None
        
        if auth_mode == "Use file path":
            creds_path = st.text_input(
                "Service account JSON path", 
                value=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            )
        elif auth_mode == "Upload JSON":
            up = st.file_uploader("Service account JSON", type="json")
            if up:
                import json
                try:
                    creds_dict = json.load(up)
                except json.JSONDecodeError:
                    st.error("Invalid JSON file")
    
    st.caption(f"Email Folder: `{emails_dir}` | Forms Folder: `{forms_dir}` | Invoices Folder: `{invoices_dir}`")
    
    # ===================== Data Processing =====================
    email_df = pd.DataFrame()
    form_df = pd.DataFrame()
    invoice_df = pd.DataFrame()
    
    # Parse emails
    if Path(emails_dir).exists() and Path(emails_dir).is_dir():
        email_df = parse_emails_dir(emails_dir)
    else:
        st.warning(f"Email directory not found: {emails_dir}")
    
    # Parse forms
    if Path(forms_dir).exists() and Path(forms_dir).is_dir():
        form_df = parse_forms_dir(forms_dir)
    else:
        st.warning(f"Forms directory not found: {forms_dir}")
    
    # Parse invoices (use the new reader)
    if Path(invoices_dir).exists() and Path(invoices_dir).is_dir():
        invoice_df = parse_invoices_from_html(invoices_dir)
    
    # Then try PDF invoices
        pdf_invoice_df = parse_pdf_invoices_dir(invoices_dir)
        invoice_df = pd.concat([invoice_df, pdf_invoice_df], ignore_index=True)    
    else:
        st.warning(f"Invoices directory not found: {invoices_dir}")
    
    # Combine datasets
    combined_df = pd.concat([email_df, form_df, invoice_df], ignore_index=True)
    
    if combined_df.empty:
        st.info("No data found. Add files to the specified folders.")
        return

    # ===================== Entry List View =====================
    st.subheader(f"Found {len(combined_df)} entries ({len(email_df)} emails, {len(form_df)} forms, {len(invoice_df)} invoices)")
    
    # Create display version
    df_display = combined_df.copy()
    
    # Ensure display columns always exist
    for col in ["amount_display", "vat_display", "total_amount_display"]:
        if col not in df_display.columns:
            df_display[col] = ""

    # Format money columns
    def money_fmt(val):
        try:
            # Convert using your invoice logic for EU/GR numbers
            if isinstance(val, str):
                # Remove euro sign and spaces
                val = val.replace("€", "").replace(" ", "")
                # Replace comma with dot if needed
                if "," in val:
                    val = val.replace(",", "")
            num = float(val)
            return f"€{num:,.2f}"
        except Exception:
            return ""

    for col in ["amount", "vat", "total_amount"]:
        if col in df_display.columns:
            df_display[f"{col}_display"] = df_display[col].apply(money_fmt)

    def priority_to_emoji(val):
        if not val:
            return ""
        val = str(val).strip().lower()
        if val in ["high", "υψηλή", "urgent"]:
            return "🔴"
        elif val in ["medium", "μέτρια"]:
            return "🟠"
        elif val in ["low", "χαμηλή"]:
            return "🟢"
        elif val in ["critical", "πολύ υψηλή"]:
            return "🚨"
        return val  # fallback to original if not matched

    if "priority" in df_display.columns:
        df_display["priority"] = df_display["priority"].apply(priority_to_emoji)

    def type_to_emoji(val):
        if val == "EMAIL":
            return "📧"
        elif val == "FORM":
            return "📝"
        elif val == "INVOICE":
            return "🧾"
        return ""

    df_display["type_emoji"] = df_display["type"].apply(type_to_emoji)

# Now you can safely use the _display columns in your column_mapping and st.dataframe
    column_mapping = {
        "type_emoji": "",
        "type": "Type",
        "source": "Source",
        "date": "Date",
        "client_name": "Client_Name",
        "email": "Email",
        "phone": "Phone",
        "company": "Company",
        "service_interest": "Service_Interest",
        "amount_display": "Amount",
        "vat_display": "VAT",
        "total_amount_display": "Total_Amount",
        "invoice_number": "Invoice_Number",
        "priority": "Priority",
        "message": "Message"
    }
    
    # Display entry list
    st.dataframe(
        df_display[list(column_mapping.keys())].rename(columns=column_mapping),
        use_container_width=True,
        height=500,
        column_config={
            "Amount": st.column_config.TextColumn("Amount", width="medium"),
            "VAT": st.column_config.TextColumn("VAT", width="medium"),
            "Total_Amount": st.column_config.TextColumn("Total", width="medium"),
            # ...other columns...
        }
    )
    
# ===================== Entry Detail View =====================
    st.subheader("Entry Details")
    if combined_df.empty:
        st.warning("No entries available for detail view")
    else:
        def entry_label(i):
            entry_type = combined_df.at[i, 'type']
            emoji = (
                "📧" if entry_type == "EMAIL"
                else "📝" if entry_type == "FORM"
                else "🧾"
            )
            date = combined_df.at[i, 'date']
            client_name = combined_df.at[i, 'client_name']
            invoice_number = combined_df.at[i, 'invoice_number']
            source = combined_df.at[i, 'source']
            # Prefer client_name, then invoice_number, then source
            name = client_name or invoice_number or source
            return f"[{emoji}] {date} • {name}"

        sel_idx = st.selectbox(
            "Select entry to view",
            options=combined_df.index,
            format_func=entry_label,
            index=0
        )
        
        row = combined_df.loc[sel_idx]
        entry_type = row.get("type", "")
        source = row.get("source", "")
        date_val = row.get("date", "")
        client_name = row.get("client_name", "")
        email_val = row.get("email", "")
        phone_val = row.get("phone", "")
        company_val = row.get("company", "")
        service_interest = row.get("service_interest", "")
        amount = row.get("amount", "")
        vat = row.get("vat", "")
        total_amount = row.get("total_amount", "")
        invoice_number = row.get("invoice_number", "")
        priority = row.get("priority", "")
        message = row.get("message", "")
        source_path = html.escape(str(row.get("source_path", "")))
        is_html_val = row.get("is_html", False)

        # Format the message body
        if entry_type == "EMAIL":
            msg_html = format_content(message, is_html_val)
        else:
            # For forms and invoices, show as plain text
            msg_html = f'<div class="email-content"><p>{html.escape(message).replace("\n", "<br>")}</p></div>'

        # Add JavaScript for actions
        st.markdown(f"""
        <script>
        function callPhone(number) {{
            // Clean the phone number
            const cleanNumber = number.replace(/[^0-9+]/g, '');
            alert("Calling: " + cleanNumber + "\\n\\n(On a real device, this would initiate a phone call)");
        }}

        function replyEmail() {{
            const email = "{html.escape(email_val)}";
            const subject = "Regarding: {html.escape(source)}";
            
            if (!email) {{
                alert("No email address found for reply");
                return;
            }}
            
            alert("Replying to: " + email + "\\nSubject: " + subject + "\\n\\n(On a real device, this would open your email client)");
        }}
        </script>
        """, unsafe_allow_html=True)

        # Build metadata chips
        chips = [
            ("📅", date_val),
            ("👤", client_name) if client_name else None,
            ("🏢", company_val) if company_val else None,
            ("📧", email_val) if email_val else None,
            ("📞", phone_val) if phone_val else None,
            ("🧩", service_interest) if service_interest else None,
            ("🧾", invoice_number) if invoice_number else None,
            ("⚠️", priority) if priority else None,
        ]

        # Filter out empty chips
        chips_html = "".join([
            chip_html("📅", date_val, "date"),
            chip_html("📧", email_val, "email") if email_val else "",
            chip_html("🏢", company_val, "company") if company_val else "",
            chip_html("👤", client_name, "client") if client_name else "",
            chip_html("📞", phone_val, "phone") if phone_val else "",
            chip_html("🧾", invoice_number, "invoice") if invoice_number else "",
            chip_html("🧩", service_interest, "service") if service_interest else "",
            chip_html("⚠️", priority, "priority") if priority else "",
        ])

        # Add PDF download capability
        pdf_base64 = ""
        if entry_type == "INVOICE" and Path(source_path).exists() and Path(source_path).suffix.lower() == '.pdf':
            try:
                with open(source_path, "rb") as pdf_file:
                    pdf_base64 = base64.b64encode(pdf_file.read()).decode('utf-8')
            except Exception as e:
                logger.error(f"Error reading PDF file: {e}")
        # Render entry card with download button
        footer_html = f"""
        <div class='email-footer'>
          <div class='footer-items'>
            <span class='source-path' title='{html.escape(source_path)}'>Source: {html.escape(source_path)}</span>
           {f'<a href="data:application/pdf;base64,{pdf_base64}" download="{html.escape(source)}" class="download-btn">📥Download</a>' if pdf_base64 else ''}
           </div>
          <span>Type: {entry_type}</span>
        </div>
        """
        st.markdown(f"""
        <div class='email-card'>
          <div class='email-meta'>{chips_html}</div>
          <div class='email-subject'>{html.escape(source)}</div>
          <div class='email-body'>{msg_html}</div>
          {footer_html}
        </div>
        """, unsafe_allow_html=True)

# ===================== Google Sheets Export =====================
    st.subheader("Export to Google Sheets")

    if not sheet_id:
        st.warning("Enter Google Sheet ID in sidebar to enable export")

    if st.button("⬆️ Push all entries to Google Sheets", 
                 disabled=not sheet_id,
                 type="primary",
                 use_container_width=True) and sheet_id:
        try:
            import gspread
            from gspread_dataframe import set_with_dataframe

            # Prepare export data
            export_df = combined_df.rename(columns={
                "type": "Type",
                "source": "Source",
                "date": "Date",
                "client_name": "Client_Name",
                "email": "Email",
                "phone": "Phone",
                "company": "Company",
                "service_interest": "Service_Interest",
                "amount_display": "Amount",
                "vat_display": "VAT",
                "total_amount_display": "Total_Amount",
                "invoice_number": "Invoice_Number",
                "priority": "Priority",
                "message": "Message"
            })

            # Select only the columns we want to export
            export_columns = [
                "Type", "Source", "Date", "Client_Name", "Email", "Phone", "Company",
                "Service_Interest", "Amount", "VAT", "Total_Amount", "Invoice_Number",
                "Priority", "Message"
            ]

            # Ensure all columns exist
            for col in export_columns:
                if col not in export_df.columns:
                    export_df[col] = ""

            # Export only the required columns
            export_df = export_df[export_columns]

            with st.spinner("Connecting to Google Sheets..."):
                sh = connect_gsheet(sheet_id, creds_path=creds_path, creds_dict=creds_dict)
                ws = upsert_worksheet(sh, tab_name)

                # Clear existing data except header
                if ws.row_count < len(export_df) + 1:
                    ws.resize(rows=len(export_df) + 100, cols=len(export_columns))

                # Write new data
                set_with_dataframe(ws, export_df, include_index=False, include_column_header=True, resize=False)

            st.success(f"✅ Exported {len(export_df)} entries to worksheet: {tab_name}")
            st.balloons()

        except Exception as e:
            st.error(f"Export failed: {str(e)}")
            logger.exception("Google Sheets export error")

def chip_html(emoji, value, chip_type):
    if not value:
        return ""
    if chip_type == "email":
        return f"<a class='chip' href='mailto:{html.escape(str(value))}' title='Send email'>{emoji} {html.escape(str(value))}</a>"
    elif chip_type == "company":
        url = f"https://www.google.com/search?q={html.escape(str(value))}"
        return f"<a class='chip' href='{url}' target='_blank' title='Search company on Google'>{emoji} {html.escape(str(value))}</a>"
    elif chip_type == "client":
        url = "https://contacts.google.com/"
        return f"<a class='chip' href='{url}' target='_blank' title='Open Google Contacts'>{emoji} {html.escape(str(value))}</a>"
    elif chip_type == "phone":
        return f"<a class='chip' href='tel:{html.escape(str(value))}' title='Call'>{emoji} {html.escape(str(value))}</a>"
    elif chip_type == "date":
        # Format date for Google Calendar (YYYYMMDD)
        from datetime import datetime
        try:
            date_str = str(value)
            if not date_str or date_str in ["", "None"]:
                dt = datetime.today()
            else:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            date_fmt = dt.strftime("%Y%m%d")
            url = f"https://calendar.google.com/calendar/render?action=TEMPLATE&dates={date_fmt}/{date_fmt}"
            return f"<a class='chip' href='{url}' target='_blank' title='Open in Google Calendar'>{emoji} {html.escape(dt.strftime('%Y-%m-%d'))}</a>"
        except Exception:
            return f"<span class='chip'>{emoji} {html.escape(str(value))}</span>"
    else:
        return f"<span class='chip'>{emoji} {html.escape(str(value))}</span>"

        


if __name__ == "__main__":
    main()

