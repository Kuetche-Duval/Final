# PART 1/10 - BOOTSTRAP
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
    request, flash, Response, jsonify, send_from_directory, make_response, abort
)
from dotenv import load_dotenv

# optional requests for server-side Drive image fetch
try:
    import requests
except Exception:
    requests = None

# optional pywebpush for server push
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# load .env
load_dotenv()

# --- service account optional handling (if you store a base64 service account) ---
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

# Flask app
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
logging.basicConfig(level=logging.INFO)
logger = app.logger

# Footer and UI
FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"
STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]

# VAPID keys: set these environment variables if you want real push delivery (recommended)
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# Other env / constants
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))
COOKIE_MAX_AGE = 365 * 24 * 60 * 60
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB)

# Admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", os.environ.get("ADMIN_CLEAR_SECRET", ""))
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# Coding exercises paths
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "").strip()

# Community links (optional JSON in env)
try:
    COMMUNITY_LINKS = json.loads(os.environ.get("COMMUNITY_LINKS_JSON", "{}") or "{}")
except Exception:
    COMMUNITY_LINKS = {}

# Simple VAPID public key check
def _is_likely_valid_vapid_public_key(s):
    if not s: return False
    key = s.strip().replace('-', '+').replace('_', '/')
    padding = '=' * ((4 - (len(key) % 4)) % 4)
    try:
        b = base64.b64decode(key + padding)
    except Exception:
        return False
    return len(b) in (64, 65)
if VAPID_PUBLIC_KEY and not _is_likely_valid_vapid_public_key(VAPID_PUBLIC_KEY):
    app.logger.warning("VAPID_PUBLIC_KEY looks suspicious — double-check.")
# PART 2/10 - SHEETS FALLBACK + SQLITE INIT
# Try to import sheets helper (if you have one). Otherwise supply fallbacks.
try:
    from sheets import (
        from_google_sheets, check_date, append_row, read_rules, get_questions,
        clear_sheet, delete_rows_by_indices, delete_rows_by_subject,
        save_subscription, list_subscriptions, delete_subscriptions_by_endpoint
    )
    SHEETS_AVAILABLE = True
except Exception as e:
    app.logger.warning("sheets.py import failed: %s — falling back to sqlite", e)
    SHEETS_AVAILABLE = False
    def from_google_sheets(*a, **k): return []
    def check_date(*a, **k): return ""
    def append_row(row, key=None):
        app.logger.info("append_row fallback called: %s", row)
    def read_rules(*a, **k): return []
    def get_questions(*a, **k): return []
    def clear_sheet(*a, **k): app.logger.info("clear_sheet fallback")
    def delete_rows_by_indices(*a, **k): app.logger.info("delete_rows_by_indices fallback")
    def delete_rows_by_subject(*a, **k): app.logger.info("delete_rows_by_subject fallback")
    def save_subscription(subject, client_id, subscription): return False
    def list_subscriptions(subject=None): return []
    def delete_subscriptions_by_endpoint(endpoint): return None

