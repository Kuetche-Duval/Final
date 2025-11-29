# app.py  -- final upgraded version (drop-in)
import os
import base64
import json
import random
import logging
import traceback
import re
import io
import binascii
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from flask import (
    Flask, render_template, url_for, redirect, send_file,
    request, flash, Response, jsonify, send_from_directory, make_response
)
from dotenv import load_dotenv

# optional requests for fetching Drive files server-side
try:
    import requests
except Exception:
    requests = None

# optional pywebpush for sending web push
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# --- Load environment ---
load_dotenv()

# --- Service account handling (optionally write base64 into file) ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64.strip())
        json.loads(data.decode("utf-8"))  # validate JSON
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode service account: {ex}")
else:
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        logging.getLogger(__name__).warning(
            "Service account file not found at %s — Sheets/Drive will likely fail.", SERVICE_ACCOUNT_PATH
        )

# --- Flask setup ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
logging.basicConfig(level=logging.INFO)

FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"

STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"
]

MOTIVATION_VERSES = [
    ("A new commandment I give to you: Love one another; as I have loved you, so you must love one another.", "John 13:34"),
    ("I can do all things through Christ who strengthens me.", "Philippians 4:13"),
    ("Trust in the Lord with all your heart and lean not on your own understanding.", "Proverbs 3:5"),
]

# community links (json string in env: {"Mathematics":"https://...", ...})
try:
    COMMUNITY_LINKS = json.loads(os.environ.get("COMMUNITY_LINKS_JSON", "{}") or "{}")
except Exception:
    COMMUNITY_LINKS = {}

# admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", os.environ.get("ADMIN_CLEAR_SECRET", ""))
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# coding config
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "").strip()

# VAPID keys for web push
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# SQL table row limit for server rendering
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))

# cookie lifetime for remembering user & endpoint (1 year)
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# --- simple VAPID public key validator (warn early) ---
def _is_likely_valid_vapid_public_key(s):
    if not s:
        return False
    key = s.strip()
    key = key.replace('-', '+').replace('_', '/')
    padding = '=' * ((4 - (len(key) % 4)) % 4)
    try:
        b = base64.b64decode(key + padding)
    except (binascii.Error, Exception) as e:
        app.logger.warning("VAPID_PUBLIC_KEY appears not to be valid base64: %s", e)
        return False
    if len(b) in (65, 64):
        return True
    app.logger.warning("VAPID_PUBLIC_KEY decoded length is %d bytes (expected ~65). It might still work but double-check.", len(b))
    return True

if VAPID_PUBLIC_KEY:
    ok = _is_likely_valid_vapid_public_key(VAPID_PUBLIC_KEY)
    if not ok:
        app.logger.warning("VAPID_PUBLIC_KEY looks invalid; push subscriptions will likely fail. Generate a proper VAPID keypair.")

# --- Sheets/Drive helpers (import if available) ---
try:
    from sheets import (
        from_google_sheets, check_date, append_row, read_rules, get_questions,
        clear_sheet, delete_rows_by_indices, delete_rows_by_subject,
        save_subscription, list_subscriptions, delete_subscriptions_by_endpoint
    )
    SHEETS_AVAILABLE = True
