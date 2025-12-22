# =========================
# app.py  — Part A/3
# Core imports, configuration, helpers, sheets/drive fallbacks
# =========================

import os
import re
import json
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from contextlib import closing
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_from_directory, make_response, flash, Response
)
from dotenv import load_dotenv

load_dotenv()

# ---------- Config ----------
BASE_DIR = Path(__file__).parent
LOCAL_QUESTIONS_JSON = BASE_DIR / "questions.json"
LOCAL_CODING_JSON = BASE_DIR / "coding_exercises.json"
FALLBACK_DB = BASE_DIR / "nn_local.db"

# Primary integrations: set keys via env
QUESTIONS_SHEET_KEY = os.environ.get("QUESTIONS_SHEET_KEY", "").strip()
TIMETABLE_SHEET_KEY = os.environ.get("TIMETABLE_SHEET_KEY", "").strip()
GROUP_RULES_SHEET_KEY = os.environ.get("GROUP_RULES_SHEET_KEY", "").strip()
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))

# Flask app
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"

# ---------- Try optional sheets/drive helpers ----------
SHEETS_AVAILABLE = False
DRIVE_AVAILABLE = False
try:
    # If you have your own sheets.py implement: append_row, get_questions, from_google_sheets, read_rules
    from sheets import append_row as sheets_append_row, get_questions as sheets_get_questions, from_google_sheets as sheets_from_google_sheets, read_rules as sheets_read_rules
    SHEETS_AVAILABLE = True
except Exception as e:
    app.logger.debug("sheets.py not available: %s", e)
    SHEETS_AVAILABLE = False

try:
    # If you have drive_loader.py implement: load_questions_from_drive, load_coding_from_drive, download_file etc.
    from drive_loader import load_questions_from_drive, load_coding_from_drive, download_file as drive_download_file
    DRIVE_AVAILABLE = True
except Exception as e:
    app.logger.debug("drive_loader.py not available: %s", e)
    DRIVE_AVAILABLE = False

