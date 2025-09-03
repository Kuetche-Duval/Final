# app.py
import os
import base64
import json
from pathlib import Path
from flask import (
    Flask, render_template, url_for, redirect, Response,
    request, flash
)
from dotenv import load_dotenv
import requests
import datetime
import random
import logging
import threading
from datetime import timedelta

load_dotenv()

# --- Service account handling (Base64 -> file) ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        b64 = "".join(SERVICE_ACCOUNT_B64.split())
        data = base64.b64decode(b64)
        json.loads(data.decode("utf-8"))  # sanity check
        tmp = SERVICE_ACCOUNT_PATH + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        Path(tmp).replace(SERVICE_ACCOUNT_PATH)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode/write service account: {ex}")
else:
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(
            "Missing service account credentials: set SERVICE_ACCOUNT_JSON_B64 or place "
            f"a JSON at {SERVICE_ACCOUNT_PATH}."
        )

# --- Flask app setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# --- App constants ---
message = "Study hard, pray harder."
STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Other"
]
MOTIVATION_VERSES = [
    ("A new commandment I give to you: Love one another; as I have loved you, so you must love one another.", "John 13:34"),
    ("I can do all things through Christ who strengthens me.", "Philippians 4:13"),
    ("Trust in the Lord with all your heart and lean not on your own understanding.", "Proverbs 3:5"),
]

# --- sheets + drive imports (defensive) ---
try:
    from sheets import (
        from_google_sheets, check_date, append_row,
        read_rules, get_questions, clear_sheet, CACHE_TTL_QUESTIONS
    )
except Exception as e:
    app.logger.exception("Failed to import sheets module.")
    def _missing_sheets(*args, **kwargs):
        raise RuntimeError(f"sheets module unavailable: {e}")
    from_google_sheets = check_date = append_row = read_rules = get_questions = clear_sheet = _missing_sheets
    CACHE_TTL_QUESTIONS = 30

try:
    from drive import from_google_drive, folders_and_images
except Exception as e:
    app.logger.warning("drive.py import failed or not present: %s", e)
    def from_google_drive(*args, **kwargs):
        raise RuntimeError(f"drive module unavailable: {e}")
    def folders_and_images(*args, **kwargs):
        return []

# -------------------------
# Daily midnight scheduler (in-process, optional)
# -------------------------
ENABLE_DAILY_CLEAR = os.environ.get("ENABLE_DAILY_CLEAR", "true").lower() in ("1", "true", "yes")

_scheduler_timer = None
_scheduler_lock = threading.Lock()

def seconds_until_next_midnight():
    now = datetime.datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()

def _run_clear_and_reschedule():
    global _scheduler_timer
    try:
        app.logger.info("Midnight scheduler running: calling clear_sheet()")
        try:
            clear_sheet()
            app.logger.info("clear_sheet() finished.")
        except Exception as e:
            app.logger.exception("clear_sheet() failed in scheduled run: %s", e)
    finally:
        try:
            sec = seconds_until_next_midnight()
            app.logger.info("Scheduling next midnight clear in %s seconds.", sec)
            t = threading.Timer(sec, _run_clear_and_reschedule)
            t.daemon = True
            with _scheduler_lock:
                _scheduler_timer = t
            t.start()
        except Exception:
            app.logger.exception("Failed to schedule next midnight clear")

def start_daily_midnight_scheduler():
    global _scheduler_timer
    if not ENABLE_DAILY_CLEAR:
        app.logger.info("Daily clear scheduler disabled by env.")
        return
    with _scheduler_lock:
        if _scheduler_timer is not None:
            app.logger.info("Scheduler already running.")
            return
        sec = seconds_until_next_midnight()
        app.logger.info("Starting midnight clear scheduler; first run in %s seconds.", sec)
        t = threading.Timer(sec, _run_clear_and_reschedule)
        t.daemon = True
        _scheduler_timer = t
        t.start()

# -------------------------
# Routes
# -------------------------
@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = random.choice(MOTIVATION_VERSES)
    try:
        return render_template("index.html", message="Smart for school, wise for eternity. ❤️", verse=verse, reference=reference)
    except Exception as e:
        app.logger.exception("Error rendering home page")
        return f"Template render error: {e}", 500

