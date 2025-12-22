# PART 1/3 - app.py (imports, config, helpers, sheets/drive fallbacks, local sqlite init)
import os
import json
import base64
import logging
import traceback
import sqlite3
import time
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from flask import (
    Flask, render_template, url_for, redirect, send_file,
    request, flash, Response, jsonify, send_from_directory, make_response
)
from markupsafe import Markup  # moved from flask in newer versions
from dotenv import load_dotenv

# optional requests for fetching Drive files server-side
try:
    import requests
except Exception:
    requests = None

# optional pywebpush
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# load env
load_dotenv()
logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

# --- Config & constants
FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"
STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"
]

SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64", "").strip()
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

# Sheets / Drive keys (set these in env)
TIMETABLE_SHEET_KEY = os.environ.get("TIMETABLE_SHEET_KEY", "").strip()
QUESTIONS_SHEET_KEY = os.environ.get("QUESTIONS_SHEET_KEY", "").strip()
GROUP_RULES_SHEET_KEY = os.environ.get("GROUP_RULES_SHEET_KEY", "").strip()
QUESTIONS_SUBSCRIPTION_KEY = os.environ.get("QUESTIONS_SUBSCRIPTION_KEY", "").strip()

# coding config
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "").strip()

# admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", os.environ.get("ADMIN_CLEAR_SECRET", "")).strip()
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY).strip()

# VAPID keys (optional)
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# SQL table row limit for server rendering
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))

# cookie lifetime
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# local sqlite DB path for fallback storage (ensure defined before init)
DB_PATH = Path(os.environ.get("NN_LOCAL_DB", "nn_local.db"))

# write service account JSON if B64 provided
if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64.strip())
        json.loads(data.decode("utf-8"))
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
        app.logger.info("Wrote service account JSON to %s", SERVICE_ACCOUNT_PATH)
    except Exception:
        app.logger.exception("Failed writing service account from base64")

# --- sheets.py and drive.py imports with safe fallbacks
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
    # provide stubs for subscription functions; real ones implemented below fallback

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

# --- Robust local sqlite initialization for fallback storage ---
def _init_local_db():
    try:
        dbp = Path(DB_PATH)
        if not dbp.parent.exists():
            dbp.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_endpoint_delivered ON notifications(endpoint, delivered)")
        except Exception:
            pass
        conn.commit()
        conn.close()
        app.logger.info("Local SQLite DB ensured at %s", DB_PATH)
    except Exception:
        app.logger.exception("Failed initializing local subscription DB")

# ensure DB exists
_init_local_db()

# convenience lock
_coding_lock = Lock()

# inject globals into templates
@app.context_processor
def inject_globals():
    return {
        "message": FOOTER_MESSAGE,
        "vapid_public_key": VAPID_PUBLIC_KEY or "",
    }

# simple time parsing filter
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
# small helpers used below
def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    try:
        u = urlparse(endpoint)
        scheme = u.scheme or "https"
        netloc = u.netloc or u.path.split("/")[0]
        path = u.path or ""
        normalized = f"{scheme}://{netloc}{path}".rstrip("/")
        return normalized
    except Exception:
        return endpoint.strip().rstrip("/")

def _is_valid_subscription_obj(sub):
    if not sub or not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint") or sub.get("url")
    if not endpoint or not isinstance(endpoint, str):
        return False
    if sub.get("keys") and not isinstance(sub.get("keys"), dict):
        return False
    return True
# PART 2/3 - app.py (sheets-subscription fallbacks, loaders, public routes, questions & subscription endpoints)

