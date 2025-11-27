# app.py  -- upgraded drop-in (replace your existing app.py)
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
from flask import url_for as flask_url_for
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
            "Service account file not found at %s — Sheets/Drive may fail.", SERVICE_ACCOUNT_PATH
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

# community links (json env)
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

# --- simple VAPID public key validator ---
def _is_likely_valid_vapid_public_key(s):
    if not s:
        return False
    key = s.strip()
    key = key.replace('-', '+').replace('_', '/')
    padding = '=' * ((4 - (len(key) % 4)) % 4)
    try:
        b = base64.b64decode(key + padding)
    except Exception as e:
        app.logger.warning("VAPID_PUBLIC_KEY appears not to be valid base64: %s", e)
        return False
    if len(b) in (65, 64):
        return True
    app.logger.warning("VAPID_PUBLIC_KEY decoded length is %d bytes (expected ~65).", len(b))
    return True

if VAPID_PUBLIC_KEY:
    ok = _is_likely_valid_vapid_public_key(VAPID_PUBLIC_KEY)
    if not ok:
        app.logger.warning("VAPID_PUBLIC_KEY looks invalid; push subscriptions may fail.")

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

    # fallback sqlite for subscriptions & notifications
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
                    app.logger.debug("save_subscription: invalid string")
                    return False
            if not isinstance(subscription, dict):
                return False
            endpoint = subscription.get('endpoint')
            if not endpoint:
                return False
            keys = subscription.get('keys') or {}
            p256dh = keys.get('p256dh') or ''
            auth = keys.get('auth') or ''
            subject_norm = (subject or 'Other').strip()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            raw = json.dumps(subscription)
            c.execute("""REPLACE INTO subscriptions(endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES(?,?,?,?,?,?,?)""", (endpoint_norm, subject_norm, client_id or '', p256dh, auth, raw, now))
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
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint_norm,))
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.exception("delete_subscriptions_by_endpoint failed: %s", e)

    def save_notification_for_endpoint(endpoint, subject, title, body, data_obj):
        try:
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            c.execute("""INSERT INTO notifications(endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""", (endpoint_norm, subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
            conn.close()
            return nid
        except Exception as e:
            app.logger.exception("save_notification_for_endpoint failed: %s", e)
            return None

    def fetch_pending_notifications_for_endpoint(endpoint):
        try:
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint_norm,))
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
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint_norm))
                except Exception:
                    continue
            conn.commit()
            changed = c.rowcount if hasattr(c, "rowcount") else len(ids)
            conn.close()
            return changed
        except Exception as e:
            app.logger.exception("ack_notifications_by_ids failed: %s", e)
            return 0

    def mark_notifications_delivered_for_endpoint(endpoint):
        try:
            endpoint_norm = (endpoint or '').strip().rstrip('/')
            conn = sqlite3.connect(str(DB_PATH))
            c = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint_norm))
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.exception("mark_notifications_delivered_for_endpoint failed: %s", e)

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

_coding_lock = Lock()

# Helpers
def _normalize_subject(s):
    return (s or "Other").strip()

def _normalize_endpoint(ep):
    try:
        return (ep or "").strip().rstrip('/')
    except Exception:
        return ep

# Safe url_for wrapper for templates (still allows original names)
def _safe_url_for(endpoint, **values):
    try:
        return flask_url_for(endpoint, **values)
    except Exception as e:
        app.logger.debug("safe_url_for could not build %s: %s", endpoint, e)
        return "#"

@app.context_processor
def inject_globals():
    return {
        "community_links": COMMUNITY_LINKS,
        "message": FOOTER_MESSAGE,
        "vapid_public_key": VAPID_PUBLIC_KEY or "",
        "url_for": _safe_url_for,
        "safe_url_for": _safe_url_for
    }

# Time formatting filter
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

