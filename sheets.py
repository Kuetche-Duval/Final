# sheets.py (improved + subscription helpers)
import os
import time
import logging
import threading
import json
from datetime import datetime
from pathlib import Path

import gspread

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

timetable_key = os.environ.get("TIMETABLE_SHEET_KEY")
questions_key = os.environ.get("QUESTIONS_SHEET_KEY")
group_rules_key = os.environ.get("GROUP_RULES_SHEET_KEY")

CACHE_TTL_TIMETABLE = int(os.environ.get("SHEETS_TIMETABLE_TTL", "300"))
CACHE_TTL_QUESTIONS = int(os.environ.get("SHEETS_QUESTIONS_TTL", "30"))
CACHE_TTL_RULES = int(os.environ.get("SHEETS_RULES_TTL", "300"))

_cache = {
    "timetable": None,
    "timetable_ts": 0,
    "questions": None,
    "questions_ts": 0,
    "rules": None,
    "rules_ts": 0,
}
_cache_lock = threading.Lock()

_gc = None
_gc_lock = threading.Lock()

# small retry helper
def _retry(fn, attempts=3, base_delay=0.5, *a, **kw):
    last_exc = None
    for i in range(attempts):
        try:
            return fn(*a, **kw)
        except Exception as e:
            last_exc = e
            logger.debug("Attempt %d/%d failed for %s: %s", i+1, attempts, getattr(fn, "__name__", fn), e)
            time.sleep(base_delay * (2 ** i))
    logger.exception("All attempts failed for %s", getattr(fn, "__name__", fn))
    raise last_exc

def _get_client():
    global _gc
    with _gc_lock:
        if _gc is None:
            if not Path(SERVICE_ACCOUNT_PATH).exists():
                raise RuntimeError(f"Service account file not found at {SERVICE_ACCOUNT_PATH}")
            try:
                _gc = gspread.service_account(filename=SERVICE_ACCOUNT_PATH)
            except Exception:
                logger.exception("Failed to create gspread client")
                raise
        return _gc

def get_work_sheet(key):
    if not key:
        raise RuntimeError("Sheet key missing in environment configuration.")
    client = _get_client()
    try:
        return _retry(lambda: client.open_by_key(key).sheet1)
    except Exception:
        logger.exception("Failed to open sheet by key: %s", key)
        raise

def from_google_sheets():
    now = time.time()
    with _cache_lock:
        if _cache["timetable"] and (now - _cache["timetable_ts"] < CACHE_TTL_TIMETABLE):
            return _cache["timetable"]
    if not timetable_key:
        logger.warning("No TIMETABLE_SHEET_KEY configured")
        return []
    ws = get_work_sheet(timetable_key)
    data = _retry(ws.get_all_values)
    with _cache_lock:
        _cache["timetable"] = data
        _cache["timetable_ts"] = time.time()
    return data

def check_date():
    try:
        week = datetime.now().strftime("%a").upper()
        col = get_work_sheet(timetable_key).col_values(1)
        return [(i, val) for i, val in enumerate(col, start=1) if val == week]
    except Exception:
        logger.exception("check_date failed")
        return []

def append_row(row):
    if not isinstance(row, (list, tuple)):
        raise ValueError("append_row expects a list/tuple: [category, username, question, attempt, timestamp]")
    row = list(row)

    # normalize values
    while len(row) < 5:
        row.append("")
    category = str(row[0]).strip() if row[0] is not None else ""
    username = str(row[1]).strip() if row[1] else "Anonymous"
    question_text = row[2] or ""
    attempt_text = row[3] or ""
    timestamp = row[4] or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    full_row = [category, username, question_text, attempt_text, timestamp]

    if not questions_key:
        logger.warning("QUESTIONS_SHEET_KEY not set; append_row will skip writing")
        return

    ws = get_work_sheet(questions_key)
    try:
        _retry(lambda: ws.append_row(full_row, value_input_option="USER_ENTERED"), attempts=3)
    except Exception:
        logger.exception("append_row failed after retries, trying one last time")
        ws.append_row(full_row, value_input_option="USER_ENTERED")

    with _cache_lock:
        _cache["questions"] = None
        _cache["questions_ts"] = 0

