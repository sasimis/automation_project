# app/utils/storage.py

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from app.utils.schema import LeadRecord, InvoiceRecord

# Optional Google Sheets deps (lazy import if available)
try:
    import gspread  # type: ignore
    from google.oauth2.service_account import Credentials  # type: ignore
except Exception:  # pragma: no cover
    gspread = None
    Credentials = None


class Storage:
    """
    Persist approved records to:
      - local Excel workbook (multi-sheet)
      - Google Sheets (optional), if credentials + sheet id are provided

    Also writes an audit log (JSON lines).
    """

    def __init__(
        self,
        excel_output: str,
        audit_log: str,
        google_sheets_enabled: bool = False,
        google_sheet_id: str = "",
        google_worksheet_name: str = "Approved",
        excel_engine: str = "openpyxl",
        max_retries: int = 2,
        retry_wait_s: float = 0.35,
    ) -> None:
        self.excel_output = Path(excel_output)
        self.audit_log = Path(audit_log)
        self.google_sheets_enabled = bool(google_sheets_enabled and gspread is not None)
        self.google_sheet_id = google_sheet_id
        self.google_worksheet_name = google_worksheet_name
        self.excel_engine = excel_engine
        self.max_retries = max_retries
        self.retry_wait_s = retry_wait_s
        self._ensure_files()

    # -------------------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------------------

    def save_lead(self, lead: LeadRecord) -> None:
        """
        Append a single approved lead to Excel + (optionally) Google Sheets.
        Also write an audit log line.
        """
        df = pd.DataFrame([lead.model_dump()])
        self._append_excel(df, sheet_name="Leads")
        self._append_gsheet(df, worksheet_name="Leads")
        self.audit({"type": "lead", "status": "approved", "data": lead.model_dump()})

    def save_invoice(self, inv: InvoiceRecord) -> None:
        """
        Append a single approved invoice to Excel + (optionally) Google Sheets.
        Also write an audit log line.
        """
        df = pd.DataFrame([inv.model_dump()])
        self._append_excel(df, sheet_name="Invoices")
        self._append_gsheet(df, worksheet_name="Invoices")
        self.audit({"type": "invoice", "status": "approved", "data": inv.model_dump()})

    def audit(self, entry: dict) -> None:
        """
        Append a JSON line to the audit trail.
        """
        try:
            with self.audit_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to write audit log: {e}")

    # -------------------------------------------------------------------------------------
    # Private helpers — Excel
    # -------------------------------------------------------------------------------------

    def _ensure_files(self) -> None:
        self.excel_output.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        if not self.audit_log.exists():
            self.audit_log.write_text("", encoding="utf-8")

    def _append_excel(self, df: pd.DataFrame, sheet_name: str) -> None:
        """
        Robust Excel append:
          - Creates workbook if missing
          - Creates sheet if missing
          - If sheet exists, reads existing and writes back concatenated data
        """
        for attempt in range(self.max_retries + 1):
            try:
                if not self.excel_output.exists():
                    with pd.ExcelWriter(self.excel_output, engine=self.excel_engine) as xw:
                        df.to_excel(xw, sheet_name=sheet_name, index=False)
                    return

                # File exists → try to read existing sheet (if any)
                try:
                    existing = pd.read_excel(self.excel_output, sheet_name=sheet_name)
                    out = pd.concat([existing, df], ignore_index=True)
                    mode = "a"
                    if_sheet_exists = "replace"  # overwrite the sheet with merged content
                except Exception:
                    # Sheet missing → write just df as a new sheet
                    out = df
                    mode = "a"
                    if_sheet_exists = None  # not used if new sheet

                with pd.ExcelWriter(
                    self.excel_output,
                    mode=mode,
                    engine=self.excel_engine,
                    if_sheet_exists=if_sheet_exists,  # type: ignore[arg-type]
                ) as xw:
                    out.to_excel(xw, sheet_name=sheet_name, index=False)
                return

            except Exception as e:  # pragma: no cover
                logger.warning(
                    f"Excel write attempt {attempt+1}/{self.max_retries+1} failed: {e}"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_wait_s)
                else:
                    logger.error("Giving up on Excel write after retries.")

    # -------------------------------------------------------------------------------------
    # Private helpers — Google Sheets
    # -------------------------------------------------------------------------------------

    def _append_gsheet(self, df: pd.DataFrame, worksheet_name: str) -> None:
        """
        Append dataframe rows to a Google Sheets worksheet.
        - No-ops if Sheets integration is disabled or credentials missing
        - Auto-creates worksheet when needed
        - Preserves header row on first write
        """
        if not self.google_sheets_enabled:
            return

        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_path or not os.path.exists(creds_path):
            logger.warning(
                "Google Sheets enabled but no valid GOOGLE_APPLICATION_CREDENTIALS found."
            )
            return

        try:
            # Use both Sheets & Drive scopes for reliability on open_by_key / worksheet ops
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)  # type: ignore[arg-type]
            gc = gspread.authorize(creds)  # type: ignore[operator]
            sh = gc.open_by_key(self.google_sheet_id)  # type: ignore[union-attr]

            # Get or create the worksheet
            try:
                ws = sh.worksheet(worksheet_name)
                # If worksheet is empty and we have columns, write header first
                if not ws.get_all_values():
                    ws.update([df.columns.astype(str).tolist()])
            except Exception:
                ws = sh.add_worksheet(title=worksheet_name, rows="1000", cols="26")
                ws.update([df.columns.astype(str).tolist()])

            # Append only data rows (no header duplication)
            if not df.empty:
                rows = df.fillna("").astype(str).values.tolist()
                ws.append_rows(rows)

        except Exception as e:  # pragma: no cover
            logger.error(f"GSheets append failed: {e}")