# TIMETABLE: detect columns flexibly and return today's rows
def get_timetable_today():
    """
    Loads values with from_google_sheets() (if available) and attempts to detect
    useful columns (day, time, subject, facilitator, link, notes). Returns a
    list of dict entries only for rows that match today's day name.
    """
    try:
        rows = from_google_sheets() or []
        if not rows or len(rows) < 2:
            return []

        # header detection
        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]

        aliases = {
            "day": ["day", "weekday", "day of week"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor", "staff"],
            "link": ["link", "location", "url", "meeting"],
            "notes": ["notes", "note", "remarks", "comments"]
        }

        def find_col_for_alias_list(alias_list):
            # exact match first
            for a in alias_list:
                a_norm = a.lower().strip()
                for i, h in enumerate(header_lower):
                    if h == a_norm:
                        return i
            # substring match
            for a in alias_list:
                a_norm = a.lower().strip()
                for i, h in enumerate(header_lower):
                    if a_norm in h:
                        return i
            # all tokens present
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

        # sensible defaults if header is short
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

        # detect today strings
        today_short = datetime.now().strftime("%a").upper()   # e.g. MON
        today_full = datetime.now().strftime("%A").upper()    # e.g. Monday

        matches = []
        for idx, r in enumerate(rows[1:], start=2):
            try:
                rlist = list(r)
                # pick day cell
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
                # skip malformed row
                continue

        return matches
    except Exception:
        app.logger.exception("get_timetable_today failed")
        return []

# Ensure sqlite local DB and tables exist (subscriptions + notifications)
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
    except Exception:
        app.logger.exception("Failed initializing local DB")

_init_local_db()

def _ensure_local_db_tables():
    try:
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS subscriptions (endpoint TEXT PRIMARY KEY, subject TEXT, client_id TEXT, p256dh TEXT, auth TEXT, raw_json TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT, subject TEXT, title TEXT, body TEXT, data TEXT, created_at TEXT, delivered INTEGER DEFAULT 0, delivered_at TEXT)")
        conn.commit()
        conn.close()
    except Exception:
        app.logger.exception("_ensure_local_db_tables failed")
# PART 3/10 - NORMALIZE + SUBS HELPERS + DRIVE FALLBACK + TEMPLATE CONTEXT
def normalize_endpoint(endpoint):
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

def save_subscription_sqlite(subject, client_id, subscription):
    try:
        _ensure_local_db_tables()
        if isinstance(subscription, str):
            subscription = json.loads(subscription)
        endpoint = subscription.get("endpoint")
        if not endpoint:
            return False
        keys = subscription.get("keys") or {}
        p256dh = keys.get("p256dh") or ""
        auth = keys.get("auth") or ""
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            raw = json.dumps(subscription)
            c.execute("""REPLACE INTO subscriptions(endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES(?,?,?,?,?,?,?)""", (endpoint, subject or "Other", client_id or "", p256dh, auth, raw, now))
            conn.commit()
        return True
    except Exception:
        app.logger.exception("save_subscription_sqlite failed")
        return False

def list_subscriptions_sqlite(subject=None):
    try:
        _ensure_local_db_tables()
        out = []
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            if subject:
                subj = subject.strip().lower()
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions WHERE lower(subject)=?", (subj,))
            else:
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions")
            rows = c.fetchall()
        for r in rows:
            subj, client_id, raw_json, created_at = r
            out.append(["__subscription__", subj, client_id or "", raw_json or "{}", created_at or ""])
        return out
    except Exception:
        app.logger.exception("list_subscriptions_sqlite failed")
        return []

# Drive fallback imports
try:
    from drive import from_google_drive, folders_and_images, download_file, update_file, create_file_in_folder
    DRIVE_AVAILABLE = True
except Exception:
    DRIVE_AVAILABLE = False
    def from_google_drive(*a, **k): return []
    def folders_and_images(*a, **k): return []
    def download_file(*a, **k): return b""
    def update_file(*a, **k): return False
    def create_file_in_folder(*a, **k): return None

# Context processor
@app.context_processor
def inject_globals():
    return {"community_links": COMMUNITY_LINKS, "message": FOOTER_MESSAGE, "vapid_public_key": VAPID_PUBLIC_KEY or ""}

# load coding exercises helper (was missing earlier — restored)
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    drive_id = (drive_id or CODING_DRIVE_FILE_ID or "").strip() or None
    local_path = (local_path or CODING_LOCAL_PATH or "").strip() or None
    # try drive
    if drive_id and DRIVE_AVAILABLE:
        try:
            raw = download_file(drive_id)
            if raw:
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            app.logger.exception("Failed loading exercises from Drive")
    # try local
    if local_path:
        try:
            p = Path(local_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
        except Exception:
            app.logger.exception("Failed loading local exercises")
    # fallback
    return [
        {"id": "sales", "title": "Sales example (SQL)", "language": "sql",
         "schema_sql": "CREATE TABLE IF NOT EXISTS Products (product_id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE IF NOT EXISTS Sales (sale_id INTEGER PRIMARY KEY, product_id INTEGER, quantity_sold INTEGER, sale_date TEXT, total_price REAL);",
         "sample_data": {"Products": [{"product_id":101,"name":"Phone"},{"product_id":102,"name":"Charger"}], "Sales": [{"sale_id":1,"product_id":101,"quantity_sold":5,"sale_date":"2024-01-01","total_price":2500.0}]},
         "example_query": "SELECT * FROM Sales;"}
    ]
# PART 4/10 - TIMETABLE HELPER, UTILITIES, SQL SPLITTER & SQL ENGINE
import html

def _try_parse_time(s):
    if not s: return None
    if isinstance(s, datetime): return s
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
        return datetime(2000,1,1,hh,mm,ss)
    return None

def format_time(value):
    if not value: return ""
    dt = _try_parse_time(value)
    if dt:
        return dt.strftime("%b %d %I:%M %p").lstrip("0")
    return str(value)
app.jinja_env.filters["format_time"] = format_time

def get_timetable_today():
    try:
        rows = from_google_sheets() or []
        if not rows or len(rows) < 2: return []
        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]
        aliases = {"day":["day","weekday"], "time":["time","hour","period"], "subject":["subject","course","topic"], "facilitator":["facilitator","teacher","instructor"], "link":["link","location","url"], "notes":["notes","remarks"]}
        def find_col(alias_list):
            for a in alias_list:
                if a.lower() in header_lower:
                    return header_lower.index(a.lower())
            for a in alias_list:
                for i,h in enumerate(header_lower):
                    if a.lower() in h:
                        return i
            return None
        col_day = find_col(aliases["day"])
        col_time = find_col(aliases["time"])
        col_subject = find_col(aliases["subject"])
        col_fac = find_col(aliases["facilitator"])
        col_link = find_col(aliases["link"])
        col_notes = find_col(aliases["notes"])
        today_short = datetime.now().strftime("%a").upper()
        today_full = datetime.now().strftime("%A").upper()
        matches = []
        for idx,r in enumerate(rows[1:], start=2):
            try:
                rlist = list(r)
                day_cell = ""
                if col_day is not None and col_day < len(rlist):
                    day_cell = str(rlist[col_day]).strip()
                elif len(rlist) > 0:
                    day_cell = str(rlist[0]).strip()
                if not day_cell: continue
                first = day_cell.upper()
                if first.startswith(today_short) or first.startswith(today_full[:3]) or today_full in first:
                    def get_cell(ci):
                        if ci is None or ci >= len(rlist): return ""
                        return rlist[ci] or ""
                    matches.append({"row_index": idx, "day": str(get_cell(col_day)).strip(), "time": str(get_cell(col_time)).strip(), "subject": str(get_cell(col_subject)).strip(), "facilitator": str(get_cell(col_fac)).strip(), "link": str(get_cell(col_link)).strip(), "notes": str(get_cell(col_notes)).strip()})
            except Exception:
                continue
        return matches
    except Exception:
        app.logger.exception("get_timetable_today failed")
        return []

# SQL splitter - robust multi-statement parser that ignores comments and respects quotes
def split_sql_statements(sql_text: str):
    if not sql_text: return []
    s = sql_text
    length = len(s); i = 0
    statements = []; current = []; in_single = False; in_double = False; in_block_comment = False
    while i < length:
        ch = s[i]; nch = s[i+1] if i+1<length else ''
        if not in_single and not in_double and not in_block_comment and ch=='/' and nch=='*':
            in_block_comment = True; i += 2; continue
        if in_block_comment:
            if ch=='*' and nch=='/': in_block_comment=False; i+=2; continue
            i+=1; continue
        if not in_single and not in_double and ch=='-' and nch=='-':
            i+=2
            while i<length and s[i] not in ('\n','\r'): i+=1
            continue
        if ch=="'" and not in_double:
            if in_single:
                if nch=="'": current.append(ch); current.append(nch); i+=2; continue
                in_single=False; current.append(ch); i+=1; continue
            else:
                in_single=True; current.append(ch); i+=1; continue
        if ch=='"' and not in_single:
            if in_double:
                if nch=='"': current.append(ch); current.append(nch); i+=2; continue
                in_double=False; current.append(ch); i+=1; continue
            else:
                in_double=True; current.append(ch); i+=1; continue
        if ch==';' and not in_single and not in_double and not in_block_comment:
            stmt=''.join(current).strip()
            if stmt: statements.append(stmt)
            current=[]; i+=1
            while i<length and s[i].isspace(): i+=1
            continue
        current.append(ch); i+=1
    tail=''.join(current).strip()
    if tail: statements.append(tail)
    return statements

def _prepare_sqlite_conn_for_exec(conn):
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        pass
# PART 5/10 - SQL ENGINE (exercise_table_route)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _prepare_sqlite_conn_for_exec(conn)
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
            conn.commit()
        except Exception:
            # try per-statement
            for stmt in split_sql_statements(schema_sql):
                try:
                    cur.execute(stmt)
                except Exception:
                    try:
                        cur.executescript(stmt)
                    except Exception:
                        app.logger.debug("schema statement failed: %s", stmt[:200])
            conn.commit()
    populated = []
    if sample_data and isinstance(sample_data, dict):
        for table, rows in sample_data.items():
            if not isinstance(rows, list): continue
            cols=[]
            for r in rows:
                if isinstance(r, dict):
                    for k in r.keys():
                        if k not in cols: cols.append(k)
            if not cols:
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                    if cur.fetchone(): populated.append(table)
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
                    vals=[]
                    if isinstance(r, dict):
                        for c in cols:
                            v = r.get(c, None)
                            vals.append(None if v is None else str(v))
                    else:
                        vals=[None]*len(cols)
                    cur.execute(insert_sql, vals)
                except Exception:
                    continue
            populated.append(table)
    conn.commit()
    return conn, populated

@app.route("/exercise-table/<exercise_id>", methods=["GET","POST"])
def exercise_table_route(exercise_id):
    try:
        exercises = load_coding_exercises()
        ex = next((e for e in exercises if str(e.get("id"))==str(exercise_id)), None)
        if not ex: return jsonify({"ok":False,"error":"exercise not found"}),404
        if ex.get("language")!="sql": return jsonify({"ok":False,"error":"exercise is not SQL"}),400
        schema_sql = ex.get("schema_sql") or ""
        sample_data = ex.get("sample_data") or {}
        try:
            conn, tables = _safe_create_and_populate_sqlite(schema_sql, sample_data)
        except Exception as e:
            return jsonify({"ok":False,"error":f"Failed preparing sample DB: {e}"}),500
        payload={}
        if request.method=="POST":
            try:
                payload = request.get_json(force=True, silent=True) or {}
            except Exception:
                payload = {}
        sql_from_client = (payload.get("sql") or "").strip()
        if not sql_from_client:
            cur = conn.cursor()
            try:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
                tables = [r[0] for r in cur.fetchall()]
            except Exception:
                tables=[]
            if not tables:
                conn.close()
                return jsonify({"ok":True,"html":'<div class="small text-muted">No sample tables available for this exercise.</div>'})
            html_parts=[]
            for tbl in tables:
                try:
                    cur.execute(f'SELECT * FROM "{tbl}" LIMIT {SQL_TABLE_ROW_LIMIT}')
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description] if cur.description else []
                    tbl_html=[f'<div class="mb-2"><strong>Table: {html.escape(tbl)}</strong></div>','<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>']
                    for c in cols: tbl_html.append(f'<th>{html.escape(c)}</th>')
                    tbl_html.append('</tr></thead><tbody>')
                    if not rows:
                        tbl_html.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
                    else:
                        for r in rows:
                            tbl_html.append('<tr>')
                            for c in cols:
                                try: val=r[c]
                                except Exception: val=None
                                tbl_html.append('<td>%s</td>' % ("" if val is None else html.escape(str(val))))
                            tbl_html.append('</tr>')
                    tbl_html.append('</tbody></table></div>')
                    html_parts.append("".join(tbl_html))
                except Exception as e:
                    html_parts.append(f'<div class="alert alert-warning small">Could not read table {html.escape(tbl)}: {html.escape(str(e))}</div>')
            conn.close()
            return jsonify({"ok":True,"html":"".join(html_parts)})
        cur = conn.cursor()
        try:
            statements = split_sql_statements(sql_from_client)
        except Exception:
            statements = [s.strip() for s in re.split(r';\s*(?=\b)', sql_from_client) if s.strip()]
        html_out=[]
        for i, stmt in enumerate(statements, start=1):
            try:
                first = stmt.strip().split(None,1)
                stmt_upper = first[0].upper() if first else ""
                if stmt_upper in ("SELECT","WITH"):
                    try:
                        cur.execute(stmt)
                        rows = cur.fetchall()
                        cols = [d[0] for d in cur.description] if cur.description else []
                        html_out.append(f'<div class="mb-2"><strong>Result (SELECT #{i})</strong></div>')
                        if not cols:
                            html_out.append('<div class="small text-muted">No columns returned.</div>')
                        else:
                            html_out.append('<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>')
                            for c in cols: html_out.append(f'<th>{html.escape(c)}</th>')
                            html_out.append('</tr></thead><tbody>')
                            if not rows:
                                html_out.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
                            else:
                                for r in rows:
                                    html_out.append('<tr>')
                                    for c in cols:
                                        try: v=r[c]
                                        except Exception: v=None
                                        html_out.append('<td>%s</td>' % ("" if v is None else html.escape(str(v))))
                                    html_out.append('</tr>')
                            html_out.append('</tbody></table></div>')
                    except Exception as qerr:
                        html_out.append(f'<div class="alert alert-danger small">SELECT error: {html.escape(str(qerr))}</div>')
                else:
                    try:
                        cur.execute(stmt)
                        affected = cur.rowcount if hasattr(cur,"rowcount") else None
                        conn.commit()
                        html_out.append(f'<div class="small text-muted">Statement #{i} executed. Affected rows: {affected if affected is not None else "unknown"}.</div>')
                    except sqlite3.OperationalError:
                        try:
                            cur.executescript(stmt)
                            conn.commit()
                            html_out.append(f'<div class="small text-muted">Statement #{i} executed via executescript.</div>')
                        except Exception as ee:
                            conn.rollback()
                            html_out.append(f'<div class="alert alert-warning small">Statement #{i} failed: {html.escape(str(ee))}</div>')
                    except Exception as nonqerr:
                        conn.rollback()
                        html_out.append(f'<div class="alert alert-warning small">Statement #{i} failed: {html.escape(str(nonqerr))}</div>')
            except Exception as e:
                html_out.append(f'<div class="alert alert-danger small">Execution error on statement #{i}: {html.escape(str(e))}</div>')
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            existing_tables = [r[0] for r in cur.fetchall()]
            if existing_tables:
                html_out.append("<div class='mt-3'><strong>Current tables:</strong></div>")
                for tbl in existing_tables: html_out.append(f'<div class="small-muted">- {html.escape(tbl)}</div>')
        except Exception:
            pass
        conn.close()
        return jsonify({"ok":True,"html":"\n".join(html_out)})
    except Exception as e:
        app.logger.exception("exercise_table_route failed: %s", e)
        return jsonify({"ok":False,"error":str(e)}),500
