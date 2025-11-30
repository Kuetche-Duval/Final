# PART 1 of 5
# app.py — part 1/5
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
import time
from urllib.parse import urlparse
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

    def normalize_endpoint(endpoint):
        """Normalize endpoint string for matching / deduplication."""
        if not endpoint:
            return ""
        try:
            u = urlparse(endpoint)
            # strip query params and fragments — keep path + host
            scheme = u.scheme or "https"
            netloc = u.netloc or u.path.split("/")[0]
            path = u.path or ""
            normalized = f"{scheme}://{netloc}{path}".rstrip("/")
            return normalized
        except Exception:
            # fallback: remove trailing slashes and whitespace
            return endpoint.strip().rstrip("/")
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
# -------------------------
# -------------------------
# PART 2 + PART 3 (REGENERATED)
# Subscriptions / notifications persistence helpers (sqlite fallback),
# plus coding loader, drive-image route, and improved push helpers.
# Paste this to replace the corresponding sections of app.py
# -------------------------

# safe imports / fallbacks used in this block
import os
import sqlite3
import json
import re
import time
import io
from pathlib import Path
from urllib.parse import urlparse

# Ensure normalize_endpoint exists (define fallback if missing)
if "normalize_endpoint" not in globals():
    def normalize_endpoint(endpoint: str) -> str:
        if not endpoint:
            return ""
        try:
            p = urlparse(endpoint)
            base = f"{p.scheme}://{p.netloc}{p.path}"
            return base.rstrip('/')
        except Exception:
            return endpoint

# Ensure DB_PATH exists (fallback to ./app.db)
if "DB_PATH" not in globals():
    # prefer environment override if present
    DB_PATH = Path(os.environ.get("DB_PATH", "app.db"))

# -------------------------
# Subscriptions / notifications persistence helpers (sqlite fallback)
# -------------------------
def delete_subscriptions_by_endpoint(endpoint):
    """
    Delete subscription rows from sqlite by endpoint or normalized endpoint.
    Attempts to use endpoint_normalized column if present, otherwise does best-effort match.
    """
    if not endpoint:
        return
    try:
        norm = normalize_endpoint(endpoint) or ""
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            try:
                # Preferred: match exact endpoint or endpoint_normalized if present
                c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint_normalized=?", (endpoint, norm))
            except sqlite3.OperationalError:
                # Fallbacks if schema doesn't include endpoint_normalized
                try:
                    c.execute("DELETE FROM subscriptions WHERE endpoint=? OR replace(endpoint,'/','')=?", (endpoint, norm.replace("/", "")))
                except Exception:
                    c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
            conn.commit()
        try:
            app.logger.info("delete_subscriptions_by_endpoint removed rows for endpoint=%s normalized=%s", endpoint, norm)
        except Exception:
            pass
    except Exception:
        try:
            app.logger.exception("delete_subscriptions_by_endpoint failed for %s", endpoint)
        except Exception:
            pass