# Timetable detection (improved)
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
        def find_col(alias_list):
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
                if not toks: continue
                for i, h in enumerate(header_lower):
                    if all(tok in h for tok in toks):
                        return i
            return None
        col_day = find_col(aliases["day"])
        col_time = find_col(aliases["time"])
        col_subject = find_col(aliases["subject"])
        col_facilitator = find_col(aliases["facilitator"])
        col_link = find_col(aliases["link"])
        col_notes = find_col(aliases["notes"])
        hl = len(header)
        if hl >= 6:
            if col_day is None: col_day = 0
            if col_time is None: col_time = 1
            if col_subject is None: col_subject = 2
            if col_facilitator is None: col_facilitator = 3
            if col_link is None: col_link = 4
            if col_notes is None: col_notes = 5
        else:
            def fallback(c_idx, pref):
                if c_idx is None and pref < hl: return pref
                return c_idx
            col_day = fallback(col_day, 0)
            col_time = fallback(col_time, 1 if hl > 1 else 0)
            col_subject = fallback(col_subject, 2 if hl > 2 else (hl-1))
            col_facilitator = fallback(col_facilitator, 3 if hl > 3 else (hl-1))
            col_link = fallback(col_link, 4 if hl > 4 else (hl-1))
            col_notes = fallback(col_notes, 5 if hl > 5 else (hl-1))

        app.logger.info("Timetable columns detected: day=%s time=%s subject=%s facilitator=%s link=%s notes=%s (header=%s)",
                        col_day, col_time, col_subject, col_facilitator, col_link, col_notes, header)

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
                        if ci is None or ci >= len(rlist): return ""
                        return rlist[ci] or ""
                    matches.append({
                        "row_index": idx,
                        "day": str(get_cell(col_day)).strip(),
                        "time": str(get_cell(col_time)).strip(),
                        "subject": str(get_cell(col_subject)).strip(),
                        "facilitator": str(get_cell(col_facilitator)).strip(),
                        "link": str(get_cell(col_link)).strip(),
                        "notes": str(get_cell(col_notes)).strip()
                    })
            except Exception:
                continue
        return matches
    except Exception:
        app.logger.exception("get_timetable_today failed")
        return []

# Coding exercises loader
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

# Drive image route (robust)
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

# Subscriptions helpers
def _is_valid_subscription_obj(sub):
    if not sub or not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint") or sub.get("url")
    if not endpoint or not isinstance(endpoint, str):
        return False
    return True

def _save_subscription_to_sheet(subject, client_id, subscription_obj):
    try:
        if not _is_valid_subscription_obj(subscription_obj):
            app.logger.warning("Invalid subscription object: %r", subscription_obj)
            return False
        subject = _normalize_subject(subject)
        endpoint = subscription_obj.get("endpoint")
        endpoint_norm = _normalize_endpoint(endpoint)
        if SHEETS_AVAILABLE:
            try:
                save_subscription(subject, client_id or "", subscription_obj)
                return True
            except Exception as e:
                app.logger.debug("sheets.save_subscription failed (fallback): %s", e)
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
                        if parsed:
                            parsed_ep = _normalize_endpoint(parsed.get("endpoint"))
                            if parsed_ep == endpoint_norm and _normalize_subject(subj) == subject:
                                found = True
                                break
                except Exception:
                    continue
            if not found:
                safe_sub = {"endpoint": endpoint_norm, "keys": subscription_obj.get("keys", {})}
                append_row(["__subscription__", subject, client_id or "", json.dumps(safe_sub), timestamp])
            return True
        except Exception:
            app.logger.exception("Fallback append for subscription failed")
            return False
    except Exception:
        app.logger.exception("Unexpected error in _save_subscription_to_sheet")
        return False

def _load_subscriptions_for_subject(subject):
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
                    if not sub_json: continue
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