# PART 6/10 - SUBSCRIPTION PERSISTENCE WRAPPER (Sheets preferred, sqlite fallback)
def _is_valid_subscription_obj(sub):
    if not sub or not isinstance(sub, dict): return False
    endpoint = sub.get("endpoint") or sub.get("url")
    if not endpoint or not isinstance(endpoint, str): return False
    if sub.get("keys") and not isinstance(sub.get("keys"), dict): return False
    return True

def _normalize_subject(s):
    return (s or "Other").strip()

def _save_subscription_to_sheet(subject, client_id, subscription_obj):
    try:
        if not _is_valid_subscription_obj(subscription_obj):
            app.logger.warning("Invalid subscription object: %s", subscription_obj)
            return False
        subject = _normalize_subject(subject)
        endpoint = subscription_obj.get("endpoint")
        if SHEETS_AVAILABLE:
            try:
                save_subscription(subject, client_id or "", subscription_obj)
                return True
            except Exception:
                app.logger.debug("sheets.save_subscription failed, falling back")
        # dedupe check via get_questions fallback
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        try:
            rows = get_questions() or []
        except Exception:
            rows = []
        found=False
        for r in rows:
            try:
                if len(r)>=1 and str(r[0])=="__subscription__":
                    subj = str(r[1]) if len(r)>1 else "Other"
                    sub_json = r[3] if len(r)>3 else None
                    if not sub_json: continue
                    try: parsed=json.loads(sub_json)
                    except Exception: parsed=None
                    if parsed and parsed.get("endpoint")==endpoint and _normalize_subject(subj)==subject:
                        found=True; break
            except Exception:
                continue
        if not found:
            safe_sub={"endpoint": endpoint, "keys": subscription_obj.get("keys", {})}
            append_row(["__subscription__", subject, client_id or "", json.dumps(safe_sub), timestamp])
        # fallback sqlite save
        try:
            save_subscription_sqlite(subject, client_id or "", subscription_obj)
        except Exception:
            app.logger.debug("sqlite save fallback failed")
        return True
    except Exception:
        app.logger.exception("_save_subscription_to_sheet error")
        return False