# ---------- Ensure fallback sqlite for local persistence ----------
def _init_local_db():
    try:
        FALLBACK_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(FALLBACK_DB))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS questions_local (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                username TEXT,
                question TEXT,
                attempt TEXT,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        app.logger.exception("_init_local_db failed")

_init_local_db()

def get_fallback_conn():
    conn = sqlite3.connect(str(FALLBACK_DB))
    conn.row_factory = sqlite3.Row
    return conn

# ---------- Local JSON helpers ----------
def _read_local_json(path: Path):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        app.logger.exception("Reading local json failed: %s", path)
    return []

def _write_local_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        app.logger.exception("Writing local json failed: %s", path)
    return False

# ---------- Questions persistence (Sheets primary, local fallback) ----------
def append_row(row, key=None):
    """
    row: [subject, username, question, attempt, timestamp]
    Prefers Sheets if available (and key supplied). Falls back to local JSON + sqlite.
    """
    try:
        if not isinstance(row, (list, tuple)):
            raise ValueError("append_row expects list/tuple")
        while len(row) < 5:
            row.append("")
        if SHEETS_AVAILABLE and (key or QUESTIONS_SHEET_KEY):
            try:
                sheets_append_row(row, key=(key or QUESTIONS_SHEET_KEY))
                return True
            except Exception:
                app.logger.exception("sheets_append_row failed; falling back to local storage")

        # fallback: local JSON
        entries = _read_local_json(LOCAL_QUESTIONS_JSON)
        entry = {
            "subject": str(row[0] or "Other"),
            "username": str(row[1] or "Anonymous"),
            "question": str(row[2] or ""),
            "attempt": str(row[3] or ""),
            "created_at": str(row[4] or datetime.utcnow().replace(microsecond=0).isoformat())
        }
        entries.append(entry)
        _write_local_json(LOCAL_QUESTIONS_JSON, entries)

        # also insert into sqlite fallback table
        try:
            with get_fallback_conn() as conn:
                c = conn.cursor()
                c.execute("INSERT INTO questions_local (subject, username, question, attempt, created_at) VALUES (?,?,?,?,?)",
                          (entry['subject'], entry['username'], entry['question'], entry['attempt'], entry['created_at']))
                conn.commit()
        except Exception:
            app.logger.exception("sqlite fallback insert failed")
        return True
    except Exception:
        app.logger.exception("append_row failed")
        return False

def get_questions():
    """
    Prefer sheets; else combine local JSON + sqlite fallback rows.
    Returns list of dicts with keys: subject, username, question, attempt, created_at
    """
    try:
        if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
            try:
                rows = sheets_get_questions(key=QUESTIONS_SHEET_KEY) if callable(sheets_get_questions) else sheets_get_questions()
                out = []
                for r in rows:
                    try:
                        lr = list(r)
                        while len(lr) < 5:
                            lr.append("")
                        out.append({"subject": lr[0], "username": lr[1], "question": lr[2], "attempt": lr[3], "created_at": lr[4]})
                    except Exception:
                        continue
                return out
            except Exception:
                app.logger.exception("sheets_get_questions failed; using local fallback")

        # local JSON
        out = _read_local_json(LOCAL_QUESTIONS_JSON) or []
        # append sqlite fallback rows
        try:
            with get_fallback_conn() as conn:
                c = conn.cursor()
                for r in c.execute("SELECT subject, username, question, attempt, created_at FROM questions_local ORDER BY id ASC"):
                    out.append({"subject": r[0], "username": r[1], "question": r[2], "attempt": r[3], "created_at": r[4]})
        except Exception:
            app.logger.debug("reading sqlite fallback rows failed or none exist")
        return out
    except Exception:
        app.logger.exception("get_questions failed")
        return []

# ---------- Timetable helper (robust column detection) ----------
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
    dt = _try_parse_time(value)
    if dt:
        return dt.strftime("%b %d %I:%M %p").lstrip("0")
    return str(value or "")

# make available in jinja
app.jinja_env.filters["format_time"] = format_time

def get_timetable_today():
    """
    Uses sheets_from_google_sheets() if available; else empty list.
    Detects columns smartly and returns list of dict entries for today's rows.
    """
    try:
        rows = []
        if SHEETS_AVAILABLE:
            try:
                rows = sheets_from_google_sheets() if callable(sheets_from_google_sheets) else sheets_from_google_sheets()
            except Exception:
                app.logger.exception("sheets_from_google_sheets failed")
        if not rows or len(rows) < 2:
            return []

        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]

        aliases = {
            "day": ["day", "weekday"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor", "staff"],
            "link": ["link", "location", "meeting", "url", "meet"],
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
                if not toks:
                    continue
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
            col_day = col_day if col_day is not None else 0
            col_time = col_time if col_time is not None else 1
            col_subject = col_subject if col_subject is not None else 2
            col_facilitator = col_facilitator if col_facilitator is not None else 3
            col_link = col_link if col_link is not None else 4
            col_notes = col_notes if col_notes is not None else 5
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
# =========================
# app.py  — Part B/3
# Page routes, questions handling, admin helpers
# =========================

# Context for templates
@app.context_processor
def inject_globals():
    return {"message": FOOTER_MESSAGE}

@app.route("/")
@app.route("/home")
def home_page():
    verse = "Study Hard, Pray Harder ❤❤❤"
    return render_template("index.html", message=FOOTER_MESSAGE, verse=verse)

@app.route("/content")
def content_page():
    try:
        ws_values = []
        if SHEETS_AVAILABLE:
            try:
                ws_values = sheets_from_google_sheets() if callable(sheets_from_google_sheets) else []
            except Exception:
                app.logger.exception("sheets_from_google_sheets failed")
        form_list = []
        dr_values = []
        try:
            # Drive listing if available
            if DRIVE_AVAILABLE:
                try:
                    dr_values = []  # replace with drive listing call if you have one
                except Exception:
                    app.logger.debug("drive listing failed")
        except Exception:
            pass

        timetable_today = get_timetable_today()
        # images fallback from static/img
        images = dr_values
        if not images:
            img_dir = BASE_DIR / "static" / "img"
            if img_dir.exists():
                images = [str(p.name) for p in sorted(img_dir.iterdir()) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif")]
        return render_template("content.html", images=images, sheet_content=ws_values[1:] if len(ws_values) > 1 else [], sheet_title=ws_values[0] if ws_values else [], timetable_today=timetable_today)
    except Exception as e:
        app.logger.exception("content_page error: %s", e)
        return render_template("content.html", images=[], sheet_content=[], sheet_title=[], timetable_today=[], error=str(e))

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
        # if drive_download_file available, use it
        if DRIVE_AVAILABLE and 'drive_download_file' in globals():
            try:
                raw = drive_download_file(fid)
                if raw:
                    return Response(raw, mimetype="image/jpeg")
            except Exception:
                app.logger.exception("drive_download_file failed for %s", fid)
        # fallback: redirect to google drive viewers
        drive_url = f"https://drive.google.com/uc?export=download&id={fid}"
        return redirect(drive_url)
    except Exception as e:
        app.logger.exception("drive_image failed: %s", e)
        return send_from_directory(app.static_folder, "img/fallback.jpg")

# -------------------------
# Questions page (GET: list, POST: submit)
# -------------------------
@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            attempt = (request.form.get("attempt") or "").strip()
            if not submitted_question or not attempt:
                flash("Please enter both question and your attempt.", "warning")
                return redirect(url_for("questions_page"))
            username = (request.form.get("name") or "").strip() or "Anonymous"
            choice_selected = (request.form.get("category") or "Other").strip()
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            append_row([choice_selected, username, submitted_question, attempt, timestamp])
            flash("Question submitted. Thanks!", "success")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=365*24*60*60, samesite='Lax')
                resp.set_cookie('nn_last_category', choice_selected, max_age=365*24*60*60, samesite='Lax')
            except Exception:
                app.logger.debug("Failed setting cookies")
            return resp

        selected_category = request.args.get("categoryFilter", "").strip()
        all_questions = get_questions() or []
        # normalized list -> keep as list of dicts for template convenience
        if selected_category:
            questions = [q for q in all_questions if str(q.get("subject","")).strip() == selected_category]
        else:
            questions = all_questions

        # build categories list (static + dynamic)
        STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
        derived = sorted({str(q.get("subject") or "").strip() for q in all_questions if q and q.get("subject")})
        categories_for_ui = STATIC_CATEGORIES + [c for c in derived if c and c not in STATIC_CATEGORIES]

        return render_template("questions.html", questions=questions, message=FOOTER_MESSAGE, categories=categories_for_ui, selected_category=selected_category)
    except Exception as e:
        app.logger.exception("Questions page error: %s\n%s", e, traceback.format_exc())
        flash("Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=[], selected_category="")

# Fallback endpoint so url_for('subscriptions_check') won't crash templates that still reference it
@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    return jsonify({"ok": False, "subjects": [], "message": "subscriptions not enabled in this build"})

# -------------------------
# Rules / Admin
# -------------------------
@app.route("/rules")
def rules_page():
    try:
        rules = []
        if SHEETS_AVAILABLE and GROUP_RULES_SHEET_KEY:
            try:
                rules = sheets_read_rules()
            except Exception:
                app.logger.exception("sheets_read_rules failed")
        return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)
    except Exception as e:
        app.logger.exception("rules_page failed: %s", e)
        return render_template("rules.html", rules=[], message=FOOTER_MESSAGE, error=str(e))

@app.route("/admin/clear_local_questions", methods=["POST"])
def admin_clear_local_questions():
    try:
        # clear local json
        _write_local_json(LOCAL_QUESTIONS_JSON, [])
        # clear sqlite fallback
        with get_fallback_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM questions_local")
            conn.commit()
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("admin_clear_local_questions failed")
        return jsonify({"ok": False}), 500

# -------------------------
# Coding routes (admin) - simple loader
# -------------------------
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    drive_id = (drive_id or os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "")).strip() or None
    local_path = (local_path or str(LOCAL_CODING_JSON)).strip() or None

    if drive_id and DRIVE_AVAILABLE:
        try:
            data = load_coding_from_drive(drive_id)
            if isinstance(data, list):
                return data
        except Exception:
            app.logger.exception("load_coding_from_drive failed; falling back")

    if local_path:
        p = Path(local_path)
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
            except Exception:
                app.logger.exception("Failed reading local coding_exercises.json")
    # fallback sample
    return [
        {"id": "sample_sql", "title": "Sample SQL", "language": "sql", "difficulty": "Easy",
         "schema_sql": "CREATE TABLE Products (product_id INT PRIMARY KEY, name TEXT);",
         "example_query": "SELECT * FROM Products;", "sample_data": {"Products": [{"product_id": 101, "name": "Apple"}]}}
    ]

@app.route("/coding")
def coding_page():
    try:
        drive_id = request.args.get("drive_id")
        file_path = request.args.get("file")
        exercises = load_coding_exercises(drive_id=drive_id, local_path=file_path)
        return render_template("coding.html", exercises=exercises, message=FOOTER_MESSAGE)
    except Exception:
        app.logger.exception("coding_page failed")
        flash("Failed to load coding page resources.", "warning")
        return render_template("coding.html", exercises=[], message=FOOTER_MESSAGE)

@app.route("/admin/exercises", methods=["GET", "POST"])
def admin_exercises():
    # simplified: this route will allow local edits only (not drive push in this simplified edition)
    if request.method == "POST":
        # expect form fields; save to local JSON
        ex = {
            "id": (request.form.get("id") or "").strip(),
            "title": (request.form.get("title") or "").strip(),
            "language": (request.form.get("language") or "js").strip(),
            "difficulty": (request.form.get("difficulty") or "").strip(),
            "description": (request.form.get("description") or "").strip()
        }
        if ex["language"] == "sql":
            ex["schema_sql"] = request.form.get("starter_code") or ""
            ex["example_query"] = request.form.get("solution_code") or ""
            try:
                sample_json = request.form.get("sample_data") or ""
                ex["sample_data"] = json.loads(sample_json) if sample_json.strip() else {}
            except Exception:
                ex["sample_data"] = {}
        if not ex["id"]:
            flash("Exercise id required", "warning")
            return redirect(url_for("admin_exercises"))
        # save locally
        arr = _read_local_json(LOCAL_CODING_JSON) or []
        existing_ids = [e.get("id") for e in arr if isinstance(e, dict)]
        if ex["id"] in existing_ids:
            for i, e in enumerate(arr):
                if e.get("id") == ex["id"]:
                    arr[i] = {**e, **ex}
                    break
            op = "updated"
        else:
            arr.append(ex); op = "created"
        _write_local_json(LOCAL_CODING_JSON, arr)
        flash(f"Exercise {ex['id']} {op}", "success")
        return redirect(url_for("admin_exercises"))
    # GET: show local list
    exercises = _read_local_json(LOCAL_CODING_JSON) or []
    return render_template("admin_exercises.html", exercises=exercises, message=FOOTER_MESSAGE)
# =========================
# app.py  — Part C/3
# SQL executor (multi-statement safe), exercise table route, error handlers, run
# =========================

# ---------- SQL safety helpers ----------
ALLOWED_SQL_KEYWORDS = [
    'select','insert','update','delete','create','drop','alter','where','join','union','group','having','order','limit','values'
]
BANNED_SQL = ['attach', 'pragma', 'vacuum', 'load_extension', 'restore']

def is_sql_allowed(sql_text: str) -> bool:
    if not sql_text or not isinstance(sql_text, str):
        return False
    lower = sql_text.lower()
    for b in BANNED_SQL:
        if b in lower:
            return False
    return any(k in lower for k in ALLOWED_SQL_KEYWORDS)

# robust splitter that attempts to avoid splitting inside quotes
split_statements_re = re.compile(r";\s*(?=(?:[^'\"\\\\]|'[^'\\\\]*'|\"[^\"\\\\]*\")*$)")

def _fix_create_if_not_exists(sql_text: str) -> str:
    try:
        return re.sub(r"(CREATE\s+TABLE)\s+([`\"'\[\]\w]+)\s+IF\s+NOT\s+EXISTS", r"\1 IF NOT EXISTS \2", sql_text, flags=re.I)
    except Exception:
        return sql_text

def _normalize_insert(stmt: str) -> str:
    try:
        return re.sub(r"^\s*(INSERT\s+INTO)\b", 'INSERT OR REPLACE INTO', stmt, flags=re.I)
    except Exception:
        return stmt

# ---------- Multi-statement executor (in-memory DB) ----------
def execute_multi_sql(sql_text: str):
    """
    Executes given multi-statement SQL in a fresh in-memory sqlite DB (so student runs are isolated).
    SELECT/ WITH -> executed and results returned.
    Non-SELECT -> executed via executescript; INSERT -> automatically converted to INSERT OR REPLACE.
    Returns dict: { ok: True, results: [ {statement:, type:'select'|'ok', columns:, rows:} | {statement:, error:...} ] }
    """
    if not is_sql_allowed(sql_text):
        return {"ok": False, "error": "SQL contains banned keywords or is not allowed."}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sql_text = _fix_create_if_not_exists(sql_text)
    statements = [s.strip() for s in split_statements_re.split(sql_text) if s.strip()]
    out = []
    for i, stmt in enumerate(statements, start=1):
        try:
            first_tok = (stmt.split()[0].upper() if stmt and len(stmt.split())>0 else "")
            if first_tok in ("SELECT", "WITH"):
                try:
                    cur.execute(stmt)
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description] if cur.description else []
                    out.append({"statement": i, "type": "select", "columns": cols, "rows": [dict(r) for r in rows]})
                except Exception as e:
                    out.append({"statement": i, "error": str(e)})
            else:
                stmt_mod = _normalize_insert(stmt)
                try:
                    cur.executescript(stmt_mod + (";" if not stmt_mod.strip().endswith(";") else ""))
                    conn.commit()
                    out.append({"statement": i, "type": "ok", "message": "executed"})
                except Exception as e:
                    conn.rollback()
                    out.append({"statement": i, "error": str(e)})
        except Exception as e:
            out.append({"statement": i, "error": str(e)})
    conn.close()
    return {"ok": True, "results": out}