except Exception as e:
    app.logger.warning("sheets.py import failed: %s — using fallbacks", e)
    SHEETS_AVAILABLE = False

    def from_google_sheets(*a, **k): return []
    def check_date(*a, **k): return ""
    def append_row(row, key=None):
        app.logger.warning("append_row fallback - row: %s", row)
    def read_rules(*a, **k): return []
    def get_questions(*a, **k): return []
    def clear_sheet(*a, **k): app.logger.warning("clear_sheet fallback")
    def delete_rows_by_indices(*a, **k): app.logger.warning("delete_rows_by_indices fallback")
    def delete_rows_by_subject(*a, **k): app.logger.warning("delete_rows_by_subject fallback")

    # --- BEGIN FALLBACK: sqlite-backed subscriptions & notifications ---
    NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
    DB_PATH = Path(NN_LOCAL_DB)

    def _init_local_db():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    subject TEXT,
                    client_id TEXT,
                    p256dh TEXT,
                    auth TEXT,
                    raw_json TEXT,
                    created_at TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT,
                    subject TEXT,
                    title TEXT,
                    body TEXT,
                    data TEXT,
                    created_at TEXT,
                    delivered INTEGER DEFAULT 0,
                    delivered_at TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.exception("Failed initializing local subscription DB: %s", e)

    _init_local_db()

    def save_subscription(subject, client_id, subscription):
        try:
            if isinstance(subscription, str):
                try:
                    subscription = json.loads(subscription)
                except Exception:
                    app.logger.debug("save_subscription: received non-json string")
                    return False
            if not isinstance(subscription, dict):
                app.logger.debug("save_subscription: invalid subscription type")
                return False
            endpoint = subscription.get('endpoint')
            if not endpoint:
                app.logger.debug("save_subscription: subscription has no endpoint")
                return False
            keys = subscription.get('keys') or {}
            p256dh = keys.get('p256dh') or ''
            auth = keys.get('auth') or ''
            subject_norm = (subject or 'Other').strip()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            raw = json.dumps(subscription)
            c.execute("""REPLACE INTO subscriptions(endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES(?,?,?,?,?,?,?)""", (endpoint, subject_norm, client_id or '', p256dh, auth, raw, now))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            app.logger.exception("save_subscription failed: %s", e)
            return False

    def list_subscriptions(subject=None):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            if subject:
                subj = subject.strip().lower()
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions WHERE lower(subject)=?", (subj,))
            else:
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions")
            rows = c.fetchall()
            conn.close()
            out = []
            for r in rows:
                subj, client_id, raw_json, created_at = r
                out.append(["__subscription__", subj, client_id or "", raw_json or "{}", created_at or ""])
            return out
        except Exception as e:
            app.logger.exception("list_subscriptions failed: %s", e)
            return []

    def delete_subscriptions_by_endpoint(endpoint):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.exception("delete_subscriptions_by_endpoint failed: %s", e)

    def get_subjects_for_endpoint(endpoint):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("SELECT subject FROM subscriptions WHERE endpoint=?", (endpoint,))
            rows = c.fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception as e:
            app.logger.exception("get_subjects_for_endpoint failed: %s", e)
            return []

    def save_notification_for_endpoint(endpoint, subject, title, body, data_obj):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            c.execute("""INSERT INTO notifications(endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""", (endpoint, subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
            conn.close()
            return nid
        except Exception as e:
            app.logger.exception("save_notification_for_endpoint failed: %s", e)
            return None

    def fetch_pending_notifications_for_endpoint(endpoint):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint,))
            rows = c.fetchall()
            conn.close()
            out = []
            for r in rows:
                nid, subj, title, body, data_text, created_at = r
                try:
                    data_obj = json.loads(data_text) if data_text else {}
                except Exception:
                    data_obj = {}
                out.append({"id": nid, "subject": subj, "title": title, "body": body, "data": data_obj, "created_at": created_at})
            return out
        except Exception as e:
            app.logger.exception("fetch_pending_notifications_for_endpoint failed: %s", e)
            return []

    def ack_notifications_by_ids(endpoint, ids):
        if not ids:
            return 0
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint))
                except Exception:
                    continue
            conn.commit()
            changed = len(ids)
            conn.close()
            return changed
        except Exception as e:
            app.logger.exception("ack_notifications_by_ids failed: %s", e)
            return 0

    def mark_notifications_delivered_for_endpoint(endpoint):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint))
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.exception("mark_notifications_delivered_for_endpoint failed: %s", e)
    # --- END FALLBACK block ---

try:
    from drive import from_google_drive, folders_and_images, download_file, update_file, create_file_in_folder
    DRIVE_AVAILABLE = True
except Exception:
    app.logger.warning("drive.py import failed — using fallbacks")
    DRIVE_AVAILABLE = False
    def from_google_drive(*a, **k): return []
    def folders_and_images(*a, **k): return []
    def download_file(*a, **k): return b""
    def update_file(*a, **k): return False
    def create_file_in_folder(*a, **k): return None

# lock for writing exercises locally
_coding_lock = Lock()

# Inject common context into templates
@app.context_processor
def inject_globals():
    return {
        "community_links": COMMUNITY_LINKS,
        "message": FOOTER_MESSAGE,
        "vapid_public_key": VAPID_PUBLIC_KEY or ""
    }

# -------------------------
# Jinja filter: format_time
# -------------------------
def _try_parse_time(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    text = str(s).strip()
    fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%H:%M:%S", "%H:%M", "%I:%M %p"]
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)(:([0-5]\d))?", text)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(4) or 0)
        return datetime(2000, 1, 1, hh, mm, ss)
    return None

