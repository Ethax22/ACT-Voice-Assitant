"""Admission-intent lead persistence — Phase 8.

Decoupled on purpose (Implementation Plan §9): intent detection and
slot-filling happen in main.py via the LLM's own tool-calling; this module
only knows how to take a finished lead dict and get it into Google Sheets.
Swapping the destination later stays isolated to this file.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("rag_backend.leads")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID = os.environ.get("LEADS_SHEET_ID")
_CREDS_PATH = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

# Fixed offset rather than zoneinfo("Asia/Kolkata"): IST has no DST, and this
# avoids needing the tzdata package on Windows hosts. Lead timestamps are IST
# because admissions staff read the sheet directly; conversation logs stay UTC.
_IST = timezone(timedelta(hours=5, minutes=30))

_worksheet = None
_init_attempted = False


def _get_worksheet():
    """Lazy singleton — a missing/misconfigured Sheet must not crash the
    backend at import time (RAG answers should keep working even if lead
    capture is unconfigured, e.g. in a dev environment)."""
    global _worksheet, _init_attempted
    if _worksheet is not None or _init_attempted:
        return _worksheet
    _init_attempted = True

    if not _SHEET_ID or not _CREDS_PATH:
        logger.warning(
            "Lead capture disabled: LEADS_SHEET_ID and/or "
            "GOOGLE_SERVICE_ACCOUNT_JSON not set — captured leads will be "
            "logged but not written to Sheets."
        )
        return None

    try:
        creds = Credentials.from_service_account_file(_CREDS_PATH, scopes=_SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(_SHEET_ID)
        worksheet = sheet.sheet1
        header = worksheet.row_values(1)
        if not header:
            worksheet.append_row(
                ["timestamp_ist", "name", "phone", "email", "preferred_course", "language", "source"]
            )
        _worksheet = worksheet
    except Exception:
        logger.exception(
            "Lead capture disabled: failed to open Google Sheet %s — check "
            "GOOGLE_SERVICE_ACCOUNT_JSON path, that the sheet is shared with "
            "the service account's email, and LEADS_SHEET_ID.",
            _SHEET_ID,
        )
        _worksheet = None

    return _worksheet


def normalize_indian_mobile(raw: str) -> str | None:
    """Return a clean 10-digit Indian mobile number, or None if `raw` isn't
    one. Accepts spaces/dashes and a leading +91, 91 or 0."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def save_lead(
    name: str,
    phone: str,
    email: str | None = None,
    preferred_course: str | None = None,
    language: str = "en",
    source: str = "chat",
) -> bool:
    """Append one captured lead as a row. Returns whether it reached Sheets
    (a False here still means the lead was logged — check the logs)."""
    row = [
        datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S"),
        name,
        phone,
        email or "",
        preferred_course or "",
        language,
        source,
    ]
    logger.info("Lead captured: name=%r phone=%r email=%r course=%r lang=%s source=%s",
                name, phone, email, preferred_course, language, source)

    worksheet = _get_worksheet()
    if worksheet is None:
        return False

    try:
        worksheet.append_row(row)
        return True
    except Exception:
        logger.exception("Failed to append lead row to Google Sheet %s", _SHEET_ID)
        return False
