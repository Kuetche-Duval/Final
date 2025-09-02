# sheets.py
import os
import time
import gspread
from datetime import datetime

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
gc = gspread.service_account(filename=SERVICE_ACCOUNT_PATH)

timetable_key = os.environ.get("TIMETABLE_SHEET_KEY")
questions_key = os.environ.get("QUESTIONS_SHEET_KEY")
group_rules_key = os.environ.get("GROUP_RULES_SHEET_KEY")

# NOTE: reload_time is kept for compatibility but not used for automatic clearing here.
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
    """
    Append a new question row.

    Expected input: row = [category, username, question_text]
    The function appends a 4th column containing only the local server time (e.g. "6:40 PM").
    Ensures insertion starts from column A by providing all leading columns.
    """
    if not isinstance(row, (list, tuple)):
        raise ValueError("append_row expects a list/tuple: [category, username, question]")

    # normalize input to three elements
    category = row[0].strip() if len(row) > 0 and row[0] is not None else ""
    username = (row[1].strip() if len(row) > 1 and row[1] else "Anonymous")
    question_text = (row[2] if len(row) > 2 and row[2] is not None else "")

    # Time only, 12-hour format like messaging apps (strip leading zero)
    timestamp = datetime.now().strftime("%I:%M %p").lstrip("0")

    full_row = [category, username, question_text, timestamp]

    ws = get_work_sheet(questions_key)
    # append_row will add the row starting at column A since we provide full_row (no leading empty cells)
    ws.append_row(full_row, value_input_option="USER_ENTERED")
    # invalidate cache so new row appears quickly
    _cache["questions"] = None
    _cache["questions_ts"] = 0


def get_questions():
    # Use independent questions cache
    if _cache["questions"] and time.time() - _cache["questions_ts"] < CACHE_TTL:
        return _cache["questions"]

    data = get_work_sheet(questions_key).get_all_values()

    # remove fully-empty rows (often Google returns extra empty trailing rows)
    filtered = [row for row in data if any(cell is not None and str(cell).strip() != "" for cell in row)]

    # ensure every row has at least 4 columns (category, name, question, time)
    normalized = []
    for row in filtered:
        row = list(row)  # in case it's tuple
        while len(row) < 4:
            row.append("")
        normalized.append(row)

    _cache["questions"] = normalized
    _cache["questions_ts"] = time.time()
    return normalized


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