def format_time(value):
    if not value:
        return ""
    dt = _try_parse_time(value)
    if dt:
        return dt.strftime("%b %d %I:%M %p").lstrip("0")
    return str(value)

app.jinja_env.filters["format_time"] = format_time

# -------------------------
# Helper: timetable for today (improved, deterministic column detection)
# -------------------------
def get_timetable_today():
    try:
        rows = from_google_sheets() or []
        if not rows or len(rows) < 2:
            return []

        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]

        aliases = {
            "day": ["day", "weekday"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor", "staff"],
            "link": ["link", "location", "link/location", "meeting", "url", "meet"],
            "notes": ["notes", "note", "remarks", "comments"]
        }

        def find_col_for_alias_list(alias_list):
            for a in alias_list:
                a_norm = a.lower().strip()
                for i, h in enumerate(header_lower):
                    if h == a_norm:
                        return i
            for a in alias_list:
                a_norm = a.lower().strip()
                for i, h in enumerate(header_lower):
                    if a_norm in h:
                        return i
            for a in alias_list:
                toks = [t for t in a.lower().split() if t]
                if not toks:
                    continue
                for i, h in enumerate(header_lower):
                    if all(tok in h for tok in toks):
                        return i
            return None

        col_day = find_col_for_alias_list(aliases["day"])
        col_time = find_col_for_alias_list(aliases["time"])
        col_subject = find_col_for_alias_list(aliases["subject"])
        col_facilitator = find_col_for_alias_list(aliases["facilitator"])
        col_link = find_col_for_alias_list(aliases["link"])
        col_notes = find_col_for_alias_list(aliases["notes"])

        hl = len(header)
        if hl >= 6:
            if col_day is None: col_day = 0
            if col_time is None: col_time = 1
            if col_subject is None: col_subject = 2
            if col_facilitator is None: col_facilitator = 3
            if col_link is None: col_link = 4
            if col_notes is None: col_notes = 5
        else:
            def fallback_assign(c_idx, preferred_idx):
                if c_idx is None and preferred_idx < hl:
                    return preferred_idx
                return c_idx
            col_day = fallback_assign(col_day, 0)
            col_time = fallback_assign(col_time, 1 if hl > 1 else 0)
            col_subject = fallback_assign(col_subject, 2 if hl > 2 else (hl-1))
            col_facilitator = fallback_assign(col_facilitator, 3 if hl > 3 else (hl-1))
            col_link = fallback_assign(col_link, 4 if hl > 4 else (hl-1))
            col_notes = fallback_assign(col_notes, 5 if hl > 5 else (hl-1))

        try:
            app.logger.info("Timetable columns detected: day=%s time=%s subject=%s facilitator=%s link=%s notes=%s (header=%s)",
                            col_day, col_time, col_subject, col_facilitator, col_link, col_notes, header)
        except Exception:
            pass

        today_short = datetime.now().strftime("%a").upper()
        today_full = datetime.now().strftime("%A").upper()

        matches = []
        for idx, r in enumerate(rows[1:], start=2):
            try:
                rlist = list(r)
                day_cell = ""
                if col_day is not None and col_day < len(rlist):
                    day_cell = str(rlist[col_day]).strip()
                elif len(rlist) > 0:
                    day_cell = str(rlist[0]).strip()
                if not day_cell:
                    continue
                first = day_cell.upper()
                if first.startswith(today_short) or first.startswith(today_full[:3]) or today_full in first:
                    def get_cell(ci):
                        if ci is None or ci >= len(rlist):
                            return ""
                        return rlist[ci] or ""
                    entry = {
                        "row_index": idx,
                        "day": str(get_cell(col_day)).strip(),
                        "time": str(get_cell(col_time)).strip(),
                        "subject": str(get_cell(col_subject)).strip(),
                        "facilitator": str(get_cell(col_facilitator)).strip(),
                        "link": str(get_cell(col_link)).strip(),
                        "notes": str(get_cell(col_notes)).strip()
                    }
                    matches.append(entry)
            except Exception:
                continue

        return matches
    except Exception:
        app.logger.exception("get_timetable_today failed")
        return []

# -------------------------
# Coding exercises loader
# -------------------------
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    drive_id = (drive_id or CODING_DRIVE_FILE_ID or "").strip() or None
    local_path = (local_path or CODING_LOCAL_PATH or "").strip() or None

    if drive_id and DRIVE_AVAILABLE:
        try:
            raw = download_file(drive_id)
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, list):
                        return data
                except Exception:
                    app.logger.exce