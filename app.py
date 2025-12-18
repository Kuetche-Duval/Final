# app.py — PART 1 of 7
"""
Main Flask app (Sheets primary, SQLite fallback).
Paste parts 1..7 in this order into a single app.py file.
"""
import os
import json
import logging
import sqlite3
import traceback
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from threading import Lock

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify,
    flash, Response, send_from_directory, g
)

# Optional 3rd-party packages that may not exist in all environments:
try:
    import requests
except Exception:
    requests = None

# Optional Google/drive/sheets modules (your repo should include sheets.py and optionally drive.py)
try:
    import sheets as sheets_mod
    SHEETS_AVAILABLE = True
except Exception as e:
    sheets_mod = None
    SHEETS_AVAILABLE = False

try:
    import drive as drive_mod
    DRIVE_AVAILABLE = True
except Exception:
    drive_mod = None
    DRIVE_AVAILABLE = False

# -------------------------
# Basic configuration
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_DEFAULT = os.environ.get("NN_LOCAL_DB") or str(BASE_DIR / "nn_local.db")
DB_PATH = os.environ.get("DB_PATH", DB_DEFAULT)
SERVICE_ACCOUNT_JSON_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_JSON_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# logging
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# Constants
FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"
STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"
]
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE", 365 * 24 * 60 * 60))

# concurrency lock for DB writes
_db_lock = Lock()

# Ensure service account is present if BASE uses it (optionally decode B64)
if SERVICE_ACCOUNT_JSON_B64:
    try:
        import base64
        data = base64.b64decode(SERVICE_ACCOUNT_JSON_B64.strip())
        with open(SERVICE_ACCOUNT_JSON_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_JSON_PATH, 0o600)
        except Exception:
            pass
        app.logger.info("Wrote service account JSON to %s", SERVICE_ACCOUNT_JSON_PATH)
    except Exception:
        app.logger.exception("Failed to decode SERVICE_ACCOUNT_JSON_B64")

# --- Ensure DB exists with required tables (SQLite fallback)
def _init_local_db():
    try:
        dbp = Path(DB_PATH)
        if not dbp.parent.exists():
            dbp.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        # simple questions table (fallback)
        c.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                username TEXT,
                question TEXT,
                attempt TEXT,
                created_at TEXT
            )
        """)
        # optional local meta/log tables
        c.execute("""
            CREATE TABLE IF NOT EXISTS sql_lab_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sql_text TEXT,
                executed_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        app.logger.info("Local SQLite DB ensured at %s", DB_PATH)
    except Exception:
        app.logger.exception("Failed initializing local DB at %s", DB_PATH)

# create DB at startup
_init_local_db()
# app.py — PART 2 of 7
# Database connection helpers (Flask g)
def get_db_conn():
    if "nn_db_conn" not in g:
        conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        g.nn_db_conn = conn
    return g.nn_db_conn

@app.teardown_appcontext
def close_db_conn(exc=None):
    conn = g.pop("nn_db_conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass

# Sheets wrappers — prefer Google Sheets (sheets_mod) but fallback to local sqlite where appropriate
def append_row_to_storage(row, questions_key=None):
    """
    Append a row. Prefer sheets_mod.append_row (Google Sheets).
    Row: [category, username, question, attempt, timestamp]
    """
    try:
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "append_row"):
            try:
                sheets_mod.append_row(row, key=questions_key)
                return True
            except Exception:
                app.logger.exception("sheets_mod.append_row failed — falling back to sqlite")
        # fallback: sqlite
        with _db_lock:
            conn = get_db_conn()
            cur = conn.cursor()
            subject = str(row[0]) if row and len(row) > 0 else "Other"
            username = row[1] if len(row) > 1 else "Anonymous"
            question = row[2] if len(row) > 2 else ""
            attempt = row[3] if len(row) > 3 else ""
            timestamp = row[4] if len(row) > 4 else datetime.utcnow().replace(microsecond=0).isoformat()
            cur.execute("INSERT INTO questions (subject, username, question, attempt, created_at) VALUES (?, ?, ?, ?, ?)",
                        (subject, username, question, attempt, timestamp))
            conn.commit()
        return True
    except Exception:
        app.logger.exception("append_row_to_storage failed")
        return False

