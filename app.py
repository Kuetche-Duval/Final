# app.py — Part 1/7
import os
import sys
import json
import base64
import logging
import sqlite3
import traceback
from pathlib import Path
from datetime import datetime
from threading import Lock

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, flash,
    send_from_directory, Response, make_response
)
# Use markupsafe for Markup (Flask removed direct export in newer versions)
try:
    from markupsafe import Markup
except Exception:
    def Markup(s): return s

# optional requests for Drive fetch server-side
try:
    import requests
except Exception:
    requests = None

# environment / dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("newtons-nerds")

# App setup
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

# Paths & DB
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("NN_LOCAL_DB") or str(BASE_DIR / "nn_local.db")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", str(BASE_DIR / "service_account.json"))
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")

# Write service account if base64 provided
if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64.strip())
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
        logger.info("Wrote service account JSON to %s", SERVICE_ACCOUNT_PATH)
    except Exception:
        logger.exception("Failed writing service account JSON")

# Configs
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", "500"))
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "").strip()
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", os.environ.get("ADMIN_KEY", ""))

# Sheets/Drive keys (main storage)
QUESTIONS_SHEET_KEY = os.environ.get("QUESTIONS_SHEET_KEY", "")
TIMETABLE_SHEET_KEY = os.environ.get("TIMETABLE_SHEET_KEY", "")
GROUP_RULES_SHEET_KEY = os.environ.get("GROUP_RULES_SHEET_KEY", "")
SUBSCRIPTION_SHEET_KEY = os.environ.get("QUESTIONS_SUBSCRIPTION_KEY", "")

# Minimal UI constants
FOOTER_MESSAGE = "Love one another as I have loved you ❤❤❤"
STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
MOTIVATION_VERSES = [
    ("I can do all things through Christ who strengthens me.", "Philippians 4:13"),
    ("Trust in the Lord with all your heart and lean not on your own understanding.", "Proverbs 3:5"),
]

