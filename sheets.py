# sheets.py (updated, uses QUESTIONS_SUBSCRIPTION_KEY for dedicated subscription sheet)
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
# Use the correctly spelled environment variable
subscription_key = os.environ.get("QUESTIONS_SUBSCRIPTION_KEY")

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
    """
    Return list of tuples (sheet_row_index, row_values) for rows whose Day column
    matches today's weekday. The returned sheet_row_index is 1-based (as in Google Sheets).
    This makes templates simpler and avoids index arithmetic errors.
    """
    try:
        # read full sheet so we can return row contents
        ws = get_work_sheet(timetable_key)
        all_rows = _retry(ws.get_all_values)

        if not all_rows:
            return []

        # normalize today's weekday (e.g. 'Mon', 'MONDAY', etc.)
        today_short = datetime.now().strftime("%a").upper()   # 'MON'
        today_full = datetime.now().strftime("%A").upper()    # 'MONDAY'

        matches = []
        # iterate rows with 1-based index
        for idx, row in enumerate(all_rows, start=1):
            if not row or len(row) == 0:
                continue
            # look at first column (Day)
            first = str(row[0]).strip().upper()
            if not first:
                continue
            # match common variations
            if (first.startswith(today_short) or
                first.startswith(today_full[:3]) or
                today_full in first):
                matches.append((idx, row))
        return matches
    except Exception:
        logger.exception("check_date failed")
        return []

def append_row(row, key=questions_key):
    if not isinstance(row, (list, tuple)):
        raise ValueError("append_row expects a list/tuple: [category, username, question, attempt, timestamp]")
    row = list(row)
    while len(row) < 5:
        row.append("")
    category = str(row[0]).strip() if row[0] is not None else ""
    username = str(row[1]).strip() if row[1] else "Anonymous"
    question_text = row[2] or ""
    attempt_text = row[3] or ""
    timestamp = row[4] or datetime.utcnow().replace(microsecond=0).isoformat()
    full_row = [category, username, question_text, attempt_text, timestamp]

    if not key:
        logger.warning("Sheet key not set; append_row will skip writing")
        return

    ws = get_work_sheet(key)
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

def delete_rows_by_indices(indices):
    if not questions_key:
        raise RuntimeError("QUESTIONS_SHEET_KEY not configured")
    if not indices:
        return
    ws = get_work_sheet(questions_key)
    idxs = sorted({int(i) for i in indices if isinstance(i, (int, str)) and str(i).strip().isdigit() and int(i) >= 1}, reverse=True)
    for i in idxs:
        try:
            _retry(lambda row=i: ws.delete_rows(row), attempts=3)
            logger.info("Deleted sheet row %s", i)
        except Exception:
            logger.exception("Failed to delete row %s", i)
    with _cache_lock:
        _cache["questions"] = None
        _cache["questions_ts"] = 0

def delete_rows_by_subject(subject):
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

# --- Subscription helpers ---
def _parse_subscription_row(row):
    try:
        if not row or len(row) < 4:
            return None
        if str(row[0]).strip() != "__subscription__":
            return None
        subj = str(row[1]).strip() if len(row) > 1 else "Other"
        client_id = row[2] if len(row) > 2 else ""
        try:
            sub = json.loads(row[3]) if row[3] else None
        except Exception:
            sub = None
        if not sub:
            return None
        return (subj, client_id, sub)
    except Exception:
        return None

