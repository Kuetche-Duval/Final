# sheets.py
import os
import time
import logging
from datetime import datetime
from pathlib import Path
import gspread

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

timetable_key = os.environ.get("TIMETABLE_SHEET_KEY")
questions_key = os.environ.get("QUESTIONS_SHEET_KEY")
group_rules_key = os.environ.get("GROUP_RULES_SHEET_KEY")

# per-resource TTLs
CACHE_TTL_TIMETABLE = int(os.environ.get("SHEETS_TIMETABLE_TTL", "300"))
CACHE_TTL_QUESTIONS = int(os.environ.get("SHEETS_QUESTIONS_TTL", "30"))
CACHE_TTL_RULES = int(os.environ.get("SHEETS_RULES_TTL", "300"))

# expose questions TTL for app
CACHE_TTL_QUESTIONS_PUBLIC = CACHE_TTL_QUESTIONS
CACHE_TTL_QUESTIONS = CACHE_TTL_QUESTIONS  # export name used by app

_cache = {
    "timetable": None,
    "timetable_ts": 0,
    "questions": None,
    "questions_ts": 0,
    "rules": None,
    "rules_ts": 0
}

_gc = None
def _get_client():
    global _gc
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
        return client.open_by_key(key).sheet1
    except Exception:
        logger.exception("Failed to open sheet by key: %s", key)
        raise

def from_google_sheets():
    now = time.time()
    if _cache["timetable"] and (now - _cache["timetable_ts"] < CACHE_TTL_TIMETABLE):
        return _cache["timetable"]
    ws = get_work_sheet(timetable_key)
    data = ws.get_all_values()
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
        raise ValueError("append_row expects a list/tuple: [category, username, question]")

    category = row[0].strip() if len(row) > 0 and row[0] is not None else ""
    username = (row[1].strip() if len(row) > 1 and row[1] else "Anonymous")
    question_text = (row[2] if len(row) > 2 and row[2] is not None else "")

    timestamp = datetime.now().strftime("%I:%M %p").lstrip("0")
    full_row = [category, username, question_text, timestamp]

    ws = get_work_sheet(questions_key)
    try:
        ws.append_row(full_row, value_input_option="USER_ENTERED")
    except Exception:
        logger.exception("append_row failed, retrying once")
        time.sleep(1)
        ws.append_row(full_row, value_input_option="USER_ENTERED")

    _cache["questions"] = None
    _cache["questions_ts"] = 0

def get_questions():
    now = time.time()
    if _cache["questions"] and (now - _cache["questions_ts"] < CACHE_TTL_QUESTIONS):
        return _cache["questions"]

    ws = get_work_sheet(questions_key)
    data = ws.get_all_values()
    filtered = [row for row in data if any(cell is not None and str(cell).strip() != "" for cell in row)]

    normalized = []
    for row in filtered:
        row = list(row)
        while len(row) < 4:
            row.append("")
        normalized.append(row)

    _cache["questions"] = normalized
    _cache["questions_ts"] = time.time()
    return normalized

def clear_sheet():
    ws = get_work_sheet(questions_key)
    ws.clear()
    _cache["questions"] = None
    _cache["questions_ts"] = 0

def read_rules():
    now = time.time()
    if _cache["rules"] and (now - _cache["rules_ts"] < CACHE_TTL_RULES):
        return _cache["rules"]
    ws = get_work_sheet(group_rules_key)
    data = ws.get_all_values()
    _cache["rules"] = data
    _cache["rules_ts"] = time.time()
    return data