# Ensure the fallback SQLite DB and minimal tables exist (idempotent)
def _init_local_db():
    try:
        dbp = Path(DB_PATH)
        if not dbp.parent.exists():
            try:
                dbp.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        conn = sqlite3.connect(str(dbp))
        c = conn.cursor()
        # common fallback tables
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS coding_exercises (
                id TEXT PRIMARY KEY,
                data_json TEXT,
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sql_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sql_text TEXT,
                executed_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Local SQLite DB ensured at %s", dbp)
    except Exception:
        logger.exception("Failed initializing local DB")

# Initialize at startup (safe)
_init_local_db()
# app.py — Part 2/7
# Attempt to import your sheets.py and drive.py helpers (they should exist in repo).
# If not, provide reasonable fallbacks that keep the app running.
SHEETS_AVAILABLE = False
DRIVE_AVAILABLE = False
try:
    from sheets import (
        from_google_sheets, check_date, append_row, read_rules, get_questions,
        clear_sheet, delete_rows_by_indices, delete_rows_by_subject,
        save_subscription, list_subscriptions, delete_subscriptions_by_endpoint
    )
    SHEETS_AVAILABLE = True
except Exception as e:
    logger.warning("sheets.py import failed: %s — falling back to minimal local functions", e)

    def from_google_sheets(*a, **k):
        return []

    def check_date(*a, **k):
        return []

    def append_row(row, key=None):
        # fallback: write to local sqlite
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            while len(row) < 5:
                row.append("")
            cur.execute("INSERT INTO questions(subject, username, question, attempt, created_at) VALUES (?,?,?,?,?)",
                        (row[0], row[1], row[2], row[3], row[4] or timestamp))
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("append_row fallback failed")

    def read_rules(*a, **k):
        return []

    def get_questions(*a, **k):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT subject, username, question, attempt, created_at FROM questions ORDER BY id DESC")
            rows = cur.fetchall()
            conn.close()
            out = []
            for r in rows:
                out.append(list(r))
            return out
        except Exception:
            logger.exception("get_questions fallback failed")
            return []

    def clear_sheet(*a, **k):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("DELETE FROM questions")
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("clear_sheet fallback failed")

    def delete_rows_by_indices(indices):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            for i in sorted(set(int(x) for x in indices), reverse=True):
                cur.execute("DELETE FROM questions WHERE id = ?", (i,))
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("delete_rows_by_indices fallback failed")

    def delete_rows_by_subject(subject):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("DELETE FROM questions WHERE subject = ?", (subject,))
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("delete_rows_by_subject fallback failed")

    def save_subscription(*a, **k):
        logger.info("save_subscription called but sheets not available (noop)")

    def list_subscriptions(*a, **k):
        return []

    def delete_subscriptions_by_endpoint(*a, **k):
        return

try:
    from drive import from_google_drive, folders_and_images, download_file, update_file, create_file_in_folder
    DRIVE_AVAILABLE = True
except Exception as e:
    logger.warning("drive.py import failed: %s — using simple drive fallbacks", e)

    def from_google_drive(*a, **k):
        return []

    def folders_and_images(*a, **k):
        return []

    def download_file(*a, **k):
        return b""

    def update_file(*a, **k):
        return False

    def create_file_in_folder(*a, **k):
        return None

# Helper: serve drive image via direct uc download or redirect if requests installed
def drive_image_url(file_id):
    if not file_id:
        return None
    fid = file_id.strip()
    if fid.startswith("http"):
        # guess ID
        import re
        m = re.search(r"(?:/d/|id=)([-_0-9A-Za-z]+)", fid)
        if m:
            fid = m.group(1)
        else:
            fid = fid.rstrip("/").split("/")[-1]
    return f"https://drive.google.com/uc?export=download&id={fid}"

@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    try:
        fid = file_id
        # If requests available, try to proxy for CORS-free delivery
        if requests:
            try:
                url = drive_image_url(fid)
                r = requests.get(url, timeout=8)
                content_type = r.headers.get("content-type", "")
                if r.status_code == 200 and content_type and content_type.startswith("image/"):
                    return Response(r.content, mimetype=content_type)
            except Exception:
                logger.exception("Failed fetching drive image via requests")
        # fallback: redirect to Drive URL (works if file is shareable)
        return redirect(drive_image_url(fid) or url_for("static", filename="img/fallback.jpg"))
    except Exception:
        logger.exception("drive_image route error")
        try:
            return send_from_directory("static", "img/fallback.jpg")
        except Exception:
            return Response("", status=404)
# app.py — Part 3/7
# Inject common context into templates
@app.context_processor
def inject_globals():
    return {
        "message": FOOTER_MESSAGE,
        "static_categories": STATIC_CATEGORIES,
    }

# Jinja filter: friendly time formatting
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
    return None

def format_time(value):
    dt = _try_parse_time(value)
    if dt:
        return dt.strftime("%b %d %I:%M %p").lstrip("0")
    return str(value) if value else ""

app.jinja_env.filters["format_time"] = format_time

# Timetable helper: robust column detection, uses Sheets if available
def get_timetable_today():
    try:
        rows = from_google_sheets() or []
        if not rows or len(rows) < 2:
            return []
        # header detection
        header = [str(h).strip() for h in rows[0]]
        header_lower = [h.lower() for h in header]
        aliases = {
            "day": ["day", "weekday"],
            "time": ["time", "hour", "period"],
            "subject": ["subject", "course", "topic"],
            "facilitator": ["facilitator", "teacher", "instructor"],
            "link": ["link", "location", "url", "meet"],
            "notes": ["notes", "note", "remarks", "comments"]
        }
        def find_col(alias_list):
            for a in alias_list:
                if a in header_lower:
                    return header_lower.index(a)
            for a in alias_list:
                for i,h in enumerate(header_lower):
                    if a in h:
                        return i
            return None
        col_day = find_col(aliases["day"])
        col_time = find_col(aliases["time"])
        col_subject = find_col(aliases["subject"])
        col_facilitator = find_col(aliases["facilitator"])
        col_link = find_col(aliases["link"])
        col_notes = find_col(aliases["notes"])
        # fallback to positions
        hl = len(header)
        if col_day is None and hl > 0: col_day = 0
        if col_time is None and hl > 1: col_time = 1
        today_short = datetime.now().strftime("%a").upper()
        today_full = datetime.now().strftime("%A").upper()
        matches = []
        for idx, r in enumerate(rows[1:], start=2):
            try:
                day_cell = str(r[col_day]).strip() if col_day is not None and col_day < len(r) else str(r[0]).strip() if r else ""
                if not day_cell: continue
                first = day_cell.upper()
                if first.startswith(today_short) or today_full in first:
                    entry = {
                        "row_index": idx,
                        "day": day_cell,
                        "time": (r[col_time] if col_time is not None and col_time < len(r) else ""),
                        "subject": (r[col_subject] if col_subject is not None and col_subject < len(r) else ""),
                        "facilitator": (r[col_facilitator] if col_facilitator is not None and col_facilitator < len(r) else ""),
                        "link": (r[col_link] if col_link is not None and col_link < len(r) else ""),
                        "notes": (r[col_notes] if col_notes is not None and col_notes < len(r) else "")
                    }
                    matches.append(entry)
            except Exception:
                continue
        return matches
    except Exception:
        logger.exception("get_timetable_today failed")
        return []
# app.py — Part 4/7
@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = MOTIVATION_VERSES[0]
    return render_template("index.html", message=FOOTER_MESSAGE, verse=verse, reference=reference)

@app.route("/content")
def content_page():
    try:
        ws_values = from_google_sheets() or []
        ws_title = ws_values[0] if ws_values else []
        ws_content = ws_values[1:] if len(ws_values) > 1 else []
        dr_values = folders_and_images() if DRIVE_AVAILABLE else []
        form_list = from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form") if DRIVE_AVAILABLE else []
        timetable_today = get_timetable_today()
        return render_template("content.html",
                               message=FOOTER_MESSAGE,
                               sheet_title=ws_title,
                               sheet_content=ws_content,
                               form_list=form_list,
                               dr_values=dr_values,
                               timetable_today=timetable_today)
    except Exception:
        logger.exception("Content page error")
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_title=[], sheet_content=[], form_list=[], dr_values=[], timetable_today=[])