def get_subjects_for_endpoint(endpoint):
    """
    Return list of subjects that a given endpoint is subscribed to (sqlite fallback).
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT subject FROM subscriptions WHERE endpoint=?", (endpoint,))
            rows = c.fetchall()
        return [r[0] for r in rows]
    except Exception:
        try:
            app.logger.exception("get_subjects_for_endpoint failed for %s", endpoint)
        except Exception:
            pass
        return []


def save_notification_for_endpoint(endpoint, subject, title, body, data_obj):
    """
    Persist a notification for later client retrieval. Returns inserted id or None.
    """
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications(endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""", (endpoint or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        return nid
    except Exception:
        try:
            app.logger.exception("save_notification_for_endpoint failed for %s", endpoint)
        except Exception:
            pass
        return None


def fetch_pending_notifications_for_endpoint(endpoint):
    """
    Fetch undelivered notifications for an endpoint (ordered oldest first).
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint,))
            rows = c.fetchall()
        out = []
        for r in rows:
            nid, subj, title, body, data_text, created_at = r
            try:
                data_obj = json.loads(data_text) if data_text else {}
            except Exception:
                data_obj = {}
            out.append({"id": nid, "subject": subj, "title": title, "body": body, "data": data_obj, "created_at": created_at})
        return out
    except Exception:
        try:
            app.logger.exception("fetch_pending_notifications_for_endpoint failed for %s", endpoint)
        except Exception:
            pass
        return []


def ack_notifications_by_ids(endpoint, ids):
    """
    Mark specific notification ids for endpoint as delivered.
    Returns number of rows marked.
    """
    if not ids:
        return 0
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        changed = 0
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint))
                    if c.rowcount:
                        changed += 1
                except Exception:
                    continue
            conn.commit()
        return changed
    except Exception:
        try:
            app.logger.exception("ack_notifications_by_ids failed for %s ids=%s", endpoint, ids)
        except Exception:
            pass
        return 0


def mark_notifications_delivered_for_endpoint(endpoint):
    """
    Best-effort: mark any undelivered notifications for endpoint as delivered.
    """
    if not endpoint:
        return
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        norm = normalize_endpoint(endpoint) or ""
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            try:
                # if you added endpoint_normalized earlier, this will catch those rows too
                c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE (endpoint=? OR endpoint=?) AND delivered=0", (now, endpoint, norm))
            except sqlite3.OperationalError:
                # fallback to endpoint only
                c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint))
            conn.commit()
        try:
            app.logger.debug("mark_notifications_delivered_for_endpoint marked pending notifications for %s", endpoint)
        except Exception:
            pass
    except Exception:
        try:
            app.logger.exception("mark_notifications_delivered_for_endpoint failed for %s", endpoint)
        except Exception:
            pass


# --- END FALLBACK block (sqlite helpers) ---

# Drive helper import fallback (preserve earlier behavior)
try:
    from drive import from_google_drive, folders_and_images, download_file, update_file, create_file_in_folder
    DRIVE_AVAILABLE = True
except Exception:
    try:
        app.logger.warning("drive.py import failed — using fallbacks")
    except Exception:
        pass
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
        try:
            app.logger.exception("get_timetable_today failed")
        except Exception:
            pass
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
                    app.logger.exception("Failed to parse coding exercises JSON from Drive file %s", drive_id)
        except Exception:
            app.logger.exception("Failed to download coding exercises from Drive id %s", drive_id)

    if local_path:
        try:
            p = Path(local_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
        except Exception:
            app.logger.exception("Failed to read local coding exercises file: %s", local_path)

    app.logger.info("Using fallback coding exercises")
    return [
        {
            "id": "fizzbuzz",
            "title": "FizzBuzz",
            "language": "js",
            "difficulty": "Easy",
            "description": "Print numbers 1..N. For multiples of 3 print 'Fizz', for 5 print 'Buzz', for both print 'FizzBuzz'.",
            "starter_js": "for (let i=1;i<=100;i++){let out=''; if(i%3===0) out+='Fizz'; if(i%5===0) out+='Buzz'; console.log(out||i);} ",
            "solution_js": "for (let i=1;i<=100;i++){let out=''; if(i%3===0) out+='Fizz'; if(i%5===0) out+='Buzz'; console.log(out||i);}"
        }
    ]

# -------------------------
# Drive image route (robust)
# -------------------------
@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    try:
        fid = file_id
        if fid.startswith("http"):
            m = re.search(r"(?:/d/|id=)([-_0-9A-Za-z]+)", fid)
            if m:
                fid = m.group(1)
            else:
                fid = fid.rstrip("/").split("/")[-1]
        drive_url = f"https://drive.google.com/uc?export=download&id={fid}"
        if requests:
            try:
                r = requests.get(drive_url, stream=True, timeout=10)
                content_type = r.headers.get("content-type", "")
                if r.status_code == 200 and content_type and content_type.startswith("image/"):
                    return Response(r.content, mimetype=content_type)
                app.logger.warning("Drive image fetch failed (status %s, content-type %s) for %s", r.status_code, content_type, fid)
            except Exception:
                app.logger.exception("requests failed when fetching drive image %s - falling back", fid)
        return redirect(drive_url)
    except Exception as e:
        app.logger.exception("drive-image fallback serving local placeholder for %s: %s", file_id, e)
        try:
            return send_file("static/img/fallback.jpg", mimetype="image/jpeg")
        except Exception:
            small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                                   b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                                   b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!"
                                   b"\xbc\x33\x00\x00\x00\x00IEND\xaeB`\x82")
            small_png.seek(0)
            return send_file(small_png, mimetype="image/png")


# -------------------------
# Subscriptions helpers (improved)
# -------------------------
def _is_valid_subscription_obj(sub):
    if not sub or not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint") or sub.get("url")
    if not endpoint or not isinstance(endpoint, str):
        return False
    # keys optional but if present must be dict
    if sub.get("keys") and not isinstance(sub.get("keys"), dict):
        return False
    return True


def _normalize_subject(s):
    return (s or "Other").strip()


def _save_subscription_to_sheet(subject, client_id, subscription_obj):
    """
    Save a subscription into Sheets (preferred) or fall back to app's storage via append_row/save_subscription.
    Deduplicates by endpoint+subject where possible.
    """
    try:
        if not _is_valid_subscription_obj(subscription_obj):
            app.logger.warning("Attempted to save invalid subscription object: %r", subscription_obj)
            return False

        subject = _normalize_subject(subject)
        endpoint = subscription_obj.get("endpoint")

        if SHEETS_AVAILABLE:
            try:
                save_subscription(subject, client_id or "", subscription_obj)
                return True
            except Exception as e:
                app.logger.debug("sheets.save_subscription failed (will fallback): %s", e)

        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        try:
            rows = []
            try:
                rows = get_questions() or []
            except Exception:
                app.logger.debug("get_questions not available for dedupe fallback")

            found = False
            for r in rows:
                try:
                    if len(r) >= 1 and str(r[0]) == "__subscription__":
                        subj = str(r[1]) if len(r) > 1 else "Other"
                        sub_json = r[3] if len(r) > 3 else None
                        if not sub_json:
                            continue
                        try:
                            parsed = json.loads(sub_json)
                        except Exception:
                            parsed = None
                        if parsed and parsed.get("endpoint") == endpoint and _normalize_subject(subj) == subject:
                            found = True
                            break
                except Exception:
                    continue

            if not found:
                safe_sub = {"endpoint": endpoint, "keys": subscription_obj.get("keys", {})}
                append_row(["__subscription__", subject, client_id or "", json.dumps(safe_sub), timestamp])

            # fallback sqlite save as well (to ensure persistence)
            try:
                if not SHEETS_AVAILABLE:
                    save_subscription(subject, client_id or "", subscription_obj)
            except Exception:
                app.logger.debug("fallback sqlite save_subscription failed but proceeding")
            return True
        except Exception:
            app.logger.exception("Fallback append for subscription failed")
            return False
    except Exception:
        app.logger.exception("Unexpected error in _save_subscription_to_sheet")
        return False


def _load_subscriptions_for_subject(subject):
    """
    Load subscriptions for a subject. Try Sheets first, then fallback to app questions storage.
    Returns list of subscription dicts.
    """
    subs = []
    try:
        subject_norm = _normalize_subject(subject)
        if SHEETS_AVAILABLE:
            try:
                raw = list_subscriptions(subject_norm) or []
                for r in raw:
                    if isinstance(r, dict):
                        sub = r.get("subscription") or r.get("sub") or r.get("push") or r
                        if isinstance(sub, dict) and _is_valid_subscription_obj(sub):
                            if not subject or _normalize_subject(r.get("subject") or "") == subject_norm:
                                subs.append(sub)
                    elif isinstance(r, (list, tuple)) and len(r) > 0:
                        try:
                            if r[0] == "__subscription__":
                                sub_json = r[3] if len(r) > 3 else None
                                if sub_json:
                                    parsed = json.loads(sub_json)
                                    if _is_valid_subscription_obj(parsed):
                                        if _normalize_subject(r[1]) == subject_norm:
                                            subs.append(parsed)
                        except Exception:
                            continue
                if subs:
                    return subs
            except Exception as e:
                app.logger.debug("sheets.list_subscriptions failed: %s", e)

        rows = get_questions() or []
        for row in rows:
            try:
                if len(row) >= 1 and str(row[0]) == "__subscription__":
                    subj = _normalize_subject(row[1] if len(row) > 1 else "Other")
                    sub_json = row[3] if len(row) > 3 else None
                    if not sub_json:
                        continue
                    parsed = None
                    try:
                        parsed = json.loads(sub_json)
                    except Exception:
                        continue
                    if subj == subject_norm and _is_valid_subscription_obj(parsed):
                        subs.append(parsed)
            except Exception:
                continue
    except Exception:
        app.logger.exception("Failed loading subscriptions fallback")
    return subs


# improved send_push_to_subject (robust, deletes stale endpoints, persists notifications)
def send_push_to_subject(subject, question_text, attempt_text):
    """
    Send push notifications to all subscriptions for subject.
    Returns dict with summary and per-endpoint details.
    """
    try:
        subs = _load_subscriptions_for_subject(subject)
    except Exception:
        app.logger.exception("Failed to load subscriptions for %s", subject)
        subs = []

    if not subs:
        app.logger.debug("No subscribers for subject %s", subject)
        return {"sent": 0, "errors": ["no subscribers"], "detail": []}

    title = f"New question in {subject}"
    q_preview = (question_text or "").strip()
    a_preview = (attempt_text or "").strip()
    if len(q_preview) > 140: q_preview = q_preview[:137] + "…"
    if len(a_preview) > 180: a_preview = a_preview[:177] + "…"
    body = f"Q: {q_preview}\nA: {a_preview}"

    payload_template = {
        "title": title,
        "body": body,
        "url": "/questions",
        "tag": f"subject-{re.sub(r'\\s+','-', subject).lower()}",
        "renotify": True,
        "subject": subject,
        "preview": {"question": q_preview, "attempt": a_preview}
    }

    results = []
    sent = 0

    # If VAPID/pywebpush missing, persist for client pull and return
    if not PYWEBPUSH_AVAILABLE or not VAPID_PRIVATE_KEY:
        app.logger.warning("pywebpush/VAPID not available — persisting notifications for offline retrieval")
        for sub in subs:
            try:
                endpoint = sub.get("endpoint") if isinstance(sub, dict) else None
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
                results.append({"endpoint": endpoint, "ok": False, "error": "pywebpush-not-configured"})
            except Exception:
                results.append({"endpoint": None, "ok": False, "error": "persist-failed"})
        return {"sent": 0, "errors": ["pywebpush/VAPID not available"], "detail": results}

    # Send to each subscription with small retry & smart trimming
    for raw_sub in subs:
        endpoint = None
        try:
            sub = raw_sub
            if isinstance(raw_sub, str):
                try:
                    sub = json.loads(raw_sub)
                except Exception:
                    sub = {"endpoint": raw_sub}

            if not _is_valid_subscription_obj(sub):
                results.append({"endpoint": None, "ok": False, "error": "invalid-subscription-object"})
                continue

            endpoint = sub.get("endpoint")
            if not endpoint:
                results.append({"endpoint": None, "ok": False, "error": "no-endpoint"})
                continue

            # compute normalized once
            try:
                norm = normalize_endpoint(endpoint)
            except Exception:
                norm = endpoint

            # persist first so client can fetch if push fails
            try:
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
            except Exception:
                app.logger.debug("Failed to persist notification for %s (continuing send)", endpoint)

            # prepare payload and provider-specific trimming
            payload = dict(payload_template)
            parsed = urlparse(endpoint)
            domain = parsed.netloc.lower() if parsed.netloc else ""

            # Conservative payload for Windows Notification Service endpoints
            if "notify.windows.com" in domain or "wns" in domain or "windows" in domain:
                payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}

            # Ensure payload not too large
            data_bytes = json.dumps(payload).encode('utf-8')
            if len(data_bytes) > 2000:
                payload["preview"] = None
                payload["body"] = (payload.get("body","")[:200] + "…") if payload.get("body") else ""
                data_bytes = json.dumps(payload).encode('utf-8')

            attempt = 0
            while attempt < 2:
                attempt += 1
                try:
                    webpush(
                        subscription_info=sub,
                        data=json.dumps(payload),
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": VAPID_SUBJECT}
                    )
                    sent += 1
                    results.append({"endpoint": endpoint, "ok": True})
                    try:
                        mark_notifications_delivered_for_endpoint(endpoint)
                    except Exception:
                        pass
                    break
                except WebPushException as ex:
                    resp = getattr(ex, "response", None)
                    status = getattr(resp, "status_code", None) if resp else None
                    body_text = None
                    try:
                        if resp and hasattr(resp, "content"):
                            body_text = resp.content.decode("utf-8", errors="ignore")
                    except Exception:
                        body_text = None
                    err_text = str(ex)
                    app.logger.warning("WebPushException endpoint=%s status=%s err=%s body=%s", endpoint, status, err_text, body_text)

                    # stale or gone -> delete subscription
                    if status in (404, 410) or (body_text and "unsubscribed" in body_text.lower()) or ("unsubscribed" in err_text.lower()):
                        try:
                            delete_subscriptions_by_endpoint(endpoint)
                            app.logger.info("Deleted stale subscription endpoint %s (normalized=%s)", endpoint, norm)
                        except Exception:
                            app.logger.exception("Failed deleting stale subscription %s", endpoint)
                        results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text, "body": body_text, "deleted": True})
                        break

                    # Bad Request on WNS / payload issues: try once with minimal payload
                    if status == 400 or ("400 Bad Request" in err_text):
                        if attempt < 2:
                            time.sleep(0.12)
                            payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}
                            continue
                        else:
                            results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text, "body": body_text})
                            break

                    # transient: retry once
                    if attempt < 2:
                        time.sleep(0.12)
                        continue
                    else:
                        results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text, "body": body_text})
                        break

                except Exception as ex:
                    app.logger.exception("Unexpected exception sending push to %s", endpoint)
                    results.append({"endpoint": endpoint, "ok": False, "error": str(ex)})
                    break

        except Exception:
            app.logger.exception("Error handling subscription for endpoint %s", endpoint)
            results.append({"endpoint": endpoint, "ok": False, "error": "processing-exception"})

    app.logger.info("send_push_to_subject result: sent=%s errors=%s", sent, len([r for r in results if not r.get("ok")]))
    return {"sent": sent, "errors": [r for r in results if not r.get("ok")], "detail": results}


# -------------------------
# Routes
# -------------------------
@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = random.choice(MOTIVATION_VERSES)
    return render_template("index.html", message="Study Hard, Pray Harder ❤❤❤", verse=verse, reference=reference)


@app.route("/content")
def content_page():
    try:
        ws_values = from_google_sheets() or []
        ws_title = ws_values[0] if ws_values else []
        ws_content = ws_values[1:] if len(ws_values) > 1 else []
        dr_values = folders_and_images()
        form_list = from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form")
        timetable_today = get_timetable_today()
        return render_template(
            "content.html",
            message=FOOTER_MESSAGE,
            sheet_content=ws_content,
            sheet_title=ws_title,
            today_date=check_date(),
            form_list=form_list,
            dr_values=dr_values,
            timetable_today=timetable_today
        )
    except Exception as e:
        app.logger.exception("Content page error: %s", e)
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], timetable_today=[], error=str(e))

# PART 4 of 5
# app.py — part 4/5 (continues)

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            attempt = (request.form.get("attempt") or "").strip()
            if not submitted_question:
                flash("⚠️ Please enter a question.", "warning")
                return redirect(url_for("questions_page"))
            if not attempt:
                flash("⚠️ Please enter your solution attempt before submitting.", "warning")
                return redirect(url_for("questions_page"))
            username = (request.form.get("name") or "").strip() or "Anonymous"
            choice_selected = (request.form.get("category") or "Other").strip()
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            try:
                append_row([choice_selected, username, submitted_question, attempt, timestamp])
                flash("✅ Question + attempt submitted!", "success")
                try:
                    result = send_push_to_subject(choice_selected, submitted_question, attempt)
                    app.logger.info("send_push_to_subject result: %s", result)
                except Exception as e:
                    app.logger.exception("send_push_to_subject error: %s", e)
            except Exception as e:
                app.logger.exception("Failed append_row: %s", e)
                flash("❌ Could not submit question. Try later.", "danger")
                return redirect(url_for("questions_page"))

            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=COOKIE_MAX_AGE, samesite='Lax')
                resp.set_cookie('nn_last_category', choice_selected, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting cookies on response")
            return resp

        selected_category = request.args.get("categoryFilter", "").strip()
        try:
            all_questions = get_questions() or []
        except Exception as e:
            app.logger.error("get_questions failed: %s", e)
            all_questions = []

        normalized_questions = []
        for row in all_questions:
            try:
                r = list(row)
                if len(r) < 5:
                    r += [""] * (5 - len(r))
                normalized_questions.append(r)
            except Exception:
                continue

        normalized_questions = [r for r in normalized_questions if not (len(r) >= 1 and str(r[0]) == "__subscription__")]

        derived_set = {str(q[0]).strip() for q in normalized_questions if q and len(q) > 0 and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]

        if selected_category:
            questions = [q for q in normalized_questions if len(q) > 0 and str(q[0]).strip() == selected_category]
        else:
            questions = normalized_questions

        return render_template(
            "questions.html",
            questions=questions,
            message=FOOTER_MESSAGE,
            categories=categories_for_ui,
            selected_category=selected_category,
            community_links=COMMUNITY_LINKS,
            vapid_public_key=VAPID_PUBLIC_KEY
        )

    except Exception as e:
        app.logger.exception("Questions page error: %s\n%s", e, traceback.format_exc())
        flash("❌ Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="", community_links=COMMUNITY_LINKS, vapid_public_key=VAPID_PUBLIC_KEY)

@app.route("/save-subscription", methods=["POST"])
def save_subscription_route():
    """
    Expects JSON: {subscription: {...}, subject: "Mathematics", client_id: "optional"}
    Saves via sheets.save_subscription if available else fallback to append row.
    Returns { ok: True, subjects: [...] } on success (subjects saved server-side for this endpoint).
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = None
        if isinstance(payload, dict):
            subscription = payload.get("subscription") or payload.get("push") or payload
        else:
            subscription = payload

        if isinstance(subscription, str):
            try:
                subscription = json.loads(subscription)
            except Exception:
                pass

        subject = _normalize_subject((payload.get("subject") if isinstance(payload, dict) else None) or request.args.get("subject") or "Other")
        client_id = payload.get("client_id") if isinstance(payload, dict) else None

        if not subscription or not _is_valid_subscription_obj(subscription):
            return jsonify({"ok": False, "error": "subscription required and must include endpoint"}), 400

        ok = _save_subscription_to_sheet(subject, client_id, subscription)
        endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None

        if ok and endpoint:
            try:
                subs = list_subscriptions() or []
                subjects_for_endpoint = []
                for r in subs:
                    try:
                        if isinstance(r, dict):
                            ep = None
                            ssub = r.get("subscription") or r.get("sub") or r
                            if isinstance(ssub, dict):
                                ep = ssub.get("endpoint")
                            subj = r.get("subject") or r.get("subj") or r.get("topic")
                            if ep == endpoint and subj:
                                subjects_for_endpoint.append(_normalize_subject(subj))
                        elif isinstance(r, (list, tuple)) and len(r) >= 4 and r[0] == "__subscription__":
                            sub_json = r[3]
                            try:
                                parsed = json.loads(sub_json)
                            except Exception:
                                parsed = None
                            if parsed and parsed.get("endpoint") == endpoint:
                                subj = _normalize_subject(r[1] if len(r) > 1 else "Other")
                                subjects_for_endpoint.append(subj)
                    except Exception:
                        continue
                subjects_for_endpoint = sorted(list(dict.fromkeys(subjects_for_endpoint)))
            except Exception:
                subjects_for_endpoint = []

            resp = jsonify({"ok": True, "subjects": subjects_for_endpoint})
            try:
                if endpoint:
                    resp.set_cookie('nn_subscription', endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting nn_subscription cookie on save")
            return resp, 200
        elif ok:
            return jsonify({"ok": True, "subjects": []}), 200
        else:
            return jsonify({"ok": False, "error": "could not save subscription"}), 500
    except Exception as e:
        app.logger.exception("save-subscription failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400

        out_subjects = []
        try:
            raw = list_subscriptions() or []
        except Exception:
            raw = []

        for r in raw:
            try:
                subj = None
                sub = None
                if isinstance(r, dict):
                    subj = r.get("subject")
                    sub = r.get("subscription") or r.get("sub") or r.get("push") or r
                elif isinstance(r, (list, tuple)) and len(r) >= 4 and r[0] == "__subscription__":
                    subj = r[1]
                    try:
                        sub = json.loads(r[3])
                    except Exception:
                        sub = None
                if not sub:
                    continue
                ep = sub.get("endpoint")
                if ep == endpoint:
                    if subj and subj not in out_subjects:
                        out_subjects.append(_normalize_subject(subj))
            except Exception:
                continue
        return jsonify({"ok": True, "subjects": out_subjects})
    except Exception as e:
        app.logger.exception("subscriptions_check failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/subscriptions/delete", methods=["POST"])
def subscriptions_delete():
    try:
        data = request.get_json(force=True, silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip() if isinstance(data, dict) else ""
        if not endpoint:
            endpoint = (request.cookies.get('nn_subscription') or "").strip()

        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        deleted = False
        try:
            try:
                delete_subscriptions_by_endpoint(endpoint)
                deleted = True
            except Exception as e:
                app.logger.debug("delete_subscriptions_by_endpoint failed (will try fallback): %s", e)
            try:
                rows = get_questions() or []
                indices = []
                for i, row in enumerate(rows, start=1):
                    try:
                        if len(row) >= 4 and str(row[0]) == "__subscription__":
                            try:
                                sub = json.loads(row[3])
                            except Exception:
                                sub = None
                            if sub and sub.get("endpoint") == endpoint:
                                indices.append(i)
                    except Exception:
                        continue
                if indices:
                    delete_rows_by_indices(indices)
                    deleted = True
            except Exception:
                app.logger.exception("Fallback deletion also failed")
        except Exception as e:
            app.logger.exception("subscriptions_delete inner failed: %s", e)

        resp = jsonify({"ok": True, "deleted": deleted})
        if deleted:
            try:
                resp.set_cookie('nn_subscription', '', expires=0, samesite='Lax')
            except Exception:
                app.logger.debug("Failed clearing nn_subscription cookie")
        return resp
    except Exception as e:
        app.logger.exception("subscriptions_delete top-level failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/vapid", methods=["GET"])
def api_vapid():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY or ""}), 200

# -------------------------
# Pending + ack endpoints (client fetch)
# -------------------------
@app.route('/notifications/pending', methods=['GET'])
def notifications_pending():
    try:
        endpoint = request.args.get('endpoint', '').strip() or request.cookies.get('nn_subscription', '').strip()
        if not endpoint:
            return jsonify({'ok': False, 'error': 'endpoint required'}), 400
        items = []
        try:
            items = fetch_pending_notifications_for_endpoint(endpoint)
        except Exception:
            app.logger.exception("fetch_pending_notifications_for_endpoint failed")
        return jsonify({'ok': True, 'items': items}), 200
    except Exception as e:
        app.logger.exception('notifications_pending failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/notifications/ack', methods=['POST'])
def notifications_ack():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        endpoint = (payload.get('endpoint') or '').strip() or request.cookies.get('nn_subscription', '').strip()
        ids = payload.get('ids') or []
        if not endpoint:
            return jsonify({'ok': False, 'error': 'endpoint required'}), 400
        if not isinstance(ids, list):
            return jsonify({'ok': False, 'error': 'ids must be a list'}), 400
        try:
            ack_notifications_by_ids(endpoint, ids)
        except Exception:
            app.logger.exception("ack_notifications_by_ids failed")
        return jsonify({'ok': True, 'count': len(ids)}), 200
    except Exception as e:
        app.logger.exception('notifications_ack failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route("/rules")
def rules_page():
    try:
        rules = read_rules() or []
    except Exception:
        rules = []
    GROUP_COMMUNITY_KEY = os.environ.get("GROUP_COMMUNITY_KEY")
    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE, GROUP_COMMUNITY_KEY=GROUP_COMMUNITY_KEY)
# PART 5 of 5
# app.py — part 5/5 (final)

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    result = None
    if request.method == "POST":
        provided_secret = (request.form.get("secret") or "").strip()
        if provided_secret == os.environ.get("ADMIN_CLEAR_SECRET"):
            try:
                clear_sheet()
                result = "✅ Questions cleared."
            except Exception as e:
                result = f"❌ Failed: {e}"
        else:
            result = "❌ Invalid secret!"
    return render_template("admin.html", result=result, message=FOOTER_MESSAGE)

@app.route("/admin/questions", methods=["GET", "POST"])
def admin_questions():
    provided_key = (request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or "").strip()
    if request.method == "GET" and not provided_key:
        return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        if request.method == "GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
        return "Unauthorized - include ?key=ADMIN_KEY or enter it into the Admin Key box on the page.", 401

    raw = get_questions() or []
    questions = []
    for idx, row in enumerate(raw, start=1):
        try:
            r = list(row)
            if len(r) < 5:
                r += [""] * (5 - len(r))
            questions.append({"index": idx, "subject": r[0], "user": r[1], "text": r[2], "attempt": r[3], "time": r[4]})
        except Exception:
            continue

    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete_selected":
            selected = request.form.getlist("selected")
            indices = []
            for i in selected:
                try:
                    indices.append(int(i))
                except Exception:
                    continue
            try:
                delete_rows_by_indices(indices)
                flash("✅ Selected rows deleted.", "success")
            except Exception as e:
                app.logger.exception("delete_rows_by_indices failed: %s", e)
                flash("⚠️ Could not delete selected rows (server error).", "warning")
        elif action == "delete_subject":
            subject = request.form.get("subject_to_delete")
            if subject:
                try:
                    delete_rows_by_subject(subject)
                    flash(f"✅ Deleted questions for subject {subject}.", "success")
                except Exception:
                    flash("⚠️ delete_rows_by_subject failed on server.", "warning")
        return redirect(url_for("admin_questions") + f"?key={provided_key}")

    subjects = sorted({q["subject"] for q in questions if q["subject"]})
    return render_template("admin_questions.html", questions=questions, subjects=subjects, key=provided_key, message=FOOTER_MESSAGE)

@app.route('/service-worker.js')
def service_worker():
    return send_from_directory(app.static_folder, 'service-worker.js')

@app.route('/admin/test-push', methods=['POST'])
def admin_test_push():
    key = (request.args.get('key') or request.form.get('key') or request.headers.get('X-ADMIN-KEY') or '').strip()
    if key != ADMIN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    content = request.get_json(silent=True) or request.form
    subject = (content.get('subject') or 'Mathematics')
    q = (content.get('question') or 'Test question')
    attempt = (content.get('attempt') or 'Sample answer')
    result = send_push_to_subject(subject, q, attempt)
    return jsonify({"ok": True, "result": result})

# -------------------------
# Coding routes / admin
# -------------------------
@app.route("/coding")
def coding_page():
    drive_id = request.args.get("drive_id")
    local_file = request.args.get("file")
    try:
        exercises = load_coding_exercises(drive_id=drive_id, local_path=local_file)
        return render_template("coding.html", exercises=exercises, message=FOOTER_MESSAGE)
    except Exception as e:
        app.logger.exception("coding_page render failed: %s", e)
        flash("Failed to load coding page resources. Check server logs.", "warning")
        return render_template("coding.html", exercises=[], message=FOOTER_MESSAGE)

@app.route("/admin/exercises", methods=["GET", "POST"])
def admin_exercises():
    provided_key = (request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or "").strip()
    if request.method == "GET" and not provided_key:
        return render_template("admin_exercises.html", key="", message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)

    if provided_key != EXERCISES_ADMIN_KEY:
        if request.method == "GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_exercises.html", key="", message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)
        return "Unauthorized - provide valid exercises admin key via ?key=... or the form field.", 401

    if request.method == "POST":
        ex = {
            "id": (request.form.get("id") or "").strip(),
            "title": (request.form.get("title") or "").strip(),
            "language": (request.form.get("language") or "js").strip(),
            "difficulty": (request.form.get("difficulty") or "").strip(),
            "description": (request.form.get("description") or "").strip()
        }
        starter = request.form.get("starter_code") or ""
        solution = request.form.get("solution_code") or ""
        if ex["language"] == "js":
            ex["starter_js"] = starter
            ex["solution_js"] = solution
        elif ex["language"] == "html":
            ex["starter_html"] = starter
            ex["solution_html"] = solution
        elif ex["language"] == "c":
            ex["starter_c"] = starter
            ex["solution_c"] = solution
        elif ex["language"] == "sql":
            ex["schema_sql"] = starter
            ex["example_query"] = solution
            sample_json = request.form.get("sample_data") or ""
            try:
                ex["sample_data"] = json.loads(sample_json) if sample_json.strip() else {}
            except Exception:
                flash("Sample data JSON was invalid — saved without sample_data.", "warning")
                ex["sample_data"] = {}

        if not ex["id"]:
            flash("Exercise id is required (unique short id)", "warning")
            return redirect(url_for("admin_exercises") + f"?key={provided_key}")

        target = request.form.get("target") or "local"
        drive_override = (request.form.get("drive_file_id") or "").strip() or None
        drive_file_to_use = drive_override or CODING_DRIVE_FILE_ID

        with _coding_lock:
            local_list = []
            p = Path(CODING_LOCAL_PATH)
            if p.exists():
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        local_list = json.load(fh)
                except Exception:
                    app.logger.exception("Failed to read existing local coding_exercises.json")
            ids = [e.get("id") for e in local_list if isinstance(e, dict)]
            if ex["id"] in ids:
                for i, e in enumerate(local_list):
                    if e.get("id") == ex["id"]:
                        local_list[i] = {**e, **ex}
                        break
                op = "updated"
            else:
                local_list.append(ex)
                op = "created"

            saved_local = False
            saved_drive = False
            try:
                tmp = p.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    json.dump(local_list, fh, ensure_ascii=False, indent=2)
                tmp.replace(p)
                saved_local = True
            except Exception:
                app.logger.exception("Failed to write local coding_exercises.json")

            if target == "drive" and drive_file_to_use and DRIVE_AVAILABLE:
                try:
                    raw = download_file(drive_file_to_use)
                    if raw:
                        try:
                            drive_list = json.loads(raw.decode("utf-8"))
                        except Exception:
                            drive_list = local_list
                    else:
                        drive_list = local_list
                    ids_drive = {e.get("id"): e for e in drive_list if isinstance(e, dict)}
                    ids_drive[ex["id"]] = {**ids_drive.get(ex["id"], {}), **ex}
                    merged = list(ids_drive.values())
                    if update_file(drive_file_to_use, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")):
                        saved_drive = True
                except Exception:
                    app.logger.exception("Drive update failed")

            if target == "drive" and saved_drive:
                flash(f"✅ Exercise {ex['id']} {op} and pushed to Drive.", "success")
            elif saved_local:
                flash(f"✅ Exercise {ex['id']} {op} and saved locally.", "success")
            else:
                flash("❌ Could not save exercise (check server logs & permissions).", "danger")

        return redirect(url_for("admin_exercises") + f"?key={provided_key}")

    return render_template("admin_exercises.html", key=provided_key, message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)

# -------------------------
# Health pages
# -------------------------
def _load_health_data():
    try:
        path = Path("data/health_data.json")
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        app.logger.debug("health_data.json load failed, using defaults")
    return [
        {"slug": "aloe-vera", "name": "Aloe vera", "virtues": ["skin-soothing"], "uses": "Topical", "notes": ""},
        {"slug": "ginger", "name": "Ginger", "virtues": ["antiemetic"], "uses": "Tea", "notes": ""}
    ]

@app.route("/health")
def health_index():
    try:
        items = _load_health_data()
        return render_template("health.html", plants=items, message=FOOTER_MESSAGE)
    except Exception as e:
        app.logger.exception("health_index failed: %s", e)
        flash("Failed to load health advice page.", "danger")
        return redirect(url_for("home_page"))

@app.route("/health/<slug>")
def health_detail(slug):
    try:
        items = _load_health_data()
        for p in items:
            if p.get("slug") == slug:
                return render_template("health_detail.html", plant=p, message=FOOTER_MESSAGE)
        flash("Item not found.", "warning")
        return redirect(url_for("health_index"))
    except Exception as e:
        app.logger.exception("health_detail failed: %s", e)
        flash("Failed to load detail.", "danger")
        return redirect(url_for("health_index"))

def _load_forms_data():
    try:
        path = Path("data/quiz_forms.json")
        if path.exists():
            with path.open("r", encoding="utf-8") as qf:
                return json.load(qf)
    except Exception:
        app.logger.debug("quiz_forms.json load failed, using defaults")
    return []

@app.route("/quizzes")
def quiz_forms():
    try:
        forms = _load_forms_data()
        return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)
    except Exception as e:
        app.logger.exception("Forms index failed, %s", e)
        flash("Failed to load Quiz Page.", "danger")
        return redirect(url_for("home_page"))
# PART 5 continued (final)
# SQL visualizer helpers & route (UPGRADED)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    """
    Build an in-memory sqlite DB from admin-provided schema_sql and sample_data.
    Returns (conn, populated_table_names)

    Improvements:
     - Accepts many statement styles and executes safely per-statement.
     - Attempts to fix common beginner mistakes like "CREATE TABLE name IF NOT EXISTS".
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if schema_sql:
        try:
            def fix_create_if_not_exists(sql_text):
                return re.sub(r"(CREATE\s+TABLE)\s+([`\"'\[\]\w]+)\s+IF\s+NOT\s+EXISTS", r"\1 IF NOT EXISTS \2", sql_text, flags=re.IGNORECASE)
            safe_schema = fix_create_if_not_exists(schema_sql)
            statements = [s.strip() for s in re.split(r";\s*(?=\b)", safe_schema) if s.strip()]
            for stmt in statements:
                try:
                    cur.executescript(stmt if stmt.endswith(";") else stmt + ";")
                except Exception as e:
                    app.logger.debug("Schema statement failed (continuing): %s -> %s", stmt[:200], e)
            conn.commit()
        except Exception as e:
            app.logger.exception("Schema execution failed: %s", e)

    populated = []
    if sample_data and isinstance(sample_data, dict):
        for table, rows in sample_data.items():
            if not isinstance(rows, list):
                continue
            cols = []
            for r in rows:
                if isinstance(r, dict):
                    for k in r.keys():
                        if k not in cols:
                            cols.append(k)
            if not cols:
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                    if cur.fetchone():
                        populated.append(table)
                    continue
                except Exception:
                    continue
            try:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                if not cur.fetchone():
                    cols_def = ", ".join([f'"{c}" TEXT' for c in cols])
                    cur.execute(f'CREATE TABLE "{table}" ({cols_def});')
            except Exception:
                pass
            placeholders = ", ".join(["?"] * len(cols))
            col_list_sql = ", ".join([f'"{c}"' for c in cols])
            insert_sql = f'INSERT INTO "{table}" ({col_list_sql}) VALUES ({placeholders})'
            for r in rows:
                try:
                    vals = []
                    if isinstance(r, dict):
                        for c in cols:
                            v = r.get(c, None)
                            vals.append(None if v is None else str(v))
                    else:
                        vals = [None] * len(cols)
                    cur.execute(insert_sql, vals)
                except Exception:
                    continue
            populated.append(table)
    conn.commit()
    return conn, populated

@app.route("/exercise-table/<exercise_id>", methods=["GET", "POST"])
def exercise_table_route(exercise_id):
    """
    Build an in-memory DB from the exercise's schema_sql and sample_data,
    then execute the SQL that comes from the text-editor (POST body: {sql: "..."})
    and return HTML results: SELECT tables or affected row counts per statement.
    """
    try:
        exercises = load_coding_exercises()
        ex = next((e for e in exercises if str(e.get("id")) == str(exercise_id)), None)
        if not ex:
            return jsonify({"ok": False, "error": "exercise not found"}), 404
        if ex.get("language") != "sql":
            return jsonify({"ok": False, "error": "exercise is not SQL"}), 400

        schema_sql = ex.get("schema_sql") or ""
        sample_data = ex.get("sample_data") or {}

        try:
            conn, tables = _safe_create_and_populate_sqlite(schema_sql, sample_data)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed preparing sample DB: {e}"}), 500

        try:
            payload = {}
            if request.method == "POST":
                try:
                    payload = request.get_json(force=True, silent=True) or {}
                except Exception:
                    payload = {}
            sql_from_client = (payload.get("sql") or "").strip()
        except Exception:
            sql_from_client = ""

        if not sql_from_client:
            cur = conn.cursor()
            if not tables:
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
                    tables = [r[0] for r in cur.fetchall()]
                except Exception:
                    tables = []
            if not tables:
                conn.close()
                return jsonify({"ok": True, "html": '<div class="small text-muted">No sample tables available for this exercise.</div>'})
            html_parts = []
            for tbl in tables:
                try:
                    cur.execute(f'SELECT * FROM "{tbl}" LIMIT {SQL_TABLE_ROW_LIMIT}')
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description] if cur.description else []
                    tbl_html = []
                    tbl_html.append(f'<div class="mb-2"><strong>Table: {tbl}</strong></div>')
                    tbl_html.append('<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>')
                    for c in cols:
                        tbl_html.append(f'<th>{c}</th>')
                    tbl_html.append('</tr></thead><tbody>')
                    if not rows:
                        tbl_html.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
                    else:
                        for r in rows:
                            tbl_html.append('<tr>')
                            for c in cols:
                                try:
                                    val = r[c]
                                except Exception:
                                    val = None
                                tbl_html.append('<td>%s</td>' % ("" if val is None else str(val)))
                            tbl_html.append('</tr>')
                    tbl_html.append('</tbody></table></div>')
                    html_parts.append("".join(tbl_html))
                except Exception as e:
                    html_parts.append(f'<div class="alert alert-warning small">Could not read table {tbl}: {e}</div>')
            conn.close()
            return jsonify({"ok": True, "html": "".join(html_parts)})

        cur = conn.cursor()
        statements = [s.strip() for s in re.split(r';\s*(?=\b)', sql_from_client) if s.strip()]
        html_out = []
        for i, stmt in enumerate(statements, start=1):
            try:
                stmt_upper = stmt.strip().split()[0].upper() if stmt.strip() else ""
                if stmt_upper == "SELECT" or stmt_upper == "WITH":
                    try:
                        cur.execute(stmt)
                        rows = cur.fetchall()
                        cols = [d[0] for d in cur.description] if cur.description else []
                        html_out.append(f'<div class="mb-2"><strong>Result (SELECT #{i})</strong></div>')
                        if not cols:
                            html_out.append('<div class="small text-muted">No columns returned.</div>')
                        else:
                            html_out.append('<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>')
                            for c in cols:
                                html_out.append(f'<th>{c}</th>')
                            html_out.append('</tr></thead><tbody>')
                            if not rows:
                                html_out.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
                            else:
                                for r in rows:
                                    html_out.append('<tr>')
                                    for c in cols:
                                        try:
                                            v = r[c]
                                        except Exception:
                                            v = None
                                        html_out.append('<td>%s</td>' % ("" if v is None else str(v)))
                                    html_out.append('</tr>')
                            html_out.append('</tbody></table></div>')
                    except Exception as qerr:
                        html_out.append(f'<div class="alert alert-danger small">SELECT error: {qerr}</div>')
                else:
                    try:
                        cur.execute(stmt)
                        affected = cur.rowcount if hasattr(cur, "rowcount") else None
                        conn.commit()
                        html_out.append(f'<div class="small text-muted">Statement #{i} executed. Affected rows: {affected if affected is not None else "unknown"}.</div>')
                    except Exception as nonqerr:
                        conn.rollback()
                        html_out.append(f'<div class="alert alert-warning small">Statement #{i} failed: {nonqerr}</div>')
            except Exception as e:
                html_out.append(f'<div class="alert alert-danger small">Execution error on statement #{i}: {e}</div>')

        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            existing_tables = [r[0] for r in cur.fetchall()]
            if existing_tables:
                html_out.append("<div class='mt-3'><strong>Current tables:</strong></div>")
                for tbl in existing_tables:
                    html_out.append(f'<div class="small-muted">- {tbl}</div>')
        except Exception:
            pass

        conn.close()
        return jsonify({"ok": True, "html": "\n".join(html_out)})
    except Exception as e:
        app.logger.exception("exercise_table_route failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------
# Global error handler
# -------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

# -------------------------
# App entry
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