# ---------- Exercise table route: builds DB from exercise schema/sample_data and runs provided SQL ----------
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

        # prepare in-memory DB and populate schema & sample data
        try:
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if schema_sql:
                try:
                    cur.executescript(_fix_create_if_not_exists(schema_sql) + ("\n" if not schema_sql.strip().endswith(";") else ""))
                except Exception as e:
                    app.logger.exception("Schema exec failed: %s", e)
            # populate sample_data dict: {table: [ {col:val}, ... ] }
            for table, rows in (sample_data.items() if isinstance(sample_data, dict) else []):
                if not isinstance(rows, list):
                    continue
                # collect columns
                cols = []
                for r in rows:
                    if isinstance(r, dict):
                        for k in r.keys():
                            if k not in cols:
                                cols.append(k)
                if not cols:
                    continue
                # create table if missing
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                    if not cur.fetchone():
                        cols_def = ", ".join([f'\"{c}\" TEXT' for c in cols])
                        cur.execute(f'CREATE TABLE \"{table}\" ({cols_def});')
                except Exception:
                    pass
                placeholders = ", ".join(["?"]*len(cols))
                col_list_sql = ", ".join([f'\"{c}\"' for c in cols])
                insert_sql = f'INSERT OR REPLACE INTO \"{table}\" ({col_list_sql}) VALUES ({placeholders})'
                for r in rows:
                    try:
                        vals = [str(r.get(c)) if r.get(c) is not None else None for c in cols]
                        cur.execute(insert_sql, vals)
                    except Exception:
                        continue
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"ok": False, "error": f"Failed preparing sample DB: {e}"}), 500

        # Get SQL from POST body (JSON) or if none, return HTML for existing tables
        payload = {}
        if request.method == "POST":
            try:
                payload = request.get_json(force=True, silent=True) or {}
            except Exception:
                payload = {}
        sql_from_client = (payload.get("sql") or "").strip()

        if not sql_from_client:
            # show existing tables
            try:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
                tables = [r[0] for r in cur.fetchall()]
            except Exception:
                tables = []
            html_parts = []
            if not tables:
                conn.close()
                return jsonify({"ok": True, "html": '<div class="small text-muted">No sample tables available for this exercise.</div>'})
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

        # Execute client SQL (multi-statement)
        results = []
        statements = [s.strip() for s in split_statements_re.split(sql_from_client) if s.strip()]
        for i, stmt in enumerate(statements, start=1):
            try:
                stmt_upper = stmt.strip().split()[0].upper() if stmt.strip() else ""
                if stmt_upper in ("SELECT", "WITH"):
                    try:
                        cur.execute(stmt)
                        rows = cur.fetchall()
                        cols = [d[0] for d in cur.description] if cur.description else []
                        # generate html table for this result
                        html = []
                        html.append(f'<div class="mb-2"><strong>Result (SELECT #{i})</strong></div>')
                        if not cols:
                            html.append('<div class="small text-muted">No columns returned.</div>')
                        else:
                            html.append('<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>')
                            for c in cols:
                                html.append(f'<th>{c}</th>')
                            html.append('</tr></thead><tbody>')
                            if not rows:
                                html.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
                            else:
                                for r in rows:
                                    html.append('<tr>')
                                    for c in cols:
                                        try:
                                            v = r[c]
                                        except Exception:
                                            v = None
                                        html.append('<td>%s</td>' % ("" if v is None else str(v)))
                                    html.append('</tr>')
                            html.append('</tbody></table></div>')
                        results.append({"statement": i, "html": "\n".join(html)})
                    except Exception as qerr:
                        results.append({"statement": i, "error": str(qerr)})
                else:
                    try:
                        cur_stmt = _normalize_insert(stmt)
                        cur.executescript(cur_stmt + (";" if not cur_stmt.strip().endswith(";") else ""))
                        conn.commit()
                        results.append({"statement": i, "ok": True, "message": "executed"})
                    except Exception as nonqerr:
                        conn.rollback()
                        results.append({"statement": i, "error": str(nonqerr)})
            except Exception as e:
                results.append({"statement": i, "error": str(e)})

        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            existing_tables = [r[0] for r in cur.fetchall()]
            if existing_tables:
                results.append({"tables": existing_tables})
        except Exception:
            pass

        conn.close()
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        app.logger.exception("exercise_table_route failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------
# Error handlers
# -------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    try:
        return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500
    except Exception:
        return f"Server error: {e}", 500

# -------------------------
# App entry
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)