@app.route("/rules")
def rules_page():
    try:
        rules = read_rules() or []
    except Exception:
        rules = []
    GROUP_COMMUNITY_KEY = os.environ.get("GROUP_COMMUNITY_KEY", "")
    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE, GROUP_COMMUNITY_KEY=GROUP_COMMUNITY_KEY)

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
            except Exception:
                logger.exception("Failed append_row")
                flash("❌ Could not submit question. Try later.", "danger")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie('nn_user', username, max_age=365*24*60*60, samesite='Lax')
                resp.set_cookie('nn_last_category', choice_selected, max_age=365*24*60*60, samesite='Lax')
            except Exception:
                pass
            return resp

        selected_category = request.args.get("categoryFilter", "").strip()
        try:
            all_questions = get_questions() or []
        except Exception:
            logger.exception("get_questions failed")
            all_questions = []

        normalized_questions = []
        for row in all_questions:
            try:
                r = list(row)
                while len(r) < 5:
                    r.append("")
                normalized_questions.append(r)
            except Exception:
                continue

        # Filter out any subscription rows in fallback sheets (if using same sheet)
        normalized_questions = [r for r in normalized_questions if not (len(r) >= 1 and str(r[0]) == "__subscription__")]

        derived_set = {str(q[0]).strip() for q in normalized_questions if q and len(q) > 0 and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]

        if selected_category:
            questions = [q for q in normalized_questions if len(q) > 0 and str(q[0]).strip() == selected_category]
        else:
            questions = normalized_questions

        return render_template("questions.html",
                               questions=questions,
                               message=FOOTER_MESSAGE,
                               categories=categories_for_ui,
                               selected_category=selected_category,
                               community_links={},
                               vapid_public_key="")
    except Exception:
        logger.exception("Questions page error")
        flash("❌ Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="", community_links={}, vapid_public_key="")
# app.py — Part 5/7
# Coding exercises loader (Drive -> local JSON -> fallback hardcoded)
def load_coding_exercises(drive_id: str = None, local_path: str = None):
    drive_id = (drive_id or CODING_DRIVE_FILE_ID or "").strip() or None
    local_path = (local_path or CODING_LOCAL_PATH or "").strip() or None

    # 1) Drive
    if drive_id and DRIVE_AVAILABLE:
        try:
            raw = download_file(drive_id)
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, list):
                        return data
                except Exception:
                    logger.exception("Failed parse coding exercises from Drive file")
        except Exception:
            logger.exception("Failed to download coding exercises from Drive")

    # 2) Local file
    if local_path:
        try:
            p = Path(local_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        return data
        except Exception:
            logger.exception("Failed to read local coding_exercises.json")

    # 3) SQLite fallback store
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT data_json FROM coding_exercises")
        rows = cur.fetchall()
        conn.close()
        if rows:
            out = []
            for r in rows:
                try:
                    out.append(json.loads(r[0]))
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        logger.exception("SQLite load_coding_exercises fallback failed")

    # 4) final fallback
    logger.info("Using fallback coding exercises")
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

@app.route("/coding")
def coding_page():
    drive_id = request.args.get("drive_id")
    local_file = request.args.get("file")
    try:
        exercises = load_coding_exercises(drive_id=drive_id, local_path=local_file)
        return render_template("coding.html", exercises=exercises, message=FOOTER_MESSAGE)
    except Exception:
        logger.exception("coding_page render failed")
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

        # Save locally (sqlite) and optionally to Drive
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            cur.execute("REPLACE INTO coding_exercises(id, data_json, created_at) VALUES (?,?,?)",
                        (ex["id"], json.dumps(ex, ensure_ascii=False), now))
            conn.commit()
            conn.close()
            saved_local = True
        except Exception:
            saved_local = False
            logger.exception("Failed saving to local sqlite coding_exercises")

        # Optionally update Drive file if requested
        target = request.form.get("target") or "local"
        drive_file_to_use = (request.form.get("drive_file_id") or "").strip() or CODING_DRIVE_FILE_ID
        saved_drive = False
        if target == "drive" and drive_file_to_use and DRIVE_AVAILABLE:
            try:
                raw = download_file(drive_file_to_use)
                if raw:
                    try:
                        drive_list = json.loads(raw.decode("utf-8"))
                    except Exception:
                        drive_list = []
                else:
                    drive_list = []
                # merge / replace by id
                ids = {e.get("id"): e for e in drive_list if isinstance(e, dict)}
                ids[ex["id"]] = ex
                merged = list(ids.values())
                if update_file(drive_file_to_use, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")):
                    saved_drive = True
            except Exception:
                logger.exception("Drive update failed")
        if saved_drive:
            flash(f"✅ Exercise {ex['id']} saved to Drive.", "success")
        elif saved_local:
            flash(f"✅ Exercise {ex['id']} saved locally.", "success")
        else:
            flash("❌ Could not save exercise (check server logs & permissions).", "danger")
        return redirect(url_for("admin_exercises") + f"?key={provided_key}")

    # GET
    return render_template("admin_exercises.html", key=provided_key, message=FOOTER_MESSAGE, drive_file_id=CODING_DRIVE_FILE_ID)
# app.py — Part 6/7
# SQL visualizer helpers & route (multi-statement support)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    """
    Build an in-memory sqlite DB from admin-provided schema_sql and sample_data.
    Uses executescript for multi-statement schema. Returns (conn, populated_table_names).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    populated = []
    # run schema with executescript to allow multi-statements
    if schema_sql:
        try:
            # small fixups: some users put IF NOT EXISTS in different order — we leave as-is but use executescript
            cur.executescript(schema_sql)
            conn.commit()
        except Exception as e:
            # schema may partially fail; log and continue
            logger.debug("Schema execution had errors: %s", e)

    # populate sample_data
    try:
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
                    # if table exists, record it
                    try:
                        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                        if cur.fetchone():
                            populated.append(table)
                        continue
                    except Exception:
                        continue
                # create table if not exists
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
    except Exception:
        logger.exception("Failed populating sample data")
    return conn, populated

@app.route("/exercise-table/<exercise_id>", methods=["GET", "POST"])
def exercise_table_route(exercise_id):
    """
    Build an in-memory DB from exercise.schema_sql and sample_data,
    then execute client SQL. Supports multi-statement (executescript for non-select).
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

        payload = {}
        if request.method == "POST":
            try:
                payload = request.get_json(force=True, silent=True) or {}
            except Exception:
                payload = {}
        sql_from_client = (payload.get("sql") or "").strip()

        # If no SQL provided, show sample tables
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

        # Execute multi-statement SQL client provided
        cur = conn.cursor()
        # split by semicolon while keeping statements intact for SELECTs with semicolons possible in strings — simple splitting is used
        import re
        statements = [s.strip() for s in re.split(r';\s*(?=(?:[^\'"]|\'[^\']*\'|"[^"]*")*$)', sql_from_client) if s.strip()]
        html_out = []
        for i, stmt in enumerate(statements, start=1):
            try:
                stmt_upper = (stmt.strip().split()[0].upper() if stmt.strip() else "")
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
                    # Non-select: use executescript to allow multi-statements when user provided one chunk
                    try:
                        cur.executescript(stmt + ";")
                        conn.commit()
                        affected = cur.rowcount if hasattr(cur, "rowcount") else "unknown"
                        html_out.append(f'<div class="small text-muted">Statement #{i} executed. Affected rows: {affected}.</div>')
                    except Exception as nonqerr:
                        conn.rollback()
                        html_out.append(f'<div class="alert alert-warning small">Statement #{i} failed: {nonqerr}</div>')
            except Exception as e:
                html_out.append(f'<div class="alert alert-danger small">Execution error on statement #{i}: {e}</div>')

        # List current tables
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            existing_tables = [r[0] for r in cur.fetchall()]
            if existing_tables:
                html_out.append("<div class='mt-3'><strong>Current tables:</strong></div>")
                for tbl in existing_tables:
                    html_out.append(f'<div class="small-muted">- {tbl}</div>')
        except Exception:
            pass

        # persist a log (best-effort)
        try:
            conn2 = sqlite3.connect(str(DB_PATH))
            cur2 = conn2.cursor()
            cur2.execute("INSERT INTO sql_logs(sql_text, executed_at) VALUES (?,?)", (sql_from_client, datetime.utcnow().isoformat()))
            conn2.commit()
            conn2.close()
        except Exception:
            logger.debug("Failed writing sql log to local DB (non-fatal)")

        conn.close()
        return jsonify({"ok": True, "html": "\n".join(html_out)})
    except Exception:
        logger.exception("exercise_table_route failed")
        return jsonify({"ok": False, "error": "server error"}), 500
# app.py — Part 7/7
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

@app.route("/quizzes")
def quiz_forms():
    try:
        path = Path("data/quiz_forms.json")
        forms = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                forms = json.load(fh)
        return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)
    except Exception:
        logger.exception("Forms index failed")
        return render_template("quiz_forms.html", forms=[], message=FOOTER_MESSAGE)

@app.route('/service-worker.js')
def service_worker():
    # serve static service worker if present
    return send_from_directory(app.static_folder, 'service-worker.js')

# Health pages
@app.route("/health")
def health_index():
    try:
        path = Path("data/health_data.json")
        items = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                items = json.load(fh)
        return render_template("health.html", plants=items, message=FOOTER_MESSAGE)
    except Exception:
        logger.exception("health_index failed")
        flash("Failed to load health advice page.", "danger")
        return redirect(url_for("home_page"))

# Global error handler
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

# Entrypoint
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)