def _load_subscriptions_for_subject(subject):
    subs=[]
    try:
        subject_norm=_normalize_subject(subject)
        if SHEETS_AVAILABLE:
            try:
                raw=list_subscriptions(subject_norm) or []
                for r in raw:
                    if isinstance(r, dict):
                        sub = r.get("subscription") or r.get("sub") or r.get("push") or r
                        if isinstance(sub, dict) and _is_valid_subscription_obj(sub):
                            if not subject or _normalize_subject(r.get("subject") or "")==subject_norm:
                                subs.append(sub)
                    elif isinstance(r,(list,tuple)) and len(r)>0 and r[0]=="__subscription__":
                        sub_json = r[3] if len(r)>3 else None
                        if sub_json:
                            try:
                                parsed=json.loads(sub_json)
                                if _is_valid_subscription_obj(parsed) and _normalize_subject(r[1])==subject_norm:
                                    subs.append(parsed)
                            except Exception:
                                continue
                if subs: return subs
            except Exception:
                app.logger.debug("sheets.list_subscriptions failed")
        # fallback to get_questions reading
        rows = get_questions() or []
        for row in rows:
            try:
                if len(row)>=1 and str(row[0])=="__subscription__":
                    subj=_normalize_subject(row[1] if len(row)>1 else "Other")
                    sub_json=row[3] if len(row)>3 else None
                    if not sub_json: continue
                    parsed=None
                    try: parsed=json.loads(sub_json)
                    except Exception: parsed=None
                    if subj==subject_norm and _is_valid_subscription_obj(parsed):
                        subs.append(parsed)
            except Exception:
                continue
    except Exception:
        app.logger.exception("_load_subscriptions_for_subject failed")
    return subs
# PART 7/10 - NOTIFICATIONS PERSIST + PENDING + ACK + DELETE SUBSCRIPTIONS
def save_notification_for_endpoint(endpoint, subject, title, body, data_obj):
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        for attempt in range(2):
            try:
                _ensure_local_db_tables()
                with sqlite3.connect(str(DB_PATH)) as conn:
                    c=conn.cursor()
                    c.execute("""INSERT INTO notifications(endpoint, subject, title, body, data, created_at, delivered) VALUES (?,?,?,?,?,?,0)""",
                              (endpoint or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
                    nid = c.lastrowid
                    conn.commit()
                return nid
            except sqlite3.OperationalError as oe:
                app.logger.warning("OperationalError saving notification %s", oe)
                try: _init_local_db()
                except Exception: pass
                time.sleep(0.05)
                continue
            except Exception:
                app.logger.exception("save_notification_for_endpoint failed")
                return None
        return None
    except Exception:
        app.logger.exception("save_notification outer failed")
        return None

def fetch_pending_notifications_for_endpoint(endpoint):
    try:
        _ensure_local_db_tables()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c=conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint,))
            rows = c.fetchall()
        out=[]
        for r in rows:
            nid, subj, title, body, data_text, created_at = r
            try: data_obj=json.loads(data_text) if data_text else {}
            except Exception: data_obj={}
            out.append({"id":nid,"subject":subj,"title":title,"body":body,"data":data_obj,"created_at":created_at})
        return out
    except Exception:
        app.logger.exception("fetch_pending_notifications_for_endpoint failed")
        return []

def ack_notifications_by_ids(endpoint, ids):
    if not ids: return 0
    try:
        _ensure_local_db_tables()
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        changed=0
        with sqlite3.connect(str(DB_PATH)) as conn:
            c=conn.cursor()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint))
                    if c.rowcount: changed+=1
                except Exception:
                    continue
            conn.commit()
        return changed
    except Exception:
        app.logger.exception("ack_notifications_by_ids failed")
        return 0

def delete_subscriptions_by_endpoint(endpoint):
    if not endpoint: return
    try:
        norm = normalize_endpoint(endpoint) or ""
        with sqlite3.connect(str(DB_PATH)) as conn:
            c=conn.cursor()
            try:
                c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint_normalized=?", (endpoint, norm))
            except sqlite3.OperationalError:
                try:
                    c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
                except Exception:
                    pass
            conn.commit()
    except Exception:
        app.logger.exception("delete_subscriptions_by_endpoint failed")