def send_push_to_subject(subject, question_text, attempt_text):
    subs = _load_subscriptions_for_subject(subject)
    if not subs:
        app.logger.debug("No subscribers for subject %s", subject)
        return {"sent": 0, "errors": ["no subscribers"]}
    title = f"New question in {subject}"
    q_preview = (question_text or "").strip()
    a_preview = (attempt_text or "").strip()
    if len(q_preview) > 140: q_preview = q_preview[:137] + "…"
    if len(a_preview) > 180: a_preview = a_preview[:177] + "…"
    body = f"Q: {q_preview}\nA: {a_preview}"
    payload = {
        "title": title,
        "body": body,
        "url": "/questions",
        "tag": f"subject-{re.sub(r'\\s+','-', subject).lower()}",
        "renotify": True,
        "subject": subject,
        "preview": {"question": q_preview, "attempt": a_preview}
    }
    if not PYWEBPUSH_AVAILABLE or not VAPID_PRIVATE_KEY:
        app.logger.warning("pywebpush or VAPID private key not available — cannot send push. Persisting notifications.")
        try:
            for sub in subs:
                try:
                    if isinstance(sub, str):
                        try:
                            sub = json.loads(sub)
                        except Exception:
                            continue
                    endpoint = _normalize_endpoint(sub.get("endpoint"))
                    save_notification_for_endpoint(endpoint, subject, payload["title"], payload["body"], payload.get("preview"))
                except Exception:
                    continue
        except Exception:
            app.logger.exception("Failed to persist notifications when pywebpush unavailable")
        return {"sent": 0, "errors": ["pywebpush/VAPID not available"]}
    sent = 0
    errors = []
    for sub in subs:
        try:
            if isinstance(sub, str):
                try:
                    sub = json.loads(sub)
                except Exception:
                    app.logger.debug("Skipping invalid subscription string: %s", sub)
                    errors.append("invalid subscription format")
                    continue
            if not _is_valid_subscription_obj(sub):
                errors.append("invalid subscription object")
                continue
            endpoint = _normalize_endpoint(sub.get("endpoint"))
            try:
                save_notification_for_endpoint(endpoint, subject, payload["title"], payload["body"], payload.get("preview"))
            except Exception:
                app.logger.debug("Failed to persist notification for endpoint %s; continuing", endpoint)
            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_SUBJECT}
                )
                sent += 1
                try:
                    mark_notifications_delivered_for_endpoint(endpoint)
                except Exception:
                    app.logger.debug("Failed to mark notifications delivered for %s", endpoint)
            except WebPushException as ex:
                app.logger.warning("WebPushException for subject %s: %s", subject, ex)
                errors.append(str(ex))
                try:
                    resp = getattr(ex, 'response', None)
                    status = None
                    if resp and hasattr(resp, 'status_code'):
                        status = resp.status_code
                    if status in (404, 410):
                        if endpoint:
                            app.logger.info("Subscription endpoint %s returned %s: deleting subscription", endpoint, status)
                            try:
                                delete_subscriptions_by_endpoint(endpoint)
                            except Exception:
                                try:
                                    conn = sqlite3.connect(str(DB_PATH))
                                    c = conn.cursor()
                                    c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
                                    conn.commit()
                                    conn.close()
                                except Exception:
                                    app.logger.exception("Failed to delete stale endpoint from fallback DB")
                except Exception:
                    app.logger.exception("Exception while handling WebPushException cleanup")
            except Exception as ex:
                app.logger.exception("webpush failed: %s", ex)
                errors.append(str(ex))
        except Exception:
            app.logger.exception("Error while processing subscription record")
            errors.append("processing error")
    return {"sent": sent, "errors": errors}

# Routes
@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = random.choice(MOTIVATION_VERSES)
    return render_template("index.html", message="Study Hard, Pray Harder ❤❤❤", verse=verse, reference=reference)