def save_subscription(subject, client_id, subscription_obj):
    """
    Save a push subscription. Prefer storing in a dedicated subscription sheet (QUESTIONS_SUBSCRIPTION_KEY).
    Deduplicate by endpoint+subject so same device can subscribe multiple subjects (one row per subject).
    """
    try:
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        subject = str(subject or "Other").strip()
        endpoint = subscription_obj.get("endpoint") if isinstance(subscription_obj, dict) else None

        # prefer subscription_key sheet
        if subscription_key:
            try:
                ws = get_work_sheet(subscription_key)
                rows = _retry(ws.get_all_values)
                endpoints_for_subject = set()
                for r in rows:
                    parsed = _parse_subscription_row(r)
                    if parsed and parsed[2].get("endpoint"):
                        subj, _, sub = parsed[0], parsed[1], parsed[2]  # parsed returns (subj, client_id, sub)
                # dedupe: check if endpoint+subject already present
                for r in rows:
                    parsed = _parse_subscription_row(r)
                    if not parsed:
                        continue
                    subj, _, sub = parsed
                    if sub and sub.get("endpoint") == endpoint and subj == subject:
                        logger.info("Subscription endpoint already exists in subscription sheet for this subject; skipping append")
                        return
                # append new subscription row
                _retry(lambda: ws.append_row(["__subscription__", subject or "Other", client_id or "", json.dumps(subscription_obj), timestamp], value_input_option="USER_ENTERED"), attempts=3)
                with _cache_lock:
                    _cache["questions"] = None
                    _cache["questions_ts"] = 0
                return
            except Exception:
                logger.exception("Failed to write to subscription sheet; falling back to questions sheet")

        # fallback: dedupe in questions sheet
        try:
            rows = get_questions() or []
            for r in rows:
                parsed = _parse_subscription_row(r)
                if not parsed:
                    continue
                subj, _, sub = parsed
                if sub and sub.get("endpoint") == endpoint and subj == subject:
                    logger.info("Subscription endpoint already exists in questions sheet for this subject; skipping append")
                    return
        except Exception:
            logger.exception("Could not scan questions sheet for dedupe; proceeding to append")

        append_row(["__subscription__", subject or "Other", client_id or "", json.dumps(subscription_obj), timestamp])
    except Exception:
        logger.exception("save_subscription failed")
        raise

def list_subscriptions(subject=None):
    out = []
    # dedicated subscription sheet first
    try:
        if subscription_key:
            try:
                ws = get_work_sheet(subscription_key)
                rows = _retry(ws.get_all_values)
                for r in rows:
                    parsed = _parse_subscription_row(r)
                    if not parsed:
                        continue
                    subj, client_id, sub = parsed
                    if subject:
                        if subj == subject:
                            out.append({"subject": subj, "client_id": client_id, "subscription": sub})
                    else:
                        out.append({"subject": subj, "client_id": client_id, "subscription": sub})
                return out
            except Exception:
                logger.exception("Failed reading subscription sheet; falling back to scanning questions sheet")
    except Exception:
        logger.exception("list_subscriptions top-level error")

    # fallback: scan questions sheet
    rows = get_questions() or []
    for row in rows:
        try:
            parsed = _parse_subscription_row(row)
            if not parsed:
                continue
            subj, client_id, sub = parsed
            if subject:
                if subj == subject:
                    out.append({"subject": subj, "client_id": client_id, "subscription": sub})
            else:
                out.append({"subject": subj, "client_id": client_id, "subscription": sub})
        except Exception:
            continue
    return out

def delete_subscriptions_by_endpoint(endpoint):
    if not endpoint:
        return
    # attempt dedicated subscription sheet first
    try:
        if subscription_key:
            ws = get_work_sheet(subscription_key)
            rows = _retry(ws.get_all_values)
            indices = []
            for i, r in enumerate(rows, start=1):
                parsed = _parse_subscription_row(r)
                if not parsed:
                    continue
                _, _, sub = parsed
                if sub and sub.get("endpoint") == endpoint:
                    indices.append(i)
            if indices:
                idxs = sorted(set(indices), reverse=True)
                for rownum in idxs:
                    try:
                        _retry(lambda rn=rownum: ws.delete_rows(rn), attempts=3)
                        logger.info("Deleted subscription row %s from subscription sheet", rownum)
                    except Exception:
                        logger.exception("Failed to delete subscription row %s from subscription sheet", rownum)
                with _cache_lock:
                    _cache["questions"] = None
                    _cache["questions_ts"] = 0
                return
    except Exception:
        logger.exception("Failed deleting from dedicated subscription sheet (will try fallback)")

    # fallback: delete from questions sheet
    try:
        rows = get_questions() or []
        indices = []
        for i, r in enumerate(rows, start=1):
            parsed = _parse_subscription_row(r)
            if not parsed:
                continue
            _, _, sub = parsed
            if sub and sub.get("endpoint") == endpoint:
                indices.append(i)
        if indices:
            delete_rows_by_indices(indices)
    except Exception:
        logger.exception("Failed deleting subscription rows from fallback questions sheet")