# --- Sheets-backed subscription helpers (prefer Sheets but fallback to sqlite)
def save_subscription(subject, client_id, subscription_obj):
    """
    Try writing subscription to dedicated subscription sheet first, else fallback to questions sheet, else sqlite.
    """
    try:
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        subject = (subject or "Other").strip()
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = None
        if not subscription_obj or not _is_valid_subscription_obj(subscription_obj):
            return False
        endpoint = subscription_obj.get("endpoint")
        if not endpoint:
            return False

        # prefer dedicated sheet
        if QUESTIONS_SUBSCRIPTION_KEY and SHEETS_AVAILABLE:
            try:
                # attempt Sheets save via provided save_subscription if available
                try:
                    from sheets import save_subscription as sheets_save
                    sheets_save(subject, client_id or "", subscription_obj)
                    return True
                except Exception:
                    # fallback append_row with __subscription__ marker to questions sheet
                    pass
            except Exception:
                pass

        # fallback: append to questions sheet or sqlite
        try:
            append_row(["__subscription__", subject, client_id or "", json.dumps(subscription_obj), timestamp])
            return True
        except Exception:
            pass

        # last fallback: sqlite
        try:
            _init_local_db()
            with sqlite3.connect(str(DB_PATH)) as conn:
                c = conn.cursor()
                raw = json.dumps(subscription_obj)
                c.execute("""REPLACE INTO subscriptions(endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                             VALUES(?,?,?,?,?,?,?)""", (endpoint, subject, client_id or '', subscription_obj.get("keys", {}).get("p256dh",""), subscription_obj.get("keys", {}).get("auth",""), raw, timestamp))
                conn.commit()
            return True
        except Exception:
            app.logger.exception("sqlite fallback save_subscription failed")
            return False

    except Exception:
        app.logger.exception("save_subscription top-level failed")
        return False

def list_subscriptions(subject=None):
    out = []
    # try dedicated subscription sheet first
    if QUESTIONS_SUBSCRIPTION_KEY and SHEETS_AVAILABLE:
        try:
            raw = list_subscriptions  # if sheets provided this name, avoid shadow
        except Exception:
            pass
    # fallback: scan questions sheet for __subscription__ rows
    try:
        rows = get_questions() or []
        for r in rows:
            try:
                if len(r) >= 1 and str(r[0]) == "__subscription__":
                    subj = r[1] if len(r) > 1 else "Other"
                    client_id = r[2] if len(r) > 2 else ""
                    sub_json = r[3] if len(r) > 3 else None
                    if not sub_json:
                        continue
                    parsed = None
                    try:
                        parsed = json.loads(sub_json)
                    except Exception:
                        parsed = None
                    if parsed:
                        if not subject or subj == subject:
                            out.append({"subject": subj, "client_id": client_id, "subscription": parsed})
            except Exception:
                continue
    except Exception:
        app.logger.exception("list_subscriptions fallback failed")
    # also merge sqlite subscriptions
    try:
        _init_local_db()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            if subject:
                c.execute("SELECT subject, client_id, raw_json FROM subscriptions WHERE lower(subject)=?", (subject.lower(),))
            else:
                c.execute("SELECT subject, client_id, raw_json FROM subscriptions")
            rows = c.fetchall()
            for subj, client_id, raw in rows:
                try:
                    parsed = json.loads(raw) if raw else None
                except Exception:
                    parsed = None
                if parsed:
                    out.append({"subject": subj, "client_id": client_id, "subscription": parsed})
    except Exception:
        app.logger.debug("sqlite list_subscriptions failed, but continuing")
    return out

def delete_subscriptions_by_endpoint(endpoint):
    if not endpoint:
        return False
    # try dedicated sheet deletion first (if available)
    try:
        if QUESTIONS_SUBSCRIPTION_KEY and SHEETS_AVAILABLE:
            try:
                ws_rows = get_questions() or []
                indices = []
                for i, r in enumerate(ws_rows, start=1):
                    try:
                        if len(r) >= 1 and str(r[0]) == "__subscription__":
                            try:
                                parsed = json.loads(r[3]) if r[3] else None
                            except Exception:
                                parsed = None
                            if parsed and parsed.get("endpoint") == endpoint:
                                indices.append(i)
                    except Exception:
                        continue
                if indices:
                    delete_rows_by_indices(indices)
                    return True
            except Exception:
                pass
    except Exception:
        pass
    # fallback sqlite deletion
    try:
        _init_local_db()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
            conn.commit()
        return True
    except Exception:
        app.logger.exception("delete_subscriptions_by_endpoint fallback failed")
        return False