@app.route("/content")
def content_page():
    try:
        ws_values = from_google_sheets() or []
        if not ws_values:
            ws_title, ws_values = [], []
        else:
            ws_title = ws_values[0] if len(ws_values) > 0 else []
            ws_values = ws_values[1:] if len(ws_values) > 1 else []

        try:
            dr_values = folders_and_images()
        except Exception as ex:
            app.logger.warning("folders_and_images failed: %s", ex)
            dr_values = []

        try:
            form_list = from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form")
        except Exception as ex:
            app.logger.info("from_google_drive issue or no forms: %s", ex)
            form_list = []

        return render_template(
            "content.html",
            message=message,
            sheet_content=ws_values,
            sheet_title=ws_title,
            today_date=check_date(),
            form_list=form_list,
            dr_values=dr_values
        )
    except Exception as e:
        app.logger.exception("Error loading content page")
        return render_template(
            "content.html",
            message=message,
            sheet_content=[],
            sheet_title=[],
            today_date=[],
            form_list=[],
            dr_values=[],
            error=f"Error loading content: {e}"
        ), 500

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        # Post = new question submission (PRG)
        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            if submitted_question:
                username = (request.form.get("name") or "Anonymous").strip()
                question_text = submitted_question
                choice_selected = (request.form.get("category") or "").strip() or "Other"
                try:
                    append_row([choice_selected, username, question_text])
                    flash("Question submitted — thanks!", "success")
                except Exception as ex:
                    app.logger.exception("Failed to append question")
                    flash("Could not submit question at this time. Try again later.", "danger")
                return redirect(url_for("questions_page"))
            else:
                flash("Please enter a question before submitting.", "warning")
                return redirect(url_for("questions_page"))

        # GET: optional category filter via querystring
        selected_category = (request.args.get("categoryFilter") or "").strip()

        all_questions = get_questions() or []
        all_questions = [
            q for q in all_questions
            if isinstance(q, (list, tuple)) and any((cell is not None and str(cell).strip()) for cell in q)
        ]

        derived_set = {
            str(q[0]).strip()
            for q in all_questions
            if len(q) > 0 and q[0]
        }
        extra_categories = [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]
        categories_for_ui = STATIC_CATEGORIES + extra_categories

        if selected_category:
            questions = [q for q in all_questions if len(q) > 0 and str(q[0]).strip() == selected_category]
        else:
            questions = all_questions

        return render_template(
            "questions.html",
            questions=questions,
            message=message,
            categories=categories_for_ui,
            selected_category=selected_category
        )
    except Exception as e:
        app.logger.exception("Error in questions_page")
        return render_template(
            "questions.html",
            questions=[],
            message=message,
            categories=STATIC_CATEGORIES,
            selected_category="",
            error="Unable to load questions right now. Please try again later."
        ), 200

@app.route("/drive-image/<file_id>")
def drive_image(file_id):
    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = requests.get(url, stream=True, timeout=10)
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.content, content_type=content_type)
    except requests.exceptions.RequestException as re:
        app.logger.exception("Drive image fetch failed")
        return f"Image fetch error: {re}", 502
    except Exception as e:
        app.logger.exception("Unexpected drive image error")
        return f"Image fetch error: {e}", 500

@app.route("/rules")
def rules_page():
    try:
        rules = read_rules() or []
        return render_template("rules.html", rules=rules, message=message)
    except Exception as e:
        app.logger.exception("Error loading rules")
        return render_template("rules.html", rules=[], message=message, error="Unable to load rules right now."), 500

# Optional: keep admin clear if you want manual clear (remove if you prefer)
@app.route("/admin/clear-questions", methods=["POST"])
def admin_clear_questions():
    admin_secret = os.environ.get("ADMIN_CLEAR_SECRET")
    provided = request.form.get("secret") or ""
    if not admin_secret or provided != admin_secret:
        return "Unauthorized", 401
    try:
        clear_sheet()
        return "Cleared", 200
    except Exception as e:
        app.logger.exception("Failed to clear questions")
        return f"Failed: {e}", 500

# --- Entry point ---
if __name__ == "__main__":
    # Start scheduler when running directly (guard reloader)
    try:
        debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("1", "true", "yes")
        should_start_scheduler = ENABLE_DAILY_CLEAR and (not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true")
        if should_start_scheduler:
            start_daily_midnight_scheduler()
    except Exception:
        app.logger.exception("Failed to start midnight clear scheduler at startup.")

    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
