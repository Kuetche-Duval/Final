import os
import time
import gspread
from datetime import datetime

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
gc = gspread.service_account(filename=SERVICE_ACCOUNT_PATH)

timetable_key = os.environ.get("TIMETABLE_SHEET_KEY")
questions_key = os.environ.get("QUESTIONS_SHEET_KEY")
group_rules_key = os.environ.get("GROUP_RULES_SHEET_KEY")

reload_time = datetime.now().time()

# --- Cache (separate timestamps for each cached item) ---
_cache = {
    "timetable": None,
    "timetable_ts": 0,
    "questions": None,
    "questions_ts": 0,
    "rules": None,
    "rules_ts": 0
}

CACHE_TTL = int(os.environ.get("SHEETS_CACHE_TTL", "180"))

def get_work_sheet(key):
    if not key:
        raise RuntimeError("Sheet key missing in .env")
    return gc.open_by_key(key).sheet1

def from_google_sheets():
    # timetable cache
    if _cache["timetable"] and time.time() - _cache["timetable_ts"] < CACHE_TTL:
        return _cache["timetable"]
    data = get_work_sheet(timetable_key).get_all_values()
    _cache["timetable"] = data
    _cache["timetable_ts"] = time.time()
    return data

def check_date():
    week = datetime.now().strftime("%a").upper()
    col = get_work_sheet(timetable_key).col_values(1)
    return [(i, val) for i, val in enumerate(col, start=1) if val == week]

def append_row(row):
    # append row and invalidate questions cache immediately so new rows appear quickly
    ws = get_work_sheet(questions_key)
    ws.append_row(row)
    _cache["questions"] = None
    _cache["questions_ts"] = 0

def get_questions():
    # Use independent questions cache
    if _cache["questions"] and time.time() - _cache["questions_ts"] < CACHE_TTL:
        return _cache["questions"]
    data = get_work_sheet(questions_key).get_all_values()
    # remove fully-empty rows (often Google returns extra empty trailing rows)
    data = [row for row in data if any(cell is not None and str(cell).strip() != "" for cell in row)]
    _cache["questions"] = data
    _cache["questions_ts"] = time.time()
    return data

def clear_sheet():
    get_work_sheet(questions_key).clear()
    _cache["questions"] = None
    _cache["questions_ts"] = 0

def read_rules():
    if _cache["rules"] and time.time() - _cache["rules_ts"] < CACHE_TTL:
        return _cache["rules"]
    data = get_work_sheet(group_rules_key).get_all_values()
    _cache["rules"] = data
    _cache["rules_ts"] = time.time()
    return data