# --- Loaders: coding exercises & timetable (minimal robust versions)
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    drive_id = (drive_id or CODING_DRIVE_FILE_ID or "").strip() or None
    local_path = (local_path or CODING_LOCAL_PATH or "").strip() or None
    # Drive priority if available
    if drive_id and DRIVE_AVAILABLE:
        try:
            raw = download_file(drive_id)
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, list):
                        return data
                except Exception:
                    app.logger.exception("Failed parsing coding exercises from Drive file")
        except Exception:
            app.logger.exception("Failed downloading coding exercises from Drive")
    # local file fallback
    if local_path:
        try:
            p = Path(local_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
        except Exception:
            app.logger.exception("Failed reading local coding_exercises.json")
    # default sample
    return [
        {
            "id": "fizzbuzz",
            "title": "FizzBuzz",
            "language": "js",
            "difficulty": "Easy",
            "description": "Print numbers 1..N with FizzBuzz rules.",
            "starter_js": "for (let i=1;i<=100;i++){console.log(i);} ",
            "solution_js": "for (let i=1;i<=100;i++){let out=''; if(i%3===0) out+='Fizz'; if(i%5===0) out+='Buzz'; console.log(out||i);}"
        }
    ]

def get_timetable_today():
    try:
        rows = from_google_sheets() or []
        if not rows or len(rows) < 2:
            return []
        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]
        # simple heuristics for columns
        aliases = {
            "day": ["day", "weekday"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor"],
            "link": ["link", "location", "url", "meeting"],
            "notes": ["notes", "note", "remarks"]
        }
        def find_col(alias_list):
            for a in alias_list:
                for i,h in enumerate(header_lower):
                    if a in h:
                        return i
            return None
        col_day = find_col(aliases["day"]) or 0
        col_time = find_col(aliases["time"]) or 1
        col_subject = find_col(aliases["subject"]) or 2
        col_facilitator = find_col(aliases["facilitator"]) or 3
        col_link = find_col(aliases["link"]) or 4
        col_notes = find_col(aliases["notes"]) or 5
        today_short = datetime.now().strftime("%a").upper()
        today_full = datetime.now().strftime("%A").upper()
        matches = []
        for idx, r in enumerate(rows[1:], start=2):
            try:
                rlist = list(r)
                day_cell = str(rlist[col_day]).strip() if col_day < len(rlist) else ""
                if not day_cell:
                    continue
                first = day_cell.upper()
                if first.startswith(today_short) or today_full in first:
                    matches.append({
                        "row_index": idx,
                        "day": str(rlist[col_day]) if col_day < len(rlist) else "",
                        "time": str(rlist[col_time]) if col_time < len(rlist) else "",
                        "subject": str(rlist[col_subject]) if col_subject < len(rlist) else "",
                        "facilitator": str(rlist[col_facilitator]) if col_facilitator < len(rlist) else "",
                        "link": str(rlist[col_link]) if col_link < len(rlist) else "",
                        "notes": str(rlist[col_notes]) if col_notes < len(rlist) else ""
                    })
            except Exception:
                continue
        return matches
    except Exception:
        app.logger.exception("get_timetable_today failed")
        return []

# -------------------------
# Routes (public pages)
@app.route("/")
@app.route("/home")
def home_page():
    verse = "Study Hard, Pray Harder ❤❤❤"
    return render_template("index.html", message=FOOTER_MESSAGE, verse=verse, reference="")

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
            today_date=check_date() if SHEETS_AVAILABLE else "",
            form_list=form_list,
            dr_values=dr_values,
            timetable_today=timetable_today
        )
    except Exception as e:
        app.logger.exception("Content page error: %s", e)
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], timetable_today=[], error=str(e))

# --- Quizzes (restored) ---
@app.route("/quizzes")
def quiz_forms():
    try:
        forms = []
        # attempt to load forms metadata from Drive or data file
        forms = _load_forms_data() if ' _load_forms_data' in globals() else []
    except Exception:
        forms = []
    return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)