def get_questions_from_storage(questions_key=None):
    """
    Returns list of normalized rows [subject, username, question, attempt, timestamp].
    Primary: sheets_mod.get_questions(); fallback: sqlite.
    """
    try:
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "get_questions"):
            try:
                rows = sheets_mod.get_questions()
                # ensure normalization (pad to 5 columns)
                out = []
                for r in rows:
                    r = list(r)
                    while len(r) < 5:
                        r.append("")
                    out.append(r)
                return out
            except Exception:
                app.logger.exception("sheets_mod.get_questions failed — falling back to sqlite")
        # fallback sqlite
        with _db_lock:
            conn = get_db_conn()
            cur = conn.cursor()
            cur.execute("SELECT subject, username, question, attempt, created_at FROM questions ORDER BY id DESC")
            rows = cur.fetchall()
            out = []
            for r in rows:
                out.append([r["subject"], r["username"], r["question"], r["attempt"], r["created_at"]])
            return out
    except Exception:
        app.logger.exception("get_questions_from_storage failed")
        return []

# Delete rows helpers (admin actions)
def delete_rows_by_indices_storage(indices, questions_key=None):
    """
    Prefer sheets_mod.delete_rows_by_indices if available; fallback to sqlite by deleting ids.
    indices: list of 1-based indices (sheet indexing) OR list of integer ids for sqlite fallback.
    """
    try:
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "delete_rows_by_indices"):
            try:
                sheets_mod.delete_rows_by_indices(indices)
                return True
            except Exception:
                app.logger.exception("sheets_mod.delete_rows_by_indices failed — falling back to sqlite")
        # fallback sqlite: interpret indices as ids
        with _db_lock:
            conn = get_db_conn()
            cur = conn.cursor()
            ids = [int(i) for i in indices if str(i).strip().isdigit()]
            for idv in ids:
                try:
                    cur.execute("DELETE FROM questions WHERE id=?", (idv,))
                except Exception:
                    continue
            conn.commit()
        return True
    except Exception:
        app.logger.exception("delete_rows_by_indices_storage failed")
        return False

def delete_rows_by_subject_storage(subject, questions_key=None):
    try:
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "delete_rows_by_subject"):
            try:
                sheets_mod.delete_rows_by_subject(subject)
                return True
            except Exception:
                app.logger.exception("sheets_mod.delete_rows_by_subject failed — falling back to sqlite")
        # fallback sqlite
        with _db_lock:
            conn = get_db_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM questions WHERE subject=?", (subject,))
            conn.commit()
        return True
    except Exception:
        app.logger.exception("delete_rows_by_subject_storage failed")
        return False
# app.py — PART 3 of 7
# Jinja context & helpers
@app.context_processor
def inject_globals():
    # expose community links and footer message to templates
    community_links = {}
    try:
        community_links = json.loads(os.environ.get("COMMUNITY_LINKS_JSON", "{}") or "{}")
    except Exception:
        community_links = {}
    return {
        "community_links": community_links,
        "message": FOOTER_MESSAGE
    }