@app.route("/content")
def content_page():
    sheet_content = []
    sheet_title = []
    dr_values = []
    form_list = []
    timetable_today = []
    sheet_status = {"ok": False, "reason": None}
    drive_status = {"ok": False, "reason": None}
    forms_status = {"ok": False, "reason": None}
    try:
        ws_values = from_google_sheets() or []
        if ws_values:
            sheet_status["ok"] = True
            sheet_title = ws_values[0] if ws_values else []
            sheet_content = ws_values[1:] if len(ws_values) > 1 else []
        else:
            sheet_status["ok"] = False
            sheet_status["reason"] = "no rows returned or sheet empty"
    except Exception as e:
        sheet_status["ok"] = False
        sheet_status["reason"] = f"exception: {e}"
        app.logger.exception("from_google_sheets failed in content_page: %s", e)
    try:
        dr_values = folders_and_images() or []
        drive_status["ok"] = True
    except Exception as e:
        drive_status["ok"] = False
        drive_status["reason"] = f"exception: {e}"
        app.logger.exception("folders_and_images failed in content_page: %s", e)
    try:
        forms_folder = os.environ.get("FORMS_FOLDER_ID", "") or None
        if forms_folder and DRIVE_AVAILABLE:
            try:
                form_list = from_google_drive(forms_folder, "application/vnd.google-apps.form") or []
                if form_list:
                    forms_status["ok"] = True
                else:
                    forms_status["ok"] = False
                    forms_status["reason"] = "no forms found"
            except Exception as e:
                forms_status["ok"] = False
                forms_status["reason"] = f"drive fetch exception: {e}"
                app.logger.exception("from_google_drive(forms) failed: %s", e)
        else:
            forms_status["ok"] = False
            forms_status["reason"] = "FORMS_FOLDER_ID not set or DRIVE_AVAILABLE is False"
    except Exception as e:
        forms_status["ok"] = False
        forms_status["reason"] = f"exception: {e}"
        app.logger.exception("forms listing failed in content_page: %s", e)
    try:
        timetable_today = get_timetable_today() or []
    except Exception as e:
        app.logger.exception("get_timetable_today failed in content_page: %s", e)
    try:
        return render_template(
            "content.html",
            message=FOOTER_MESSAGE,
            sheet_content=sheet_content,
            sheet_title=sheet_title,
            today_date=check_date() if 'check_date' in globals() else "",
            form_list=form_list,
            dr_values=dr_values,
            timetable_today=timetable_today,
            sheet_status=sheet_status,
            drive_status=drive_status,
            forms_status=forms_status
        )
    except Exception as e:
        app.logger.exception("Content page error: %s", e)
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], timetable_today=[], error=str(e), sheet_status=sheet_status, drive_status=drive_status, forms_status=forms_status)

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            attempt = (request.form.get("attempt") or "").strip()
            if not submitted_question:
                flash("⚠️ Please enter a question.", "warning")
                return redirect(flask_url_for("questions_page"))
            if not attempt:
                flash("⚠️ Please enter your solution attempt before submitting.", "warning")
                return redirect(flask_url_for("questions_page"))
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
                return redirect(flask_url_for("questions_page"))
            resp = redirect(flask_url_for("questions_page"))
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
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = payload.get("subscription") if isinstance(payload, dict) and payload.get("subscription") else payload.get("push") if isinstance(payload, dict) and payload.get("push") else payload
        if isinstance(subscription, str):
            try:
                subscription = json.loads(subscription)
            except Exception:
                pass
        subject = _normalize_subject((payload.get("subject") if isinstance(payload, dict) else None) or request.args.get("subject") or "Other")
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        if not subscription or not _is_valid_subscription_obj(subscription):
            return jsonify({"ok": False, "error": "subscription required and must include endpoint"}), 400
        try:
            if isinstance(subscription, dict) and subscription.get("endpoint"):
                subscription["endpoint"] = _normalize_endpoint(subscription.get("endpoint"))
        except Exception:
            pass
        ok = _save_subscription_to_sheet(subject, client_id, subscription)
        if ok:
            endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
            resp = jsonify({"ok": True})
            try:
                if endpoint:
                    resp.set_cookie('nn_subscription', endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting nn_subscription cookie on save")
            return resp, 200
        else:
            return jsonify({"ok": False, "error": "could not save subscription"}), 500
    except Exception as e:
        app.logger.exception("save-subscription failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip()
        if not endpoint:
            endpoint = (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        endpoint_norm = _normalize_endpoint(endpoint)
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
                    sub = r.get("subscription")
                elif isinstance(r, (list, tuple)) and len(r) >= 4 and r[0] == "__subscription__":
                    subj = r[1]
                    try:
                        sub = json.loads(r[3])
                    except Exception:
                        sub = None
                if not sub:
                    continue
                ep = _normalize_endpoint(sub.get("endpoint"))
                if ep and endpoint_norm and (ep == endpoint_norm):
                    if subj and subj not in out_subjects:
                        out_subjects.append(subj)
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
        endpoint_norm = _normalize_endpoint(endpoint)
        deleted = False
        try:
            try:
                delete_subscriptions_by_endpoint(endpoint_norm)
                deleted = True
            except Exception as e:
                app.logger.debug("delete_subscriptions_by_endpoint failed (fallback): %s", e)
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
                            if sub:
                                sub_ep = _normalize_endpoint(sub.get("endpoint"))
                                if sub_ep == endpoint_norm:
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

@app.route('/notifications/pending', methods=['GET'])
def notifications_pending():
    try:
        endpoint = request.args.get('endpoint', '').strip() or request.cookies.get('nn_subscription', '').strip()
        if not endpoint:
            return jsonify({'ok': False, 'error': 'endpoint required'}), 400
        endpoint_norm = _normalize_endpoint(endpoint)
        items = []
        try:
            items = fetch_pending_notifications_for_endpoint(endpoint_norm)
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
        endpoint_norm = _normalize_endpoint(endpoint)
        try:
            ack_notifications_by_ids(endpoint_norm, ids)
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
        return redirect(flask_url_for("admin_questions") + f"?key={provided_key}")
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
# Important: keep original endpoint "coding_page" so your templates are unchanged
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
            return redirect(flask_url_for("admin_exercises") + f"?key={provided_key}")
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
        return redirect(flask_url_for("admin_exercises") + f"?key={provided_key}")
    return render_template("admin_exercises.html", key=provided_key, message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)

# Health pages
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
        return redirect(flask_url_for("home_page"))

@app.route("/health/<slug>")
def health_detail(slug):
    try:
        items = _load_health_data()
        for p in items:
            if p.get("slug") == slug:
                return render_template("health_detail.html", plant=p, message=FOOTER_MESSAGE)
        flash("Item not found.", "warning")
        return redirect(flask_url_for("health_index"))
    except Exception as e:
        app.logger.exception("health_detail failed: %s", e)
        flash("Failed to load detail.", "danger")
        return redirect(flask_url_for("health_index"))

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
        return redirect(flask_url_for("home_page"))

# -------------------------
# SQL visualizer helpers & route
# -------------------------
def _normalize_schema_sql(schema_sql: str) -> str:
    if not schema_sql:
        return schema_sql or ""
    s = str(schema_sql)
    # Move misplaced IF NOT EXISTS into canonical position
    s = re.sub(r'(?i)\bCREATE\s+TABLE\s+([`"\[]?[\w\-:]+[`"\]]?)\s+IF\s+NOT\s+EXISTS\b', r'CREATE TABLE IF NOT EXISTS \1', s)
    s = re.sub(r';\s*;', ';', s)
    return s

def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> (sqlite3.Connection, list):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    populated = []
    if schema_sql:
        normalized = _normalize_schema_sql(schema_sql)
        try:
            cur.executescript(normalized)
        except Exception as e:
            app.logger.warning("Schema execution error (attempt salvage): %s", e)
            try:
                names = re.findall(r'(?i)\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`"\[]?([\w\-:]+)[`"\]]?', normalized)
                if names:
                    for name in names:
                        try:
                            cur.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (id TEXT);')
                            if name not in populated: populated.append(name)
                        except Exception:
                            continue
            except Exception:
                app.logger.exception("Failed salvage parsing of schema_sql")
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
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            if t not in populated:
                populated.append(t)
    except Exception:
        pass
    return conn, populated

@app.route("/exercise-table/<exercise_id>", methods=["GET", "POST"])
def exercise_table_route(exercise_id):
    """
    POST logic:
      - If JSON body contains { "sql": "<query>" }, attempt to execute that query against the exercise sample DB.
        * Only allowed: SELECT / PRAGMA / EXPLAIN / WITH (case-insensitive).
      - Otherwise returns server-rendered sample tables (SELECT * FROM each).
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

        conn, tables = _safe_create_and_populate_sqlite(schema_sql, sample_data)
        cur = conn.cursor()

        # Check if user provided SQL in the POST body to execute
        try:
            payload = {}
            if request.method == "POST":
                try:
                    payload = request.get_json(force=True, silent=True) or {}
                except Exception:
                    payload = {}
            sql_text = (payload.get("sql") or "").strip()
        except Exception:
            sql_text = ""

        if sql_text:
            # allow only read/inspection queries
            allowed_prefixes = ("select", "pragma", "explain", "with")
            first = sql_text.lstrip().split(None, 1)
            first_word = first[0].lower() if first else ""
            if not any(first_word.startswith(p) for p in allowed_prefixes):
                conn.close()
                return jsonify({"ok": False, "error": "Only SELECT/PRAGMA/EXPLAIN/WITH queries are allowed for execution."}), 400
            try:
                cur.execute(sql_text)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                tbl_html = []
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
                conn.close()
                return jsonify({"ok": True, "html": "".join(tbl_html)})
            except Exception as e:
                conn.close()
                return jsonify({"ok": False, "error": f"Query execution failed: {e}"}), 400

        # No SQL provided -> render sample tables as before
        if not tables:
            try:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
                tables = [r[0] for r in cur.fetchall()]
            except Exception:
                tables = []
        if not tables:
            html = '<div class="small text-muted">No sample tables available for this exercise.</div>'
            conn.close()
            return jsonify({"ok": True, "html": html})
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
    except Exception as e:
        app.logger.exception("exercise_table_route failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
