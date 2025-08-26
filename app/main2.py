from __future__ import annotations
import os
import base64
import re
import html
import logging
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import pandas as pd
import streamlit as st
from dateutil import parser as dateparse
from bs4 import BeautifulSoup
from html import unescape as html_unescape
from gspread_dataframe import set_with_dataframe
from datetime import datetime
from processors.invoice_reader import parse_invoices_dir as parse_invoices_from_html
from processors.invoice_reader import eu_to_float
from processors.pdf_invoice_reader import parse_pdf_invoices_dir
import decimal
import sqlite3
import gspread
import json
from fpdf import FPDF
from weasyprint import HTML
from scroll_to_anchor import scroll_to
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

# --- Money parsing helpers (EU/US) for EMAILS ---
def eu_to_float(x):
    """Converts EU/US formatted money strings to float."""
    if x is None or (isinstance(x, str) and x.strip() == ""):
        return pd.NA
    s = str(x)
    raw = re.sub(r"[^\d,.\-+]", "", s)
    # US style: 1,234.56 (comma thousands, dot decimal)
    if re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", raw):
        raw = raw.replace(",", "")
    # EU style: 1.234,56 (dot thousands, comma decimal)
    elif re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    # Only comma decimal: 1234,56
    elif raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")
    # Only dot decimal: 1234.56
    # else: leave as is
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

