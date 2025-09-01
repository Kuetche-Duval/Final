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

# --- Cache ---
_cache = {"timetable": None, "questions": None, "rules": None, "timestamp": 0}
CACHE_TTL = int(os.environ.get("SHEETS_CACHE_TTL", "180"))

def get_work_sheet(key):
    if not key:
        raise RuntimeError("Sheet key missing in .env")
    return gc.open_by_key(key).sheet1

def from_google_sheets():
    if _cache["timetable"] and time.time() - _cache["timestamp"] < CACHE_TTL:
        return _cache["timetable"]
    data = get_work_sheet(timetable_key).get_all_values()
    _cache["timetable"] = data
    _cache["timestamp"] = time.time()
    return data

def check_date():
    week = datetime.now().strftime("%a").upper()
    col = get_work_sheet(timetable_key).col_values(1)
    return [(i, val) for i, val in enumerate(col, start=1) if val == week]

def append_row(row):
    get_work_sheet(questions_key).append_row(row)

def get_questions():
    if _cache["questions"] and time.time() - _cache["timestamp"] < CACHE_TTL:
        return _cache["questions"]
    data = get_work_sheet(questions_key).get_all_values()
    _cache["questions"] = data
    _cache["timestamp"] = time.time()
    return data

def clear_sheet():
    get_work_sheet(questions_key).clear()

def read_rules():
    if _cache["rules"] and time.time() - _cache["timestamp"] < CACHE_TTL:
        return _cache["rules"]
    data = get_work_sheet(group_rules_key).get_all_values()
    _cache["rules"] = data
    _cache["timestamp"] = time.time()
    return data
