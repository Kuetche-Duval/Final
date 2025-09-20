# app.py  -- final revised
import os
import base64
import json
import random
import logging
import traceback
import re
import io
from datetime import datetime
from pathlib import Path
from threading import Lock
from flask import (
    Flask, render_template, url_for, redirect, send_file,
    request, flash, Response, jsonify, send_from_directory
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
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# --- Sheets/Drive helpers (import if available) ---
try:
    from sheets import (
        from_google_sheets, check_date, append_row, read_rules, get_questions,
        clear_sheet, delete_rows_by_indices, delete_rows_by_subject,
        save_subscription, list_subscriptions
    )
    SHEETS_AVAILABLE = True
except Exception as e:
    app.logger.warning("sheets.py import failed: %s — using fallbacks", e)
    SHEETS_AVAILABLE = False

    # minimal fallbacks to keep the app running (no sheets)
    def from_google_sheets(*a, **k): return []
    def check_date(*a, **k): return ""
    def append_row(row):
        app.logger.warning("append_row fallback - row: %s", row)
    def read_rules(*a, **k): return []
    def get_questions(*a, **k): return []
    def clear_sheet(*a, **k): app.logger.warning("clear_sheet fallback")
    def delete_rows_by_indices(*a, **k): app.logger.warning("delete_rows_by_indices fallback")
    def delete_rows_by_subject(*a, **k): app.logger.warning("delete_rows_by_subject fallback")
    def save_subscription(subject, client_id, subscription): raise NotImplementedError
    def list_subscriptions(subject): raise NotImplementedError

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
# Subscriptions helpers
# -------------------------
def _save_subscription_to_sheet(subject, client_id, subscription_obj):
    """
    Try to save via sheets.save_subscription(subject, client_id, subscription)
    Fallback: append_row(['__subscription__', subject, client_id, json.dumps(subscription), timestamp])
    """
    try:
        if SHEETS_AVAILABLE:
            try:
                save_subscription(subject, client_id or "", subscription_obj)
                return True
            except Exception as e:
                app.logger.debug("sheets.save_subscription failed: %s", e)
        # fallback append
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
        append_row(["__subscription__", subject, client_id or "", json.dumps(subscription_obj), timestamp])
        return True
    except Exception:
        app.logger.exception("Failed saving subscription via fallback")
        return False

def _load_subscriptions_for_subject(subject):
    """
    Try sheets.list_subscriptions(subject) -> return list of subscription dicts
    Fallback: scan get_questions rows for "__subscription__" entries and parse json
    """
    subs = []
    try:
        if SHEETS_AVAILABLE:
            try:
                raw = list_subscriptions(subject)
                # expected list of dicts with key 'subscription' or equivalent
                for r in raw:
                    if isinstance(r, dict) and r.get("subscription"):
                        subs.append(r.get("subscription"))
                # allow if list_subscriptions returned raw push subscription dicts
                if subs:
                    return subs
            except Exception as e:
                app.logger.debug("sheets.list_subscriptions failed: %s", e)
        # fallback: scan get_questions rows
        raw = get_questions() or []
        for row in raw:
            try:
                if len(row) >= 4 and str(row[0]) == "__subscription__" and str(row[1]) == subject:
                    sub_json = row[3]
                    sub = json.loads(sub_json)
                    subs.append(sub)
            except Exception:
                continue
    except Exception:
        app.logger.exception("Failed loading subscriptions fallback")
    return subs

def send_push_to_subject(subject, question_text, attempt_text):
    """
    Send push notifications to all subscriptions for subject.
    Returns dict {sent: int, errors: list}
    """
    subs = _load_subscriptions_for_subject(subject)
    if not subs:
        app.logger.debug("No subscribers for subject %s", subject)
        return {"sent": 0, "errors": ["no subscribers"]}

    title = f"New question in {subject}"
    q_preview = question_text.strip()
    a_preview = attempt_text.strip()
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
        app.logger.warning("pywebpush or VAPID private key not available — cannot send push. Payload: %s", payload)
        return {"sent": 0, "errors": ["pywebpush/VAPID not available"]}

    sent = 0
    errors = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT}
            )
            sent += 1
        except WebPushException as ex:
            app.logger.warning("WebPushException for subject %s: %s", subject, ex)
            errors.append(str(ex))
        except Exception as ex:
            app.logger.exception("webpush failed: %s", ex)
            errors.append(str(ex))
    return {"sent": sent, "errors": errors}

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
        return render_template(
            "content.html",
            message=FOOTER_MESSAGE,
            sheet_content=ws_content,
            sheet_title=ws_title,
            today_date=check_date(),
            form_list=form_list,
            dr_values=dr_values
        )
    except Exception as e:
        app.logger.exception("Content page error: %s", e)
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], error=str(e))

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
                # send pushes (best-effort)
                try:
                    result = send_push_to_subject(choice_selected, submitted_question, attempt)
                    app.logger.info("send_push_to_subject result: %s", result)
                except Exception as e:
                    app.logger.exception("send_push_to_subject error: %s", e)
            except Exception as e:
                app.logger.exception("Failed append_row: %s", e)
                flash("❌ Could not submit question. Try later.", "danger")
            return redirect(url_for("questions_page"))

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
    """
    try:
        payload = request.get_json(force=True)
        subscription = payload.get("subscription")
        subject = (payload.get("subject") or "").strip() or "Other"
        client_id = payload.get("client_id", "")
        if not subscription:
            return jsonify({"ok": False, "error": "subscription required"}), 400
        ok = _save_subscription_to_sheet(subject, client_id, subscription)
        if ok:
            return jsonify({"ok": True}), 200
        else:
            return jsonify({"ok": False, "error": "could not save subscription"}), 500
    except Exception as e:
        app.logger.exception("save-subscription failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

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
    # For GET: show the form even without key
    if request.method == "GET" and not provided_key:
        return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
    # if key provided, validate
    if provided_key != ADMIN_KEY:
        if request.method == "GET":
            flash("Unauthorized admin key. Please enter a valid admin key.", "warning")
            return render_template("admin_questions.html", questions=[], subjects=[], key="", message=FOOTER_MESSAGE)
        return "Unauthorized - include ?key=ADMIN_KEY or enter it into the Admin Key box on the page.", 401

    # authorized: load questions
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
    # serve static/service-worker.js at root so scope covers the whole site
    return send_from_directory(app.static_folder, 'service-worker.js')

# --- Admin test push endpoint (protected by ADMIN_KEY) ---
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
    # debug controlled by environment FLASK_DEBUG if you want
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