# Database functions
def load_entries_from_db():
    """Load all entries from SQLite database"""
    conn = sqlite3.connect("data.db")
    try:
        df = pd.read_sql("SELECT * FROM entries", conn)
    except Exception as e:
        logger.error(f"Error loading from database: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

def save_entries_to_db(df):
    """Save entries to SQLite database"""
    conn = sqlite3.connect("data.db")
    try:
        df.to_sql("entries", conn, if_exists="replace", index=False)
    except Exception as e:
        logger.error(f"Error saving to database: {e}")
    finally:
        conn.close()

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
    
    # Extract invoice fields from email body
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
            else:
                data["invoice_number"] = "" 
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

    # Convert money columns to float for emails
    for col in ["amount", "vat", "total_amount"]:
        if col in df.columns:
            df[col] = df[col].apply(eu_to_float)

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

# ===================== Data Processing =====================
def process_data(email_df, form_df, invoice_df):
    db_exists = os.path.exists("data.db")
    if db_exists:
        existing_df = load_entries_from_db()
    else:
        existing_df = pd.DataFrame()

    # Combine new data
    new_df = pd.concat([email_df, form_df, invoice_df], ignore_index=True)
    new_df["status"] = "pending"  # Force new items to pending

    # Merge, avoiding duplicates by 'source_path'
    if not existing_df.empty:
        # Remove any new items that already exist (keep DB status)
        new_items = new_df[~new_df["source_path"].isin(existing_df["source_path"])]
        combined_df = pd.concat([existing_df, new_items], ignore_index=True)
    else:
        combined_df = new_df

    if combined_df.empty:
        st.info("No data found. Add files to the specified folders.")
        return pd.DataFrame()

    # Add status column if not present
    if "status" not in combined_df.columns:
        combined_df["status"] = "pending"

    # Convert decimals to float
    def convert_decimal(val):
        if isinstance(val, decimal.Decimal):
            return float(val)
        return val

    for col in combined_df.columns:
        combined_df[col] = combined_df[col].map(convert_decimal)

    # Save merged data to DB
    save_entries_to_db(combined_df)

    # Load from DB for display
    combined_df = load_entries_from_db()

    if combined_df.empty:
        st.info("No data found in database.")
        return pd.DataFrame()

    return combined_df

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
    
    # ===================== Tutorial =====================
    if "tutorial_step" not in st.session_state:
        st.session_state["tutorial_step"] = 0

    if st.session_state["tutorial_step"] == 0:
        with st.container():
            st.info("👋 **Καλωσήρθατε!** Αυτό το εργαλείο σας βοηθά να διαχειριστείτε emails, φόρμες και τιμολόγια & να τα προωθήσετε στο Google Spreadsheet. Κάνε κλικ στο Επόμενο για το tutorial.")
            if st.button("Επόμενο➡️"):
                st.session_state["tutorial_step"] = 1

    elif st.session_state["tutorial_step"] == 1:
        if st.button("Επόμενο➡️"):
            st.session_state["tutorial_step"] = 2

    elif st.session_state["tutorial_step"] == 2:
        if st.button("Επόμενο➡️"):
            st.session_state["tutorial_step"] = 3

    elif st.session_state["tutorial_step"] == 3:
        if st.button("Επόμενο➡️"):
            st.session_state["tutorial_step"] = 4

    elif st.session_state["tutorial_step"] == 4:
        if st.button("Επόμενο➡️"):
           st.session_state["tutorial_step"] = 5        
    
    # Helper function for chip HTML
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
    
    # ===================== Sidebar =====================
    with st.sidebar:
        if st.session_state["tutorial_step"] == 1:
            st.markdown(
                "<div id='tutorial'>⬇️ Εδώ ρυθμίζετε τους φακέλους!<br>Μπορείτε να προσθέσετε περισσότερα αρχεία αρκεί το PATH να είναι σωστό. </div>",
                unsafe_allow_html=True
            )
        st.title("⚙️ Settings")
        st.header("Data Sources")
        default_emails_dir = os.environ.get("EMAILS_DIR", "./data/emails")
        emails_dir = st.text_input("Emails folder", value=default_emails_dir)
        
        default_forms_dir = os.environ.get("FORMS_DIR", "./data/forms")
        forms_dir = st.text_input("Forms folder", value=default_forms_dir)
        
        default_invoices_dir = os.environ.get("INVOICES_DIR", "./data/invoices")
        invoices_dir = st.text_input("Invoices folder", value=default_invoices_dir)
        st.markdown("---")
        # Highlight Google Sheets ID field during tutorial step 2
        if st.session_state["tutorial_step"] == 2:
            st.markdown(
                "<div id='tutorial'>⬇️ Εδώ εισάγετε το Google Sheets ID και πατήστε enter!</div>",
                unsafe_allow_html=True
            )

        st.header("Google Sheets")
        sheet_id = st.text_input("Google Sheet ID", value=os.environ.get("GOOGLE_SHEET_ID", ""))
        st.markdown("---")
        # Highlight Google Auth field during tutorial step 3
        if st.session_state["tutorial_step"] == 3:
            st.markdown(
                "<div id='tutorial'>⬇️ Εδώ διαλέγετε το json για ταυτοποίηση με Google λογαριασμό!</div>",
                unsafe_allow_html=True
            )
        
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
                try:
                    creds_dict = json.load(up)
                except json.JSONDecodeError:
                    st.error("Invalid JSON file")
    
    
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
    
    # Process and combine all data
    combined_df = process_data(email_df, form_df, invoice_df)
    
    if combined_df.empty:
        st.info("No data to display. Add files to the specified folders.")
        
        return
    
        

    # ===================== Entry List View =====================
    num_emails = (combined_df["type"] == "EMAIL").sum()
    num_forms = (combined_df["type"] == "FORM").sum()
    num_invoices = (combined_df["type"] == "INVOICE").sum()
    st.subheader(f"Found {len(combined_df)} entries ({num_emails} emails, {num_forms} forms, {num_invoices} invoices)")
    
    # Create display version
    df_display = combined_df.copy()
    
    # Ensure display columns always exist
    for col in ["amount_display", "vat_display", "total_amount_display"]:
        if col not in df_display.columns:
            df_display[col] = ""

    # Format money columns
    def money_fmt(val):
        try:
            num = float(val)
            # Format as European: thousands dot, decimal comma
            int_part, dec_part = f"{abs(num):,.2f}".split(".")
            int_part = int_part.replace(",", ".")
            sign = "-" if num < 0 else ""
            return f"{sign}€{int_part},{dec_part}"
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
    df_to_show = df_display.copy()
    df_to_show.index = df_to_show.index + 1
    st.dataframe(
    df_to_show[list(column_mapping.keys())].rename(columns=column_mapping),
    use_container_width=True,
    height=500,
    column_config={
        "Amount": st.column_config.TextColumn("Amount", width="medium"),
        "VAT": st.column_config.TextColumn("VAT", width="medium"),
        "Total_Amount": st.column_config.TextColumn("Total", width="medium"),
    }
)
    
    # ===================== Entry List / Detail / Accepted-Rejected =====================
    # Split by status
    pending_display = df_display[df_display["status"] == "pending"].reset_index()
    accepted_display = df_display[df_display["status"] == "accepted"].reset_index()
    rejected_display = df_display[df_display["status"] == "rejected"].reset_index()

    st.markdown(
        f"Queue: <span style='color:orange;font-weight:bold;font-size:1.5em'>{len(pending_display)} pending</span>  •  "
        f"<span style='color:green;font-weight:bold;font-size:1.3em'>Accepted: {len(accepted_display)}</span>  •  "
        f"<span style='color:#eb5169;font-weight:bold;font-size:1.3em'>Rejected: {len(rejected_display)}</span>",
        unsafe_allow_html=True,)

    # Show pending table (compact)
    st.markdown('<div id="pending-entries-anchor"></div>', unsafe_allow_html=True)
    st.markdown("#### Pending entries")
    if not pending_display.empty:
        df_to_show = pending_display.copy()
        df_to_show.index = df_to_show.index + 1
        st.dataframe(
            df_to_show[list(column_mapping.keys())].rename(columns=column_mapping),
            use_container_width=True,
            height=400
        )
    else:
        st.info("No pending entries")

    # ===================== Entry Detail View (only for pending entries) =====================
    tutorial_placeholder = st.empty()
    if st.session_state.get("tutorial_step") == 4:
        tutorial_placeholder.markdown(
        "<div id='pending-tutorial-anchor' style='margin-top:100px;'></div>"
        "<div id='tutorial'>⬇️ Εδώ επιλέγετε μια εγγραφή από τη dropdown λίστα Entry Details,<br>"
        "ύστερα μπορείτε είτε να την αποδεχτείτε, απορρίψετε ή επεξεργαστείτε!<br><br>"
        "<strong> Δίνεται επίσης η δυνατότητα:</strong><i>(αναλόγως τα δεδομένα)</i><br>"
        "• να δημιουργήσετε ενα Google calendar, <br>"
        "• να στείλετε e-mail στον πελάτη, <br>"
        "• να κάνετε αναζήτηση της επιχείρησης στο διαδίκτυο,<br>"
        "• να προσθέσετε την επαφή στο Google Contacts, <br>"
        "• να καλέσετε τον πελάτη. <br>"
        "Στο τέλος, πατήστε για να στείλετε/ανανεώσετε τα δεδομένα στο Google Sheets </div>",
        unsafe_allow_html=True
    )
        if st.button("Επόμενο➡️", key="tutorial_next_4"):
            st.session_state["tutorial_step"] = 5
            st.rerun()
        scroll_to("pending-tutorial-anchor")


    st.subheader("Entry Details")
    if pending_display.empty:
        st.info("No pending entries to review")
    else:
        
        def entry_label(idx):
            r = combined_df.loc[idx]
            typ = r.get("type", "")
            emoji = "📧" if typ == "EMAIL" else "📝" if typ == "FORM" else "🧾"
            name = r.get("client_name") or r.get("invoice_number") or r.get("source")
            date = r.get("date", "")
            return f"[{emoji}] {date} • {name}"

        # use original combined_df indices (pending_display['index'] holds them)
        options = pending_display["index"].tolist()
        sel_idx = st.selectbox("Επιλέξτε εκκρεμή καταχώρηση για προβολή", options=options, format_func=entry_label, index=0)

        # load selected row from combined_df
        row = combined_df.loc[sel_idx]
        entry_type = row.get("type", "")
        source = row.get("source", "")
        date_val = row.get("date", "")
        client_name = row.get("client_name", "")
        # amount, vat, total_amount are not used directly here; removed to avoid unused variable warning
        service_interest = row.get("service_interest", "")
        amount = row.get("amount", "")
        vat = row.get("vat", "")
        total_amount = row.get("total_amount", "")
        invoice_number = row.get("invoice_number", "")
        priority = row.get("priority", "")
        message = row.get("message", "")
        source_path = str(row.get("source_path", ""))
        is_html_val = row.get("is_html", False)
        email_val = row.get("email", "")
        phone_val = row.get("phone", "")
        company_val = row.get("company", "")

        # Format the message body once
        if entry_type == "EMAIL":
            msg_html = format_content(message, is_html_val)
        else:
            msg_html = f'<div class="email-content"><p>{html.escape(message).replace("\\n", "<br>")}</p></div>'

        # Build metadata chips
        chips_html = "".join([
            chip_html("📅", date_val, "date"),
            chip_html("📧", email_val, "email") if email_val else "",
            chip_html("🏢", company_val, "company") if company_val else "",
            chip_html("👤", client_name, "client") if client_name else "",
            chip_html("📞", phone_val, "phone") if phone_val else "",
            # Only show invoice number chip if it matches expected pattern
            chip_html("🧾", invoice_number, "invoice") if entry_type == "INVOICE" and invoice_number and re.match(r"^(TF-|IN|INV)-\d{4}-\d+$", invoice_number) else "",
            chip_html("🧩", service_interest, "service") if service_interest else "",
            chip_html("𝐏𝐫𝐢𝐨𝐫𝐢𝐭𝐲", priority, "priority") if priority else "",
        ])

        # Render card (only once)
        card_html = f"""
        <div class='email-card'>
          <div class='email-meta'>{chips_html}</div>
          <div class='email-subject'>{html.escape(source)}</div>
          <div class='email-body'>{msg_html}</div>
          <div class='email-footer'>
            <span>Type: {row.get("type", "")}"""

        if entry_type == "INVOICE":
            pdf_path = Path(source_path)
            # PDF invoice
            if pdf_path.exists() and pdf_path.suffix.lower() == ".pdf":
                card_html += "</span>"  # close the Type span
                # Add a placeholder for the download button and source
                card_html += "<span style='margin-left: 1em; display: inline-block;' id='pdf-btn-placeholder'></span>"
                card_html += f"<span style='float:right;'>Source: {html.escape(source_path)}</span></div></div>"
                st.markdown(card_html, unsafe_allow_html=True)
                # Use st.columns to keep the button inline
                cols = st.columns([0.15, 0.85])
                with cols[0]:
                    # Load the image and encode as base64
                    icon_path = "assets/pdf_icon2.png"  # update path as needed
                    with open(icon_path, "rb") as img_file:
                        img_bytes = img_file.read()
                        img_b64 = base64.b64encode(img_bytes).decode()

                    img_md = f"![PDF](data:image/png;base64,{img_b64}) Download PDF"
                    st.download_button(
                    label=img_md,
                    data=pdf_path.read_bytes(),
                    file_name=pdf_path.name,
                    mime="application/pdf",
                    key=f"download_pdf_{sel_idx}",
                    use_container_width=True,
                    help="Download PDF",
                    disabled=False,
                    )
            # HTML invoice
            elif pdf_path.exists() and pdf_path.suffix.lower() in [".htm", ".html"]:
                card_html += f"</span><span style='float:right;'>Source: {html.escape(source_path)}</span></div></div>"
                st.markdown(card_html, unsafe_allow_html=True)
                # Only generate PDF if button is clicked
                if st.button("🧾 Export Invoice as PDF", help="Εξαγωγή αρχείου", key=f"gen_html_invoice_pdf_{sel_idx}"):
                    with st.spinner("Δημιουργία PDF..."):
                        pdf_bytes = html_invoice_to_pdf(str(pdf_path))
                    st.download_button(
                        label="⬇️ Download PDF",
                        data=pdf_bytes,
                        file_name=pdf_path.with_suffix(".pdf").name,
                        mime="application/pdf",
                        key=f"download_html_invoice_pdf_{sel_idx}",
                        use_container_width=False,
                        help="Λήψη PDF",
                    )
            else:
                # If not a PDF or HTML, just close the Type span and footer
                card_html += f"</span><span style='float:right;'>Source: {html.escape(source_path)}</span></div></div>"
                st.markdown(card_html, unsafe_allow_html=True)
        else:
            # For non-invoice types, just close the Type span and footer
            card_html += f"</span><span style='float:right;'>Source: {html.escape(source_path)}</span></div></div>"
            st.markdown(card_html, unsafe_allow_html=True)

                # --- Add PDF download for EMAILs with invoice_number ---
        if (
            entry_type == "EMAIL"
            and invoice_number
            and invoice_number != ""
        ):
            pdf_bytes = email_to_pdf(row)
            st.download_button(
                label="📄 Generate Email as PDF",
                data=pdf_bytes,
                file_name=f"{invoice_number}_email.pdf",
                mime="application/pdf",
                key=f"download_email_pdf_{sel_idx}",
                use_container_width=False,
                help="Δημιουργία PDF από email",
            )   

        # Accept/Reject/Edit form (below the body)
        with st.form(key=f"status_form_{sel_idx}", clear_on_submit=True):
            st.caption("**Actions**")
            c1, c2, c3 = st.columns([1, 1, 1])
            # Disable all actions if any edit is in progress
            editing = "edit_idx" in st.session_state
            with c1:
                accept_clicked = st.form_submit_button(
                    "✅ Accept",
                    help="Αποδοχή",
                    use_container_width=True,
                    disabled=editing
                )
            with c2:
                reject_clicked = st.form_submit_button(
                    "❌ Reject",
                    help="Απόρριψη",
                    use_container_width=True,
                    disabled=editing
                )
            with c3:
                edit_clicked = st.form_submit_button(
                    "✏️ Edit",
                    help="Επεξεργασία",
                    use_container_width=True,
                    disabled=editing
                )
        if accept_clicked:
            conn = sqlite3.connect("data.db")
            try:
                conn.execute("UPDATE entries SET status = ? WHERE source_path = ?", ("accepted", source_path))
                conn.commit()
                st.toast("Entry marked as accepted.", icon="✅")
                st.rerun()
            except Exception as e:
                st.toast(f"Error updating status: {e}", icon="🚫")
                st.rerun()
            finally:
                conn.close()

        if reject_clicked:
            conn = sqlite3.connect("data.db")
            try:
                conn.execute("UPDATE entries SET status = ? WHERE source_path = ?", ("rejected", source_path))
                conn.commit()
                st.toast("Entry marked as rejected.", icon="❌")
                st.rerun()
            except Exception as e:
                st.toast(f"Error updating status: {e}", icon="🚫")
                st.rerun()
            finally:
                conn.close()

        if edit_clicked:
            st.session_state["edit_idx"] = sel_idx
            st.rerun()  # Force immediate UI update so buttons are disabled right away

        # Show edit form if triggered (outside the columns and main form)
        if "edit_idx" in st.session_state and st.session_state["edit_idx"] == sel_idx:
            st.markdown("#### Edit Entry")
            with st.form(key=f"edit_form_{sel_idx}"):
                new_client_name = st.text_input("Client Name", value=client_name, placeholder="Όνομα Πελάτη")
                new_email = st.text_input("Email", value=email_val, placeholder="example@email.com")
                new_phone = st.text_input("Phone", value=phone_val, placeholder="μόνο αριθμός τηλ.")
                new_company = st.text_input("Company", value=company_val, placeholder="Εταιρεία")
                new_service_interest = st.text_input("Service Interest", value=service_interest, placeholder="Υπηρεσία")
                new_priority = st.selectbox(
                    "Priority",
                    options=["", "high", "medium", "low", "critical"],
                    index=["", "high", "medium", "low", "critical"].index(priority.lower() if priority else "")
                )
                new_message = st.text_area("Message", value=message, height=150)
                save_edit = st.form_submit_button("💾 Save")
                cancel_edit = st.form_submit_button("❌ Cancel")

            # Validation
            email_valid = re.match(r"[^@]+@[^@]+\.[^@]+", new_email) if new_email else True
            phone_valid = re.match(r"^\+?[\d\-]{6,}$", new_phone) if new_phone else True

            if save_edit:
                if not email_valid:
                    st.error("Please enter a valid email address.")
                elif not phone_valid:
                    st.error("Please enter a valid phone number (digits only, min 6).")
                else:
                    conn = sqlite3.connect("data.db")
                    try:
                        conn.execute("""
                            UPDATE entries SET client_name=?, email=?, phone=?, company=?, service_interest=?, priority=?, message=?
                            WHERE source_path=?
                        """, (new_client_name, new_email, new_phone, new_company, new_service_interest, new_priority, new_message, source_path))
                        conn.commit()
                        st.toast("Entry updated.", icon="✅")
                        del st.session_state["edit_idx"]
                        st.rerun()
                    except Exception as e:
                        st.toast(f"Error updating entry: {e}", icon="🚫")
                    finally:
                        conn.close()
            if cancel_edit:
                del st.session_state["edit_idx"]
                st.rerun()
                
    # ===================== Accepted / Rejected lists =====================
    with st.expander(f"Accepted ({len(accepted_display)})", expanded=False):
        if not accepted_display.empty:
            df_to_show = accepted_display.copy()
            df_to_show.index = df_to_show.index + 1
            st.dataframe(
                df_to_show[list(column_mapping.keys())].rename(columns=column_mapping),
                use_container_width=True,
                height=300
            )
            # A simple restore list below the table
            st.markdown("**Επαναφορά σε εκκρεμότητα:**")
            for i, row in accepted_display.iterrows():
                entry_name = row['client_name'] or row['invoice_number'] or row['source']
                cols = st.columns(2)
                with cols[0]:
                    st.markdown(f"Εγγραφή {i+1}: {entry_name}")
                with cols[1]:
                    st.button(
                        "🔄",  # Compact label
                        key=f"restore_accepted_{row['index']}",
                        use_container_width=False,
                        help="Επαναφορά σε εκκρεμότητα"
                    )
                    if st.session_state.get(f"restore_accepted_{row['index']}"):
                        conn = sqlite3.connect("data.db")
                        try:
                            conn.execute("UPDATE entries SET status = ? WHERE source_path = ?", ("pending", row["source_path"]))
                            conn.commit()
                            st.toast("Entry restored to pending.", icon="🔄")
                            st.rerun()
                        finally:
                            conn.close()
        else:
            st.info("No accepted entries")

    with st.expander(f"Rejected ({len(rejected_display)})", expanded=False):
        if not rejected_display.empty:
            df_to_show = rejected_display.copy()
            df_to_show.index = df_to_show.index + 1
            st.dataframe(
                df_to_show[list(column_mapping.keys())].rename(columns=column_mapping),
                use_container_width=True,
                height=300
            )
            # A simple restore list below the table
            st.markdown("**Επαναφορά σε εκκρεμότητα:**")
            for i, row in rejected_display.iterrows():
                entry_name = row['client_name'] or row['invoice_number'] or row['source']
                cols = st.columns(2)
                with cols[0]:
                    st.markdown(f"Εγγραφή {i+1}: {entry_name}")
                with cols[1]:
                    st.button(
                        "🔄",
                        key=f"restore_rejected_{row['index']}",
                        use_container_width=False,
                        help="Επαναφορά σε εκκρεμότητα"
                    )
                    if st.session_state.get(f"restore_rejected_{row['index']}"):
                        conn = sqlite3.connect("data.db")
                        try:
                            conn.execute("UPDATE entries SET status = ? WHERE source_path = ?", ("pending", row["source_path"]))
                            conn.commit()
                            st.toast("Entry restored to pending.", icon="🔄")
                            st.rerun()
                        finally:
                            conn.close()
        else:
            st.info("No rejected entries")
            
    # ===================== Google Sheets Export =====================
    st.subheader("Export to Google Sheets")
    # Highlight Google Sheets export during tutorial step 5
    tutorial_placeholder = st.empty()
    if st.session_state.get("tutorial_step") == 5:
        tutorial_placeholder.markdown(
            "<div id='pending-tutorial-anchor' style='margin:140px auto 0 auto;'></div>"
            "<div id='tutorial'>Μην ξεχνάτε πως μπορείτε να επαναφέρετε εγγραφές από τις λίστες Accepted-Rejected<br>πατώντας επαναφορά εγγραφής.  <br>🎉 Το tutorial ολοκληρώθηκε!<br>"
            "Μπορείτε να ξεκινήσετε ξανά όποτε θέλετε πάνω.</div>",
            unsafe_allow_html=True
        )

        if st.button("Τέλος Tutorial", key="tutorial_end"):
            st.session_state["tutorial_step"] = 0
            tutorial_placeholder.empty()
            st.rerun()

        scroll_to("pending-tutorial-anchor")
    elif st.session_state.get("tutorial_step") < 4:
        tutorial_placeholder.markdown("<div style='height: 18px;'></div>", unsafe_allow_html=True)
    
    if not sheet_id:
        st.warning("Εισαγάγετε το Google sheets ID στην πλαϊνή γραμμή για να ενεργοποιήσετε την εξαγωγή")

    if st.button("⬆️ Push all entries to Google Sheets", 
                 disabled=not sheet_id,
                 type="primary",
                 use_container_width=False) and sheet_id:
        try:
            # Prepare export data
            export_columns = [
                "type", "source", "date", "client_name", "email", "phone", "company",
                "service_interest", "amount", "vat", "total_amount", "invoice_number",
                "priority", "message"
            ]
            # Ensure all columns exist
            for col in export_columns:
                if col not in combined_df.columns:
                    combined_df[col] = ""

            # 1. Accepted items (detailed)
            accepted_df = combined_df[combined_df["status"] == "accepted"].copy()
            accepted_df = accepted_df[export_columns]

            # 2. Rejected items (detailed)
            rejected_df = combined_df[combined_df["status"] == "rejected"].copy()
            rejected_df = rejected_df[export_columns]

            # Ensure numeric columns for summary calculations and export
            for col in ["amount", "vat", "total_amount"]:
                if col in accepted_df.columns:
                    accepted_df[col] = pd.to_numeric(accepted_df[col], errors="coerce")
                if col in rejected_df.columns:
                    rejected_df[col] = pd.to_numeric(rejected_df[col], errors="coerce")

            # 3. Summary: total amounts of accepted items
            summary_data = {
                "Total Amount": [accepted_df["total_amount"].sum()],
                "Total VAT": [accepted_df["vat"].sum()],
                "Total Net": [accepted_df["amount"].sum()],
                "Count": [len(accepted_df)],
            }
            summary_df = pd.DataFrame(summary_data)
            

            with st.spinner("Connecting to Google Sheets..."):
                sh = connect_gsheet(sheet_id, creds_path=creds_path, creds_dict=creds_dict)

                # Write summary
                ws_summary = upsert_worksheet(sh, "Summary", rows=10, cols=10)
                ws_summary.clear()
                set_with_dataframe(ws_summary, summary_df, include_index=False, include_column_header=True, resize=True)

                # Write accepted
                ws_accepted = upsert_worksheet(sh, "Accepted", rows=len(accepted_df)+10, cols=len(export_columns)+2)
                ws_accepted.clear()
                set_with_dataframe(ws_accepted, accepted_df, include_index=False, include_column_header=True, resize=True)

                # Write rejected
                ws_rejected = upsert_worksheet(sh, "Rejected", rows=len(rejected_df)+10, cols=len(export_columns)+2)
                ws_rejected.clear()
                set_with_dataframe(ws_rejected, rejected_df, include_index=False, include_column_header=True, resize=True)

            # 4. Accepted Invoices tab (only accepted entries of type INVOICE)
            accepted_invoices_df = accepted_df[accepted_df["type"] == "INVOICE"].copy()
            accepted_invoices_export = accepted_invoices_df[[
                "invoice_number", "date", "client_name", "amount", "vat", "total_amount"
            ]].rename(columns={
                "invoice_number": "Αριθμός τιμολογίου",
                "date": "Ημερομηνία",
                "client_name": "Πελάτης",
                "amount": "Ποσό",
                "vat": "ΦΠΑ",
                "total_amount": "Σύνολο"
            })
            ws_acc_inv = upsert_worksheet(sh, "Accepted Invoices", rows=len(accepted_invoices_export)+10, cols=6)
            ws_acc_inv.clear()
            set_with_dataframe(ws_acc_inv, accepted_invoices_export, include_index=False, include_column_header=True, resize=True)

            # 5. Accepted Forms tab (only accepted entries of type FORM)
            accepted_forms_df = accepted_df[accepted_df["type"] == "FORM"].copy()
            accepted_forms_export = accepted_forms_df[[
                "client_name", "email", "phone", "company", "service_interest", "message"
            ]].rename(columns={
                "client_name": "Όνομα",
                "email": "Email",
                "phone": "Τηλέφωνο",
                "company": "Εταιρεία",
                "service_interest": "Υπηρεσία ενδιαφέροντος",
                "message": "Μήνυμα" 
            })

            ws_acc_forms = upsert_worksheet(sh, "Accepted Forms", rows=len(accepted_forms_export)+10, cols=6)
            ws_acc_forms.clear()
            set_with_dataframe(ws_acc_forms, accepted_forms_export, include_index=False, include_column_header=True, resize=True)

            # 6. Accepted Emails tab (only accepted entries of type EMAIL)
            accepted_emails_df = accepted_df[accepted_df["type"] == "EMAIL"].copy()
            accepted_emails_export = accepted_emails_df[[
                "client_name", "email", "phone", "company", "service_interest", "message"
            ]].rename(columns={
                "client_name": "Όνομα",
                "email": "Email",
                "phone": "Τηλέφωνο",
                "company": "Εταιρεία",
                "service_interest": "Υπηρεσία ενδιαφέροντος",
                "message": "Μήνυμα"
            })

            ws_acc_emails = upsert_worksheet(sh, "Accepted Emails", rows=len(accepted_emails_export)+10, cols=6)
            ws_acc_emails.clear()
            set_with_dataframe(ws_acc_emails, accepted_emails_export, include_index=False, include_column_header=True, resize=True)

            st.success(f"✅ Exported summary, accepted, and rejected entries to Google Sheets!")
            st.balloons()

        except Exception as e:
            st.error(f"Export failed: {str(e)}")
            logger.exception("Google Sheets export error")


def email_to_pdf(email_data: Dict[str, Any]) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Fonts 
    font_path = "assets/DejaVuSans.ttf"
    pdf.add_font("DejaVu", "", font_path, uni=True)
    pdf.add_font("DejaVu-Bold", "", font_path, uni=True)

    # Colors
    HEADER_BLUE = (44, 62, 80)
    BORDER      = (200, 200, 200)
    BG_GRAY     = (248, 248, 248)

    pdf.add_page()

    # ---- Title ----
    pdf.set_font("DejaVu-Bold", size=18)
    pdf.set_text_color(*HEADER_BLUE)
    pdf.cell(0, 12, "ΤΙΜΟΛΟΓΙΟ", ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # ---- Meta info ----
    pdf.set_font("DejaVu", size=11)
    meta = [
        ("Αριθμός",   email_data.get("invoice_number", "")),
        ("Αποστολέας", email_data.get("from_name", "")),
        ("Παραλήπτης", email_data.get("to", "")),
        ("Ημερομηνία", email_data.get("date_raw", "")),
    ]
    for label, val in meta:
        pdf.set_font("DejaVu-Bold", size=11)
        pdf.cell(35, 7, f"{label}:", ln=0)
        pdf.set_font("DejaVu", size=11)
        pdf.cell(0, 7, val or "—", ln=1)
    pdf.ln(4)

    # ---- Message box ----
    pdf.set_font("DejaVu-Bold", size=12)
    pdf.set_text_color(*HEADER_BLUE)
    pdf.set_text_color(0, 0, 0)

    body = email_data.get("message", "") or ""
    lines = body.splitlines() or [""]

    # Draw background box
    x0, y0 = pdf.get_x(), pdf.get_y()
    box_w = pdf.w - pdf.l_margin - pdf.r_margin
    line_height = 7
    box_h = max(20, len(lines) * line_height + 10)
    pdf.set_fill_color(*BG_GRAY)
    pdf.set_draw_color(*BORDER)
    pdf.rect(x0, y0, box_w, box_h, style="DF")

    # Write text inside box
    pdf.set_xy(x0 + 3, y0 + 5)
    pdf.set_font("DejaVu", size=11)
    for ln in lines:
        pdf.multi_cell(box_w - 6, line_height, ln)
    pdf.ln(4)

    # ---- Footer ----
    pdf.set_font("DejaVu", size=9)
    pdf.set_text_color(120, 120, 120)
    pdf.set_text_color(0, 0, 0)

    return pdf.output(dest="S").encode("latin1")

def html_invoice_to_pdf(html_path: str) -> bytes:
    """Convert HTML invoice to PDF with full HTML/CSS rendering using WeasyPrint."""

    # Read HTML file
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # a CSS file for styling, you can add it here
    css_path = "styles.css"
    css_files = []
    if os.path.exists(css_path):
        css_files.append(css_path)

    # Generate PDF
    pdf_bytes = HTML(string=html_content, base_url=os.path.dirname(html_path)).write_pdf(stylesheets=css_files)
    return pdf_bytes



if __name__ == "__main__":
    main()