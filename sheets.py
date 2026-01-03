"""
sheets.py - Google Sheets helper (defensive).
Exposes initialize(service_account_path) and a small set of helpers used by the app:
- read_rules()
- get_timetable_for_today()
- get_timetable_sheet_content()
- list_gallery()
- fetch_drive_image_bytes(file_id)  -> uses drive helper if available
If google libs are missing, these return safe defaults.
"""

import os
import logging
import json
from datetime import datetime
from typing import Tuple, List

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GOOGLE_API_AVAILABLE = False
_SERVICE_ACCOUNT_PATH = None
_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]

# lazy imports
_credentials_mod = None
_build = None
_drive = None

def initialize(service_account_path: str | None = None):
    """
    Initialize Sheets helper. Pass the service account path (or set SERVICE_ACCOUNT_JSON_PATH env var).
    Returns {"ok": True/False, "reason": "..."}
    """
    global GOOGLE_API_AVAILABLE, _SERVICE_ACCOUNT_PATH, _credentials_mod, _build, _drive
    _SERVICE_ACCOUNT_PATH = service_account_path or os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "") or ""
    try:
        from google.oauth2 import service_account as _creds_mod
        from googleapiclient.discovery import build as _build_f
        _credentials_mod = _creds_mod
        _build = _build_f
        # import local drive helper if available (drive.py in same project)
        try:
            import drive as _drive_mod
            _drive = _drive_mod
        except Exception:
            _drive = None
        GOOGLE_API_AVAILABLE = True
        logger.info("sheets: google-api client appears available")
        return {"ok": True}
    except Exception as e:
        GOOGLE_API_AVAILABLE = False
        logger.warning("sheets.initialize: google apis not available: %s", e)
        # still set _drive if present for image helpers
        try:
            import drive as _drive_mod
            _drive = _drive_mod
        except Exception:
            _drive = None
        return {"ok": False, "reason": str(e)}

def _make_sheets_service():
    if not GOOGLE_API_AVAILABLE:
        raise RuntimeError("google api not available")
    if not _SERVICE_ACCOUNT_PATH or not os.path.exists(_SERVICE_ACCOUNT_PATH):
        raise RuntimeError("service account JSON not found at " + str(_SERVICE_ACCOUNT_PATH))
    creds = _credentials_mod.service_account.Credentials.from_service_account_file(_SERVICE_ACCOUNT_PATH, scopes=_SHEETS_SCOPES)
    svc = _build("sheets", "v4", credentials=creds, cache_discovery=False)
    return svc

# ---- helper: read rules ----
def read_rules(spreadsheet_id: str | None = None, sheet_range: str | None = None) -> List[dict]:
    """
    Returns a list of rule dicts, or [] on failure.
    If spreadsheet_id is None, we try the env var RULES_SPREADSHEET_ID and RULES_RANGE.
    """
    if not GOOGLE_API_AVAILABLE:
        logger.debug("read_rules: google api not available; returning []")
        return []
    spreadsheet_id = spreadsheet_id or os.environ.get("RULES_SPREADSHEET_ID")
    sheet_range = sheet_range or os.environ.get("RULES_RANGE", "Rules!A2:B500")
    if not spreadsheet_id:
        logger.debug("read_rules: no spreadsheet_id configured")
        return []
    try:
        svc = _make_sheets_service()
        resp = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=sheet_range).execute()
        rows = resp.get("values", []) or []
        out = []
        for r in rows:
            # simple mapping: [title, text]
            title = r[0] if len(r) > 0 else ""
            text = r[1] if len(r) > 1 else ""
            out.append({"title": title, "text": text})
        return out
    except Exception:
        logger.exception("read_rules failed")
        return []

# ---- helper: timetable ----
def get_timetable_for_today(spreadsheet_id: str | None = None, sheet_name: str | None = None) -> List[dict]:
    """
    Basic timetable fetcher. Returns list of rows (dicts) or [].
    This is intentionally generic; adapt your sheet and mapping as needed.
    """
    if not GOOGLE_API_AVAILABLE:
        return []
    spreadsheet_id = spreadsheet_id or os.environ.get("TIMETABLE_SPREADSHEET_ID")
    sheet_name = sheet_name or os.environ.get("TIMETABLE_SHEET_NAME", "Timetable")
    if not spreadsheet_id:
        logger.debug("get_timetable_for_today: no spreadsheet id configured")
        return []
    try:
        svc = _make_sheets_service()
        rng = f"{sheet_name}!A1:Z200"
        resp = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
        rows = resp.get("values", []) or []
        # rudimentary conversion: header -> dict rows
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        out = []
        for r in rows[1:]:
            rowd = {}
            for i, h in enumerate(header):
                rowd[h or f"col{i}"] = r[i] if i < len(r) else ""
            out.append(rowd)
        return out
    except Exception:
        logger.exception("get_timetable_for_today failed")
        return []

def get_timetable_sheet_content(spreadsheet_id: str | None = None, sheet_name: str | None = None) -> Tuple[str, list]:
    """
    Returns (title, rows) or (None, []).
    """
    try:
        rows = get_timetable_for_today(spreadsheet_id, sheet_name)
        title = os.environ.get("TIMETABLE_TITLE") or "Timetable"
        return title, rows
    except Exception:
        return None, []

# ---- gallery listing ----
def list_gallery(parent_folder_id: str | None = None, sample_limit: int = 12):
    """
    Uses drive.from_google_drive if available to list images in a parent folder.
    Returns list of tuples (name, (folder_name, file_id)) as in your previous drive code.
    """
    try:
        parent_folder_id = parent_folder_id or os.environ.get("DRIVE_PARENT_FOLDER_ID", "")
        if not parent_folder_id or not _drive:
            return []
        files = _drive.from_google_drive(parent_folder_id)
        # simple mapping: group by folder name using file metadata (if available)
        out = []
        for f in files:
            name = f.get("name") or f.get("id")
            folder = f.get("description") or ""  # might not have folder; we keep description
            out.append((name, (folder, f.get("id"))))
        if len(out) <= sample_limit:
            return out
        import random
        return random.sample(out, sample_limit)
    except Exception:
        logger.exception("list_gallery failed")
        return []

# ---- image helper used by app.drive_image ----
def fetch_drive_image_bytes(file_id: str):
    """
    Prefer to use the drive helper if available; otherwise return (None, None).
    """
    try:
        if _drive and hasattr(_drive, "fetch_drive_image_bytes"):
            return _drive.fetch_drive_image_bytes(file_id)
    except Exception:
        logger.exception("sheets.fetch_drive_image_bytes via drive helper failed")
    return None, None

# ---- small convenience: read arbitrary sheet range into list of dicts ----
def read_sheet(spreadsheet_id: str, range_name: str):
    if not GOOGLE_API_AVAILABLE:
        return []
    try:
        svc = _make_sheets_service()
        resp = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
        rows = resp.get("values", []) or []
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        out = []
        for r in rows[1:]:
            rowd = {}
            for i, h in enumerate(header):
                rowd[h or f"col{i}"] = r[i] if i < len(r) else ""
            out.append(rowd)
        return out
    except Exception:
        logger.exception("read_sheet failed")
        return []

# End of sheets.py