# jinja filter: friendly time formatting
def _try_parse_time(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    text = str(s).strip()
    fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%H:%M:%S", "%H:%M", "%I:%M %p"]
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    # fallback: try to read YYYY-MM-DD
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None

def format_time(value):
    dt = _try_parse_time(value)
    if dt:
        return dt.strftime("%b %d %I:%M %p").lstrip("0")
    return str(value or "")

app.jinja_env.filters["format_time"] = format_time

# Timetable helper (prefer sheets_mod.from_google_sheets)
def get_timetable_today():
    """
    Return list of entries for today's day. Uses sheets_mod.from_google_sheets() if available.
    Each entry returned as dict with keys: row_index, day, time, subject, facilitator, link, notes
    """
    try:
        rows = []
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "from_google_sheets"):
            try:
                rows = sheets_mod.from_google_sheets() or []
            except Exception:
                app.logger.exception("sheets_mod.from_google_sheets failed")
                rows = []
        else:
            app.logger.debug("Sheets not available — get_timetable_today returns []")
            return []

        if not rows or len(rows) < 2:
            return []

        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]

        # alias detection (robust)
        aliases = {
            "day": ["day", "weekday"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor"],
            "link": ["link", "location", "url", "meet"],
            "notes": ["notes", "remarks", "comments"]
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
            return None

        col_day = find_col(aliases["day"])
        col_time = find_col(aliases["time"])
        col_subject = find_col(aliases["subject"])
        col_facilitator = find_col(aliases["facilitator"])
        col_link = find_col(aliases["link"])
        col_notes = find_col(aliases["notes"])

        # safe fallback indices
        hl = len(header)
        if hl >= 6:
            col_day = 0 if col_day is None else col_day
            col_time = 1 if col_time is None else col_time
            col_subject = 2 if col_subject is None else col_subject
            col_facilitator = 3 if col_facilitator is None else col_facilitator
            col_link = 4 if col_link is None else col_link
            col_notes = 5 if col_notes is None else col_notes

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
                if first.startswith(today_short) or today_full in first or first.startswith(today_full[:3]):
                    def get_cell(ci):
                        if ci is None or ci >= len(rlist):
                            return ""
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
# app.py — PART 4 of 7
# Drive image route (robust)
@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    """
    Serve or redirect to a Google Drive image.
    If drive_mod has a direct access function, use it; otherwise redirect to drive.uc endpoint.
    """
    try:
        fid = file_id
        if fid.startswith("http"):
            # try to extract id
            import re
            m = re.search(r"(?:/d/|id=)([-_0-9A-Za-z]+)", fid)
            if m:
                fid = m.group(1)
            else:
                fid = fid.rstrip("/").split("/")[-1]
        # If drive_mod offers a proxy, use it
        if DRIVE_AVAILABLE and hasattr(drive_mod, "download_file"):
            try:
                raw = drive_mod.download_file(fid)
                if raw:
                    # try to sniff content-type from filename ext
                    ext = str(fid).split(".")[-1].lower()
                    ct = "image/png"
                    if ext in ("jpg", "jpeg"): ct = "image/jpeg"
                    if ext in ("webp",): ct = "image/webp"
                    return Response(raw, mimetype=ct)
            except Exception:
                app.logger.debug("drive_mod.download_file failed for %s", fid)
        # fallback: redirect to drive's public download link
        drive_url = f"https://drive.google.com/uc?export=download&id={fid}"
        # Optionally proxy via requests if running server-side and want to avoid CORS issues
        if requests:
            try:
                r = requests.get(drive_url, stream=True, timeout=8)
                content_type = r.headers.get("content-type", "")
                if r.status_code == 200 and content_type and content_type.startswith("image/"):
                    return Response(r.content, mimetype=content_type)
            except Exception:
                app.logger.debug("drive_image proxy failed, redirecting")
        return redirect(drive_url)
    except Exception:
        app.logger.exception("drive_image failed for %s", file_id)
        # try to serve a fallback static image
        try:
            return send_from_directory(app.static_folder, "img/fallback.jpg")
        except Exception:
            return "", 404

# Content page
@app.route("/content")
def content_page():
    """
    Show content, including images. Primary source: drive_mod.folders_and_images() (if available).
    Fallback: list static/img directory.
    """
    try:
        timetable_today = get_timetable_today()
        dr_values = []
        try:
            if DRIVE_AVAILABLE and hasattr(drive_mod, "folders_and_images"):
                try:
                    dr_values = drive_mod.folders_and_images()
                except Exception:
                    app.logger.exception("drive_mod.folders_and_images failed")
                    dr_values = []
            else:
                # fallback: list local static images
                img_dir = Path(app.static_folder) / "img"
                if img_dir.exists():
                    for p in sorted(img_dir.iterdir()):
                        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
                            dr_values.append({"type": "static", "url": url_for("static", filename=f"img/{p.name}"), "name": p.name})
        except Exception:
            app.logger.exception("Failed preparing drive/static image list")
        # forms listing via drive (optional)
        form_list = []
        try:
            if DRIVE_AVAILABLE and hasattr(drive_mod, "from_google_drive"):
                form_list = drive_mod.from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form")
        except Exception:
            form_list = []
        return render_template("content.html", message=FOOTER_MESSAGE, dr_values=dr_values, timetable_today=timetable_today, form_list=form_list)
    except Exception:
        app.logger.exception("Content page render failed")
        return render_template("content.html", message=FOOTER_MESSAGE, dr_values=[], timetable_today=[], form_list=[])
# app.py — PART 5 of 7
# Questions page (submit + list), Sheets primary, SQLite fallback
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
            row = [choice_selected, username, submitted_question, attempt, timestamp]
            ok = append_row_to_storage(row)
            if ok:
                flash("✅ Question + attempt submitted!", "success")
            else:
                flash("❌ Could not submit question. Try later.", "danger")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=COOKIE_MAX_AGE, samesite='Lax')
                resp.set_cookie('nn_last_category', choice_selected, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                app.logger.debug("cookie set failed")
            return resp

        # GET — fetch questions and render
        all_questions = get_questions_from_storage()
        # remove possible subscription rows that older sheets might include (rows starting with "__subscription__")
        normalized = []
        for r in all_questions:
            try:
                if len(r) > 0 and str(r[0]) == "__subscription__":
                    continue
                normalized.append(r)
            except Exception:
                continue
        derived_set = {str(q[0]).strip() for q in normalized if q and len(q) > 0 and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "").strip()
        if selected_category:
            questions = [q for q in normalized if len(q) > 0 and str(q[0]).strip() == selected_category]
        else:
            questions = normalized
        # community links from env or sheets
        community_links = {}
        try:
            community_links = json.loads(os.environ.get("COMMUNITY_LINKS_JSON", "{}") or "{}")
        except Exception:
            community_links = {}
        return render_template("questions.html", questions=questions, message=FOOTER_MESSAGE, categories=categories_for_ui, selected_category=selected_category, community_links=community_links)
    except Exception:
        app.logger.exception("Questions page error: %s", traceback.format_exc())
        flash("❌ Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="", community_links={})
# app.py — PART 6 of 7
# SQL exercise table / lab with multi-statement support
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    """
    Load coding exercises config from local JSON or Drive (prefer Drive if available).
    Each exercise is a dict. For SQL exercises expect keys:
        id, language='sql', schema_sql, sample_data (dict)
    """
    drive_id = (drive_id or os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID") or "").strip() or None
    local_path = (local_path or os.environ.get("CODING_EXERCISES_PATH") or "coding_exercises.json").strip() or None
    if drive_id and DRIVE_AVAILABLE and hasattr(drive_mod, "download_file"):
        try:
            raw = drive_mod.download_file(drive_id)
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, list):
                        return data
                except Exception:
                    app.logger.exception("Failed parse exercises JSON from Drive")
        except Exception:
            app.logger.exception("drive download failed for exercises")
    if local_path:
        try:
            p = Path(local_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
        except Exception:
            app.logger.exception("Failed read local coding_exercises.json")
    # fallback: empty list
    return []

from flask import Markup

@app.route("/exercise-table/<exercise_id>", methods=["GET", "POST"])
def exercise_table_route(exercise_id):
    """
    Build an in-memory SQLite DB from schema_sql + sample_data, then execute SQL provided by client.
    Multi-statement SQL allowed (executescript used for DDL/DML).
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

        # create in-memory DB and populate
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if schema_sql:
            try:
                cur.executescript(schema_sql)
            except Exception:
                # try naive splitting
                for s in [s.strip() for s in schema_sql.split(";") if s.strip()]:
                    try:
                        cur.execute(s)
                    except Exception:
                        continue

        # populate sample_data
        if isinstance(sample_data, dict):
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
                    # if table exists, skip
                    try:
                        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                        if cur.fetchone():
                            pass
                    except Exception:
                        pass
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
        conn.commit()

        payload = {}
        if request.method == "POST":
            payload = request.get_json(force=True, silent=True) or {}
        sql_from_client = (payload.get("sql") or "").strip()

        # If no SQL provided — return current tables and up to SQL_TABLE_ROW_LIMIT rows
        if not sql_from_client:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            tables = [r[0] for r in cur.fetchall()]
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

        # Execute provided SQL — support multi-statement (executescript for non-SELECT, execute for SELECT)
        statements = [s.strip() for s in sql_from_client.split(";") if s.strip()]
        html_out = []
        for i, stmt in enumerate(statements, start=1):
            try:
                first_tok = stmt.strip().split()[0].upper() if stmt.strip() else ""
            except Exception:
                first_tok = ""
            if first_tok in ("SELECT", "WITH"):
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
                    # executescript to allow multiple DDL/DML statements merged
                    conn.executescript(stmt + (";" if not stmt.strip().endswith(";") else ""))
                    conn.commit()
                    affected = "unknown"
                    html_out.append(f'<div class="small text-muted">Statement #{i} executed. Affected rows: {affected}.</div>')
                except Exception as nonqerr:
                    conn.rollback()
                    html_out.append(f'<div class="alert alert-warning small">Statement #{i} failed: {nonqerr}</div>')
        # show existing tables
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
    except Exception:
        app.logger.exception("exercise_table_route failed: %s", traceback.format_exc())
        return jsonify({"ok": False, "error": "server error"}), 500
# app.py — PART 7 of 7
# Admin pages and other utilities

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    result = None
    if request.method == "POST":
        provided_secret = (request.form.get("secret") or "").strip()
        if provided_secret == os.environ.get("ADMIN_CLEAR_SECRET"):
            # Clear sheet via sheets_mod if available else sqlite
            try:
                if SHEETS_AVAILABLE and hasattr(sheets_mod, "clear_sheet"):
                    sheets_mod.clear_sheet()
                else:
                    with _db_lock:
                        conn = get_db_conn()
                        cur = conn.cursor()
                        cur.execute("DELETE FROM questions")
                        conn.commit()
                result = "✅ Questions cleared."
            except Exception as e:
                app.logger.exception("admin clear failed")
                result = f"❌ Failed: {e}"
        else:
            result = "❌ Invalid secret!"
    return render_template("admin.html", result=result, message=FOOTER_MESSAGE)


@app.route("/admin/questions", methods=["GET", "POST"])
def admin_questions():
    provided_key = (request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or "").strip()
    if request.method == "GET" and not provided_key:
        return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
    if provided_key != os.environ.get("ADMIN_CLEAR_SECRET"):
        if request.method == "GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
        return "Unauthorized - include ?key=ADMIN_KEY or enter it into the Admin Key box on the page.", 401

    raw = get_questions_from_storage()
    questions = []
    for idx, row in enumerate(raw, start=1):
        try:
            r = list(row)
            while len(r) < 5:
                r.append("")
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
                delete_rows_by_indices_storage(indices)
                flash("✅ Selected rows deleted.", "success")
            except Exception as e:
                app.logger.exception("delete_rows_by_indices failed")
                flash("⚠️ Could not delete selected rows (server error).", "warning")
        elif action == "delete_subject":
            subject = request.form.get("subject_to_delete")
            if subject:
                try:
                    delete_rows_by_subject_storage(subject)
                    flash(f"✅ Deleted questions for subject {subject}.", "success")
                except Exception:
                    flash("⚠️ delete_rows_by_subject failed on server.", "warning")
        return redirect(url_for("admin_questions") + f"?key={provided_key}")

    subjects = sorted({q["subject"] for q in questions if q["subject"]})
    return render_template("admin_questions.html", questions=questions, subjects=subjects, key=provided_key, message=FOOTER_MESSAGE)


# Rules, quizzes, health pages (simple wrappers)
@app.route("/rules")
def rules_page():
    try:
        rules = []
        if SHEETS_AVAILABLE and hasattr(sheets_mod, "read_rules"):
            rules = sheets_mod.read_rules() or []
        return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)
    except Exception:
        app.logger.exception("rules_page failed")
        return render_template("rules.html", rules=[], message=FOOTER_MESSAGE)


@app.route("/quizzes")
def quiz_forms():
    try:
        forms = []
        path = Path("data/quiz_forms.json")
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                forms = json.load(fh)
        return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)
    except Exception:
        app.logger.exception("quiz_forms failed")
        return render_template("quiz_forms.html", forms=[], message=FOOTER_MESSAGE)


@app.route("/health")
def health_index():
    try:
        items = []
        path = Path("data/health_data.json")
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                items = json.load(fh)
        return render_template("health.html", plants=items, message=FOOTER_MESSAGE)
    except Exception:
        app.logger.exception("health_index failed")
        flash("Failed to load health advice page.", "danger")
        return redirect(url_for("content_page"))


# Global error handler
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    try:
        return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500
    except Exception:
        return jsonify({"error": str(e)}), 500

# App entry
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