# PART 8/10 - SEND PUSH ENGINE + ADMIN ANNOUNCEMENTS
def mark_notifications_delivered_for_endpoint(endpoint):
    if not endpoint: return
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        norm = normalize_endpoint(endpoint) or ""
        for attempt in range(2):
            try:
                _ensure_local_db_tables()
                with sqlite3.connect(str(DB_PATH)) as conn:
                    c=conn.cursor()
                    try:
                        c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE (endpoint=? OR endpoint=?) AND delivered=0", (now, endpoint, norm))
                    except sqlite3.OperationalError:
                        c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint))
                    conn.commit()
                return
            except sqlite3.OperationalError:
                try: _init_local_db()
                except Exception: pass
                time.sleep(0.05)
                continue
    except Exception:
        app.logger.exception("mark_notifications_delivered_for_endpoint failed")

def send_push_to_subject(subject, question_text, attempt_text):
    try:
        subs = _load_subscriptions_for_subject(subject)
    except Exception:
        app.logger.exception("Failed to load subscriptions")
        subs=[]
    if not subs:
        return {"sent":0,"errors":["no subscribers"],"detail":[]}
    title = f"New question in {subject}"
    q_preview = (question_text or "").strip()
    a_preview = (attempt_text or "").strip()
    if len(q_preview)>140: q_preview=q_preview[:137]+"…"
    if len(a_preview)>180: a_preview=a_preview[:177]+"…"
    body = f"Q: {q_preview}\nA: {a_preview}"
    payload_template = {"title":title,"body":body,"url":"/questions","tag":f"subject-{re.sub(r'\\s+','-',subject).lower()}","renotify":True,"subject":subject,"preview":{"question":q_preview,"attempt":a_preview}}
    results=[]; sent=0
    if not PYWEBPUSH_AVAILABLE or not VAPID_PRIVATE_KEY:
        app.logger.warning("pywebpush/VAPID not configured - persisting notifications")
        for sub in subs:
            try:
                endpoint = sub.get("endpoint") if isinstance(sub, dict) else None
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
                results.append({"endpoint":endpoint,"ok":False,"error":"pywebpush-not-configured"})
            except Exception:
                results.append({"endpoint":None,"ok":False,"error":"persist-failed"})
        return {"sent":0,"errors":["pywebpush/VAPID not available"],"detail":results}
    for raw_sub in subs:
        endpoint=None
        try:
            sub = raw_sub
            if isinstance(raw_sub,str):
                try: sub=json.loads(raw_sub)
                except Exception: sub={"endpoint":raw_sub}
            if not _is_valid_subscription_obj(sub):
                results.append({"endpoint":None,"ok":False,"error":"invalid-subscription"})
                continue
            endpoint = sub.get("endpoint")
            if not endpoint:
                results.append({"endpoint":None,"ok":False,"error":"no-endpoint"})
                continue
            try: norm=normalize_endpoint(endpoint)
            except Exception: norm=endpoint
            try:
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
            except Exception:
                app.logger.debug("Could not persist notif for endpoint %s", endpoint)
            payload = dict(payload_template)
            parsed = urlparse(endpoint); domain = parsed.netloc.lower() if parsed.netloc else ""
            if "notify.windows.com" in domain or "wns" in domain or "windows" in domain:
                payload = {"title":payload_template["title"], "body":(q_preview or a_preview)[:80], "url":"/questions"}
            data_bytes = json.dumps(payload).encode("utf-8")
            if len(data_bytes) > 2000:
                payload["preview"]=None
                payload["body"]=(payload.get("body","")[:200]+"…") if payload.get("body") else ""
            attempt=0
            while attempt<2:
                attempt+=1
                try:
                    webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
                    sent+=1
                    results.append({"endpoint":endpoint,"ok":True})
                    try: mark_notifications_delivered_for_endpoint(endpoint)
                    except Exception: pass
                    break
                except WebPushException as ex:
                    resp = getattr(ex, "response", None)
                    status = getattr(resp, "status_code", None) if resp else None
                    body_text=None
                    try:
                        if resp and hasattr(resp,"content"): body_text = resp.content.decode("utf-8", errors="ignore")
                    except Exception: body_text=None
                    err_text=str(ex)
                    app.logger.warning("WebPushException endpoint=%s status=%s err=%s body=%s", endpoint, status, err_text, body_text)
                    if status in (404,410) or (body_text and "unsubscribed" in body_text.lower()) or ("unsubscribed" in err_text.lower()):
                        try: delete_subscriptions_by_endpoint(endpoint)
                        except Exception: app.logger.exception("Failed removing stale endpoint")
                        results.append({"endpoint":endpoint,"ok":False,"status":status,"error":err_text,"body":body_text,"deleted":True})
                        break
                    if status==400 or ("400 Bad Request" in err_text):
                        if attempt<2:
                            time.sleep(0.12)
                            payload={"title":payload_template["title"], "body":(q_preview or a_preview)[:80], "url":"/questions"}
                            continue
                        else:
                            results.append({"endpoint":endpoint,"ok":False,"status":status,"error":err_text,"body":body_text})
                            break
                    if attempt<2:
                        time.sleep(0.12); continue
                    else:
                        results.append({"endpoint":endpoint,"ok":False,"status":status,"error":err_text,"body":body_text}); break
                except Exception as ex:
                    app.logger.exception("Unexpected sending error to %s", endpoint)
                    results.append({"endpoint":endpoint,"ok":False,"error":str(ex)})
                    break
        except Exception:
            app.logger.exception("Error handling subscription %s", endpoint)
            results.append({"endpoint":endpoint,"ok":False,"error":"processing-exception"})
    app.logger.info("send_push_to_subject: sent=%s failed=%s", sent, len([r for r in results if not r.get("ok")]))
    return {"sent":sent,"errors":[r for r in results if not r.get("ok")],"detail":results}