# Questions page (submit + list)
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
                    # If push configured, call send_push_to_subject (if present)
                    if 'send_push_to_subject' in globals():
                        result = send_push_to_subject(choice_selected, submitted_question, attempt)
                        app.logger.info("send_push_to_subject result: %s", result)
                except Exception:
                    app.logger.exception("send_push_to_subject error")
            except Exception:
                app.logger.exception("Failed append_row")
                flash("❌ Could not submit question. Try later.", "danger")
                return redirect(url_for("questions_page"))
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=COOKIE_MAX_AGE, samesite='Lax')
                resp.set_cookie('nn_last_category', choice_selected, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting cookies")
            return resp

        selected_category = request.args.get("categoryFilter", "").strip()
        try:
            all_questions = get_questions() or []
        except Exception:
            app.logger.exception("get_questions failed")
            all_questions = []
        normalized_questions = []
        for row in all_questions:
            try:
                r = list(row)
                while len(r) < 5:
                    r += [""]
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
            vapid_public_key=VAPID_PUBLIC_KEY,
            community_links={}
        )
    except Exception as e:
        app.logger.exception("Questions page error: %s", e)
        flash("❌ Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="", vapid_public_key=VAPID_PUBLIC_KEY, community_links={})

# --- Subscription endpoints (simple JSON APIs) ---
@app.route("/save-subscription", methods=["POST"])
def save_subscription_route():
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
        subject = (payload.get("subject") if isinstance(payload, dict) else None) or request.args.get("subject") or "Other"
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        if not subscription or not _is_valid_subscription_obj(subscription):
            return jsonify({"ok": False, "error": "subscription required"}), 400
        ok = save_subscription(subject, client_id, subscription)
        endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
        if ok:
            resp = jsonify({"ok": True, "subjects": []})
            try:
                if endpoint:
                    resp.set_cookie('nn_subscription', endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting cookie")
            return resp, 200
        return jsonify({"ok": False, "error": "could not save subscription"}), 500
    except Exception as e:
        app.logger.exception("save-subscription failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        out_subjects = []
        subs = list_subscriptions()
        for r in subs:
            try:
                subj = r.get("subject")
                sub = r.get("subscription")
                if sub and sub.get("endpoint") == endpoint:
                    if subj and subj not in out_subjects:
                        out_subjects.append(subj)
            except Exception:
                continue
        return jsonify({"ok": True, "subjects": out_subjects})
    except Exception as e:
        app.logger.exception("subscriptions_check failed")
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
        deleted = delete_subscriptions_by_endpoint(endpoint)
        resp = jsonify({"ok": True, "deleted": bool(deleted)})
        if deleted:
            try:
                resp.set_cookie('nn_subscription', '', expires=0, samesite='Lax')
            except Exception:
                pass
        return resp
    except Exception as e:
        app.logger.exception("subscriptions_delete failed")
        return jsonify({"ok": False, "error": str(e)}), 500

# service endpoints for web push (if you later enable)
@app.route("/api/vapid", methods=["GET"])
def api_vapid():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY or ""}), 200
# PART 3/3 - app.py (SQL visualizer, admin routes, static helpers, error handler, app entry)

# --- Pending notifications endpoints (simple pull model for clients if needed) ---
def save_notification_for_endpoint(endpoint, subject, title, body, data_obj):
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        _init_local_db()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications(endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""", (endpoint or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        return nid
    except Exception:
        app.logger.exception("save_notification_for_endpoint failed")
        return None

def fetch_pending_notifications_for_endpoint(endpoint):
    try:
        _init_local_db()
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
        app.logger.exception("fetch_pending_notifications_for_endpoint failed")
        return []

@app.route('/notifications/pending', methods=['GET'])
def notifications_pending():
    try:
        endpoint = request.args.get('endpoint', '').strip() or request.cookies.get('nn_subscription', '').strip()
        if not endpoint:
            return jsonify({'ok': False, 'error': 'endpoint required'}), 400
        items = fetch_pending_notifications_for_endpoint(endpoint)
        return jsonify({'ok': True, 'items': items}), 200
    except Exception as e:
        app.logger.exception('notifications_pending failed')
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
        # ack
        try:
            _init_local_db()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            with sqlite3.connect(str(DB_PATH)) as conn:
                c = conn.cursor()
                for nid in ids:
                    try:
                        c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint))
                    except Exception:
                        continue
                conn.commit()
        except Exception:
            app.logger.exception("ack_notifications failed")
        return jsonify({'ok': True, 'count': len(ids)}), 200
    except Exception as e:
        app.logger.exception('notifications_ack failed')
        return jsonify({'ok': False, 'error': str(e)}), 500

# -------------------------
# Admin routes (exercises management, protected)
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
        elif ex["language"] == "sql":
            ex["schema_sql"] = starter
            ex["example_query"] = solution
            sample_json = request.form.get("sample_data") or ""
            try:
                ex["sample_data"] = json.loads(sample_json) if sample_json.strip() else {}
            except Exception:
                flash("Sample data JSON invalid — saved without sample_data.", "warning")
                ex["sample_data"] = {}
        if not ex["id"]:
            flash("Exercise id is required (unique short id)", "warning")
            return redirect(url_for("admin_exercises") + f"?key={provided_key}")

        target = request.form.get("target") or "local"
        drive_file_to_use = (request.form.get("drive_file_id") or "").strip() or CODING_DRIVE_FILE_ID

        with _coding_lock:
            local_list = []
            p = Path(CODING_LOCAL_PATH)
            if p.exists():
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        local_list = json.load(fh)
                except Exception:
                    app.logger.exception("Failed reading existing coding_exercises.json")
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
                app.logger.exception("Failed writing local coding_exercises.json")
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

    # GET with valid key: show local exercises
    try:
        p = Path(CODING_LOCAL_PATH)
        exercises = []
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                exercises = json.load(fh)
    except Exception:
        app.logger.exception("Failed loading exercises for admin view")
        exercises = []
    return render_template("admin_exercises.html", exercises=exercises, key=provided_key, message=FOOTER_MESSAGE)

# -------------------------
# SQL visualizer for exercises (multi-statement allowed in-memory)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # execute schema (split on semicolons but allow multi-statement executescript)
    if schema_sql:
        try:
            statements = [s.strip() for s in re.split(r";\s*(?=\b)", schema_sql) if s.strip()]
            for stmt in statements:
                try:
                    cur.executescript(stmt if stmt.endswith(";") else stmt + ";")
                except Exception:
                    app.logger.debug("Schema stmt failed (continuing): %s", stmt[:200])
            conn.commit()
        except Exception:
            app.logger.exception("Schema execution failed")
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
        payload = {}
        if request.method == "POST":
            payload = request.get_json(force=True, silent=True) or {}
        sql_from_client = (payload.get("sql") or "").strip()
        cur = conn.cursor()
        if not sql_from_client:
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
        # execute client's SQL (allow multi-statement)
        statements = [s.strip() for s in re.split(r';\s*(?=\b)', sql_from_client) if s.strip()]
        html_out = []
        for i, stmt in enumerate(statements, start=1):
            try:
                stmt_upper = stmt.strip().split()[0].upper() if stmt.strip() else ""
                if stmt_upper in ("SELECT", "WITH"):
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
                        cur.executescript(stmt if stmt.endswith(";") else stmt + ";")
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
# Utility static routes: service-worker & favicon & debug
@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        return Response("", mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    try:
        return send_from_directory(app.static_folder, "favicon.ico")
    except Exception:
        return Response("", mimetype="image/x-icon")

@app.route("/debug/routes")
def debug_routes():
    lines = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        lines.append(f"{rule.endpoint}  ->  {rule.rule}")
    return "<br>".join(lines)

# -------------------------
# Error handler
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    # For HTTPExceptions, let Flask handle the status code
    try:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), e.code
    except Exception:
        pass
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

# -------------------------
# App entry
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = debug_mode)