def get_questions():
    now = time.time()
    with _cache_lock:
        if _cache["questions"] and (now - _cache["questions_ts"] < CACHE_TTL_QUESTIONS):
            return _cache["questions"]

    if not questions_key:
        logger.warning("QUESTIONS_SHEET_KEY not set; get_questions returns []")
        return []

    ws = get_work_sheet(questions_key)
    data = _retry(ws.get_all_values)
    filtered = [row for row in data if any(cell is not None and str(cell).strip() != "" for cell in row)]

    normalized = []
    for row in filtered:
        row = list(row)
        while len(row) < 5:
            row.append("")
        normalized.append(row)

    with _cache_lock:
        _cache["questions"] = normalized
        _cache["questions_ts"] = time.time()
    return normalized

def clear_sheet():
    if not questions_key:
        logger.warning("QUESTIONS_SHEET_KEY not set; clear_sheet does nothing")
        return
    ws = get_work_sheet(questions_key)
    _retry(ws.clear)
    with _cache_lock:
        _cache["questions"] = None
        _cache["questions_ts"] = 0

# --- New utilities for admin deletion ---
def delete_rows_by_indices(indices):
    """
    indices: list of sheet row numbers (1-based) to delete. Deletes in descending order so indices remain correct.
    """
    if not questions_key:
        raise RuntimeError("QUESTIONS_SHEET_KEY not configured")
    if not indices:
        return
    ws = get_work_sheet(questions_key)
    # deduplicate and only keep integers >= 1
    idxs = sorted({int(i) for i in indices if isinstance(i, (int, str)) and str(i).strip().isdigit() and int(i) >= 1}, reverse=True)
    for i in idxs:
        try:
            _retry(lambda row=i: ws.delete_rows(row), attempts=3)
            logger.info("Deleted sheet row %s", i)
        except Exception:
            logger.exception("Failed to delete row %s", i)
            # continue attempting to delete other rows

    with _cache_lock:
        _cache["questions"] = None
        _cache["questions_ts"] = 0

def delete_rows_by_subject(subject):
    """
    Deletes all rows where the first cell (subject/ category) exactly matches `subject`.
    """
    if not questions_key:
        raise RuntimeError("QUESTIONS_SHEET_KEY not configured")
    if not subject:
        return
    ws = get_work_sheet(questions_key)
    data = _retry(ws.get_all_values)
    indices = []
    for idx, row in enumerate(data, start=1):
        if not row:
            continue
        first = str(row[0]).strip()
        if first == str(subject).strip():
            indices.append(idx)
    if not indices:
        logger.info("No rows found for subject %s", subject)
        return
    delete_rows_by_indices(indices)

def read_rules():
    now = time.time()
    with _cache_lock:
        if _cache["rules"] and (now - _cache["rules_ts"] < CACHE_TTL_RULES):
            return _cache["rules"]
    if not group_rules_key:
        logger.warning("GROUP_RULES_SHEET_KEY not configured; read_rules returns []")
        return []
    ws = get_work_sheet(group_rules_key)
    data = _retry(ws.get_all_values)
    with _cache_lock:
        _cache["rules"] = data
        _cache["rules_ts"] = time.time()
    return data

# --- Subscription helpers (store subscriptions as special rows by default) ---
def save_subscription(subject, client_id, subscription_obj):
    """
    Save a push subscription. By default this appends a special row marked with __subscription__.
    You can change schema to store subscriptions in a dedicated sheet.
    """
    try:
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        append_row(["__subscription__", subject or "Other", client_id or "", json.dumps(subscription_obj), timestamp])
    except Exception:
        logger.exception("save_subscription failed")
        raise

def list_subscriptions(subject=None):
    """
    Return list of subscription dicts (each element is {'subject':..., 'client_id':..., 'subscription': {...}}).
    If subject is provided, filter by subject.
    """
    out = []
    rows = get_questions() or []
    for row in rows:
        try:
            if len(row) >= 4 and str(row[0]) == "__subscription__":
                subj = str(row[1])
                client_id = row[2]
                try:
                    sub = json.loads(row[3])
                except Exception:
                    sub = None
                if subject:
                    if subj == subject and sub:
                        out.append({"subject": subj, "client_id": client_id, "subscription": sub})
                else:
                    if sub:
                        out.append({"subject": subj, "client_id": client_id, "subscription": sub})
        except Exception:
            continue
    return out