# adminAnnouncement route (broadcast)
@app.route('/adminAnnouncement', methods=['POST'])
def admin_announcement():
    key = (request.args.get('key') or request.form.get('key') or request.headers.get('X-ADMIN-KEY') or '').strip()
    if not ADMIN_KEY or key != ADMIN_KEY:
        return jsonify({"ok":False,"error":"unauthorized"}),401
    payload = request.get_json(force=True, silent=True) or request.form or {}
    subject = (payload.get('subject') or "").strip() or None
    title = payload.get('title') or "Announcement"
    message = payload.get('message') or ""
    url = payload.get('url') or "/"
    try:
        if subject:
            res = send_push_to_subject(subject, title, message)
        else:
            # broadcast to all subscriptions in sqlite fallback
            rows = list_subscriptions_sqlite() or []
            subs=[]
            for r in rows:
                try:
                    if isinstance(r,(list,tuple)) and len(r)>=4 and r[0]=="__subscription__":
                        parsed=None
                        try: parsed=json.loads(r[3])
                        except Exception: parsed=None
                        if parsed: subs.append(parsed)
                except Exception:
                    continue
            sent=0; errors=[]
            payload_template={"title":title,"body":message,"icon":url_for('static', filename='icons/icon-192.svg'),"badge":url_for('static', filename='icons/icon-192.svg'),"url":url}
            for s in subs:
                try:
                    if PYWEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY:
                        webpush(subscription_info=s, data=json.dumps(payload_template), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
                        sent+=1
                    else:
                        save_notification_for_endpoint(s.get("endpoint"), subject or "All", title, message, {"url":url})
                except Exception as e:
                    errors.append({"endpoint": s.get("endpoint"), "error": str(e)})
            res = {"sent": sent, "total": len(subs), "errors": errors}
        return jsonify({"ok":True,"result":res})
    except Exception as e:
        app.logger.exception("adminAnnouncement failed: %s", e)
        return jsonify({"ok":False,"error":str(e)}),500
# PART 9/10 - ROUTES: QUESTIONS PAGE, SUBSCRIPTION ROUTES, PENDING/ACK, DRIVE IMAGE

@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = random.choice([("I can do all things...", "Phil 4:13"), ("Love one another...", "John 13:34")])
    return render_template("index.html", message="Study Hard, Pray Harder ❤❤❤", verse=verse, reference=reference)

@app.route("/questions", methods=["GET","POST"])
def questions_page():
    try:
        if request.method=="POST":
            submitted_question=(request.form.get("question") or "").strip()
            attempt=(request.form.get("attempt") or "").strip()
            if not submitted_question:
                flash("⚠️ Please enter a question.", "warning"); return redirect(url_for("questions_page"))
            if not attempt:
                flash("⚠️ Please enter your solution attempt before submitting.", "warning"); return redirect(url_for("questions_page"))
            username=(request.form.get("name") or "").strip() or "Anonymous"
            choice_selected=(request.form.get("category") or "Other").strip()
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            try:
                append_row([choice_selected, username, submitted_question, attempt, timestamp])
                flash("✅ Question + attempt submitted!", "success")
                try:
                    result = send_push_to_subject(choice_selected, submitted_question, attempt)
                    app.logger.info("send_push_to_subject result: %s", result)
                except Exception:
                    app.logger.exception("send_push_to_subject error")
            except Exception:
                app.logger.exception("append_row failed"); flash("❌ Could not submit question. Try later.", "danger"); return redirect(url_for("questions_page"))
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=COOKIE_MAX_AGE, samesite='Lax'); resp.set_cookie('nn_last_category', choice_selected, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("cookie set failed")
            return resp

        selected_category = request.args.get("categoryFilter","").strip()
        try: all_questions = get_questions() or []
        except Exception:
            app.logger.exception("get_questions failed"); all_questions=[]
        normalized_questions=[]
        for row in all_questions:
            try:
                r=list(row)
                if len(r)<5: r += [""]*(5-len(r))
                normalized_questions.append(r)
            except Exception:
                continue
        normalized_questions = [r for r in normalized_questions if not (len(r)>=1 and str(r[0])=="__subscription__")]
        derived_set = {str(q[0]).strip() for q in normalized_questions if q and len(q)>0 and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]
        if selected_category:
            questions = [q for q in normalized_questions if len(q)>0 and str(q[0]).strip()==selected_category]
        else:
            questions = normalized_questions
        return render_template("questions.html", questions=questions, message=FOOTER_MESSAGE, categories=categories_for_ui, selected_category=selected_category, community_links=COMMUNITY_LINKS, vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception as e:
        app.logger.exception("questions_page failed: %s", e)
        flash("❌ Failed to load questions.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="", community_links=COMMUNITY_LINKS, vapid_public_key=VAPID_PUBLIC_KEY)

@app.route("/save-subscription", methods=["POST"])
def save_subscription_route():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = payload.get("subscription") if isinstance(payload, dict) else payload
        if isinstance(subscription, str):
            try: subscription = json.loads(subscription)
            except Exception: pass
        subject = _normalize_subject((payload.get("subject") if isinstance(payload, dict) else None) or request.args.get("subject") or "Other")
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        if not subscription or not _is_valid_subscription_obj(subscription):
            return jsonify({"ok":False,"error":"subscription required and must include endpoint"}),400
        ok = _save_subscription_to_sheet(subject, client_id, subscription)
        endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
        if ok and endpoint:
            subjects_for_endpoint=[]
            try:
                subs = list_subscriptions_sqlite() if not SHEETS_AVAILABLE else list_subscriptions()
                for r in subs:
                    try:
                        if isinstance(r, dict):
                            ep = None; ssub = r.get("subscription") or r.get("sub") or r
                            if isinstance(ssub, dict): ep=ssub.get("endpoint")
                            subj = r.get("subject") or r.get("subj") or r.get("topic")
                            if ep==endpoint and subj: subjects_for_endpoint.append(_normalize_subject(subj))
                        elif isinstance(r,(list,tuple)) and len(r)>=4 and r[0]=="__subscription__":
                            sub_json=r[3]
                            try: parsed=json.loads(sub_json)
                            except Exception: parsed=None
                            if parsed and parsed.get("endpoint")==endpoint:
                                subj=_normalize_subject(r[1] if len(r)>1 else "Other"); subjects_for_endpoint.append(subj)
                    except Exception:
                        continue
                subjects_for_endpoint = sorted(list(dict.fromkeys(subjects_for_endpoint)))
            except Exception:
                subjects_for_endpoint=[]
            resp = jsonify({"ok":True,"subjects":subjects_for_endpoint})
            try: resp.set_cookie('nn_subscription', endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception: app.logger.debug("cookie write failed")
            return resp,200
        elif ok:
            return jsonify({"ok":True,"subjects":[]}),200
        else:
            return jsonify({"ok":False,"error":"could not save subscription"}),500
    except Exception as e:
        app.logger.exception("save-subscription failed: %s", e)
        return jsonify({"ok":False,"error":str(e)}),500

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint","").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint: return jsonify({"ok":False,"error":"endpoint required"}),400
        out_subjects=[]
        try:
            raw = list_subscriptions_sqlite() if not SHEETS_AVAILABLE else list_subscriptions()
        except Exception:
            raw=[]
        for r in raw:
            try:
                subj=None; sub=None
                if isinstance(r, dict):
                    subj = r.get("subject"); sub = r.get("subscription") or r.get("sub") or r.get("push") or r
                elif isinstance(r,(list,tuple)) and len(r)>=4 and r[0]=="__subscription__":
                    subj = r[1]; try: sub = json.loads(r[3])
                    except Exception: sub=None
                if not sub: continue
                ep = sub.get("endpoint")
                if ep==endpoint:
                    if subj and subj not in out_subjects: out_subjects.append(_normalize_subject(subj))
            except Exception:
                continue
        return jsonify({"ok":True,"subjects":out_subjects})
    except Exception as e:
        app.logger.exception("subscriptions_check failed: %s", e)
        return jsonify({"ok":False,"error":str(e)}),500

@app.route("/subscriptions/delete", methods=["POST"])
def subscriptions_delete():
    try:
        data = request.get_json(force=True, silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip() if isinstance(data, dict) else ""
        if not endpoint: endpoint = (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint: return jsonify({"ok":False,"error":"endpoint required"}),400
        deleted=False
        try:
            delete_subscriptions_by_endpoint(endpoint); deleted=True
        except Exception:
            try:
                rows = get_questions() or []
                indices=[]
                for i,row in enumerate(rows, start=1):
                    try:
                        if len(row)>=4 and str(row[0])=="__subscription__":
                            try: sub=json.loads(row[3])
                            except Exception: sub=None
                            if sub and sub.get("endpoint")==endpoint: indices.append(i)
                    except Exception: continue
                if indices:
                    delete_rows_by_indices(indices); deleted=True
            except Exception:
                app.logger.exception("fallback deletion failed")
        resp = jsonify({"ok":True,"deleted":deleted})
        if deleted:
            try: resp.set_cookie('nn_subscription','',expires=0,samesite='Lax')
            except Exception: pass
        return resp
    except Exception as e:
        app.logger.exception("subscriptions_delete failed: %s", e)
        return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/vapid", methods=["GET"])
def api_vapid():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY or ""}),200

@app.route('/notifications/pending', methods=['GET'])
def notifications_pending():
    try:
        endpoint = request.args.get('endpoint','').strip() or request.cookies.get('nn_subscription','').strip()
        if not endpoint: return jsonify({'ok':False,'error':'endpoint required'}),400
        items=[]
        try: items = fetch_pending_notifications_for_endpoint(endpoint)
        except Exception:
            app.logger.exception("fetch_pending_notifications failed")
        return jsonify({'ok':True,'items':items}),200
    except Exception as e:
        app.logger.exception("notifications_pending failed: %s", e)
        return jsonify({'ok':False,'error':str(e)}),500

@app.route('/notifications/ack', methods=['POST'])
def notifications_ack():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        endpoint = (payload.get('endpoint') or '').strip() or request.cookies.get('nn_subscription','').strip()
        ids = payload.get('ids') or []
        if not endpoint: return jsonify({'ok':False,'error':'endpoint required'}),400
        if not isinstance(ids, list): return jsonify({'ok':False,'error':'ids must be a list'}),400
        try: ack_notifications_by_ids(endpoint, ids)
        except Exception: app.logger.exception("ack failed")
        return jsonify({'ok':True,'count':len(ids)}),200
    except Exception as e:
        app.logger.exception("notifications_ack failed: %s", e)
        return jsonify({'ok':False,'error':str(e)}),500

# drive image route (robust)
@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    try:
        fid = file_id
        if fid.startswith("http"):
            m = re.search(r"(?:/d/|id=)([-_0-9A-Za-z]+)", fid)
            if m: fid = m.group(1)
            else: fid = fid.rstrip("/").split("/")[-1]
        drive_url = f"https://drive.google.com/uc?export=download&id={fid}"
        if requests:
            try:
                r = requests.get(drive_url, stream=True, timeout=10)
                content_type = r.headers.get("content-type","")
                if r.status_code==200 and content_type and content_type.startswith("image/"):
                    return Response(r.content, mimetype=content_type)
            except Exception:
                app.logger.exception("drive image fetch failed")
        return redirect(drive_url)
    except Exception as e:
        app.logger.exception("drive image fallback: %s", e)
        try: return send_file("static/img/fallback.jpg", mimetype="image/jpeg")
        except Exception:
            small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                                   b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                                   b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!"
                                   b"\xbc\x33\x00\x00\x00\x00IEND\xaeB`\x82")
            small_png.seek(0)
            return send_file(small_png, mimetype="image/png")
# PART 10/10 - ADMIN ROUTES, CODING PAGE, HEALTH, FAVICON, SERVICE WORKER, ERROR HANDLER, ENTRY

@app.route("/rules")
def rules_page():
    try: rules = read_rules() or []
    except Exception: rules=[]
    GROUP_COMMUNITY_KEY = os.environ.get("GROUP_COMMUNITY_KEY")
    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE, GROUP_COMMUNITY_KEY=GROUP_COMMUNITY_KEY)

@app.route("/admin", methods=["GET","POST"])
def admin_page():
    result=None
    if request.method=="POST":
        provided_secret = (request.form.get("secret") or "").strip()
        if provided_secret == os.environ.get("ADMIN_CLEAR_SECRET"):
            try: clear_sheet(); result="✅ Questions cleared."
            except Exception as e: result=f"❌ Failed: {e}"
        else: result="❌ Invalid secret!"
    return render_template("admin.html", result=result, message=FOOTER_MESSAGE)

@app.route("/admin/questions", methods=["GET","POST"])
def admin_questions():
    provided_key=(request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or "").strip()
    if request.method=="GET" and not provided_key:
        return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        if request.method=="GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
        return "Unauthorized - include ?key=ADMIN_KEY", 401
    raw = get_questions() or []
    questions=[]
    for idx,row in enumerate(raw, start=1):
        try:
            r=list(row)
            if len(r)<5: r+=[""]*(5-len(r))
            questions.append({"index":idx,"subject":r[0],"user":r[1],"text":r[2],"attempt":r[3],"time":r[4]})
        except Exception: continue
    if request.method=="POST":
        action = request.form.get("action")
        if action=="delete_selected":
            selected = request.form.getlist("selected")
            indices=[]
            for i in selected:
                try: indices.append(int(i))
                except Exception: continue
            try: delete_rows_by_indices(indices); flash("✅ Selected rows deleted.", "success")
            except Exception: app.logger.exception("delete_rows failed"); flash("⚠️ Could not delete selected rows.", "warning")
        elif action=="delete_subject":
            subject = request.form.get("subject_to_delete")
            if subject:
                try: delete_rows_by_subject(subject); flash(f"✅ Deleted questions for subject {subject}.", "success")
                except Exception: flash("⚠️ delete_rows_by_subject failed.", "warning")
        return redirect(url_for("admin_questions") + f"?key={provided_key}")
    subjects = sorted({q["subject"] for q in questions if q["subject"]})
    return render_template("admin_questions.html", questions=questions, subjects=subjects, key=provided_key, message=FOOTER_MESSAGE)

@app.route("/coding")
def coding_page():
    drive_id = request.args.get("drive_id"); local_file = request.args.get("file")
    try:
        exercises = load_coding_exercises(drive_id=drive_id, local_path=local_file)
        return render_template("coding.html", exercises=exercises, message=FOOTER_MESSAGE)
    except Exception as e:
        app.logger.exception("coding_page render failed: %s", e)
        flash("Failed to load coding page resources. Check server logs.", "warning")
        return render_template("coding.html", exercises=[], message=FOOTER_MESSAGE)

@app.route("/admin/exercises", methods=["GET","POST"])
def admin_exercises():
    provided_key=(request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or "").strip()
    if request.method=="GET" and not provided_key:
        return render_template("admin_exercises.html", key="", message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)
    if provided_key != EXERCISES_ADMIN_KEY:
        if request.method=="GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_exercises.html", key="", message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)
        return "Unauthorized", 401
    if request.method=="POST":
        ex = {"id": (request.form.get("id") or "").strip(), "title": (request.form.get("title") or "").strip(), "language": (request.form.get("language") or "js").strip(), "difficulty": (request.form.get("difficulty") or "").strip(), "description": (request.form.get("description") or "").strip()}
        starter = request.form.get("starter_code") or ""; solution = request.form.get("solution_code") or ""
        if ex["language"]=="sql":
            ex["schema_sql"] = starter; ex["example_query"] = solution
            sample_json = request.form.get("sample_data") or ""
            try: ex["sample_data"] = json.loads(sample_json) if sample_json.strip() else {}
            except Exception: flash("Sample data JSON invalid", "warning"); ex["sample_data"] = {}
        else:
            if ex["language"]=="js": ex["starter_js"]=starter; ex["solution_js"]=solution
            elif ex["language"]=="html": ex["starter_html"]=starter; ex["solution_html"]=solution
        if not ex["id"]:
            flash("Exercise id required", "warning"); return redirect(url_for("admin_exercises")+f"?key={provided_key}")
        target = request.form.get("target") or "local"
        drive_override = (request.form.get("drive_file_id") or "").strip() or None
        drive_file_to_use = drive_override or CODING_DRIVE_FILE_ID
        with _coding_lock:
            local_list=[]
            p = Path(CODING_LOCAL_PATH)
            if p.exists():
                try:
                    with p.open("r", encoding="utf-8") as fh: local_list = json.load(fh)
                except Exception: app.logger.exception("read local exercises failed")
            ids=[e.get("id") for e in local_list if isinstance(e, dict)]
            if ex["id"] in ids:
                for i,e2 in enumerate(local_list):
                    if e2.get("id")==ex["id"]: local_list[i] = {**e2, **ex}; break
                op="updated"
            else:
                local_list.append(ex); op="created"
            saved_local=False; saved_drive=False
            try:
                tmp = p.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as fh: json.dump(local_list, fh, ensure_ascii=False, indent=2)
                tmp.replace(p); saved_local=True
            except Exception:
                app.logger.exception("save local exercises failed")
            if target=="drive" and drive_file_to_use and DRIVE_AVAILABLE:
                try:
                    raw = download_file(drive_file_to_use)
                    if raw:
                        try: drive_list=json.loads(raw.decode("utf-8"))
                        except Exception: drive_list=local_list
                    else: drive_list = local_list
                    ids_drive = {e.get("id"): e for e in drive_list if isinstance(e, dict)}
                    ids_drive[ex["id"]] = {**ids_drive.get(ex["id"],{}), **ex}
                    merged = list(ids_drive.values())
                    if update_file(drive_file_to_use, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")): saved_drive=True
                except Exception:
                    app.logger.exception("drive update failed")
            if target=="drive" and saved_drive: flash(f"✅ Exercise {ex['id']} {op} and pushed to Drive.", "success")
            elif saved_local: flash(f"✅ Exercise {ex['id']} {op} and saved locally.", "success")
            else: flash("❌ Could not save exercise", "danger")
        return redirect(url_for("admin_exercises") + f"?key={provided_key}")

# health and quizzes
def _load_health_data():
    try:
        path = Path("data/health_data.json")
        if path.exists():
            with path.open("r", encoding="utf-8") as fh: return json.load(fh)
    except Exception:
        app.logger.debug("health data load failed")
    return [{"slug":"aloe-vera","name":"Aloe vera","virtues":["skin-soothing"],"uses":"Topical","notes":""}]

@app.route("/health")
def health_index():
    try: items=_load_health_data(); return render_template("health.html", plants=items, message=FOOTER_MESSAGE)
    except Exception as e: app.logger.exception("health_index failed"); flash("Failed to load", "danger"); return redirect(url_for("home_page"))

@app.route("/health/<slug>")
def health_detail(slug):
    try:
        items=_load_health_data()
        for p in items:
            if p.get("slug")==slug: return render_template("health_detail.html", plant=p, message=FOOTER_MESSAGE)
        flash("Not found", "warning"); return redirect(url_for("health_index"))
    except Exception:
        app.logger.exception("health detail failed"); flash("Failed", "danger"); return redirect(url_for("health_index"))

# favicon route (prevent 404 spam)
@app.route('/favicon.ico')
def favicon():
    try:
        return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')
    except Exception:
        return "", 204

# service worker route (static/service-worker.js must exist)
@app.route('/service-worker.js')
def service_worker():
    try:
        return send_from_directory(app.static_folder, 'service-worker.js')
    except Exception:
        return "", 404

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    try:
        return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500
    except Exception:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1","true","yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)