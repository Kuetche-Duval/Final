# app.py
import os
import base64
import json
import random
import logging
import traceback
from flask import Flask, render_template, url_for, redirect, request, flash
from dotenv import load_dotenv

# --- Load environment ---
load_dotenv()

# --- Service account handling (best-effort, do not crash app) ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64.strip())
        json.loads(data.decode("utf-8"))  # validate
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode service account: {ex}")
else:
    # Do not raise here — allow the app to start even if service account is not present.
    # Sheets/Drive endpoints will log errors but app won't crash immediately.
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        logging.getLogger(__name__).warning(
            "Service account file not found at %s — Sheets/Drive will likely fail.", SERVICE_ACCOUNT_PATH
        )

# --- Flask setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
logging.basicConfig(level=logging.INFO)

FOOTER_MESSAGE = "Study hard, pray harder."
STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Other"
]
MOTIVATION_VERSES = [
    ("A new commandment I give to you: Love one another; as I have loved you, so you must love one another.", "John 13:34"),
    ("I can do all things through Christ who strengthens me.", "Philippians 4:13"),
    ("Trust in the Lord with all your heart and lean not on your own understanding.", "Proverbs 3:5"),
]

# --- Sheets/Drive imports with safe fallbacks ---
try:
    from sheets import from_google_sheets, check_date, append_row, read_rules, get_questions, clear_sheet
except Exception as e:
    app.logger.warning("sheets.py import failed: %s — substituting no-op fallbacks", e)

    def from_google_sheets(*a, **k):
        app.logger.debug("from_google_sheets fallback called")
        return []

    def check_date(*a, **k):
        app.logger.debug("check_date fallback called")
        return []

    def append_row(row):
        app.logger.warning("append_row fallback called (no-op). Row: %s", row)
        return None

    def read_rules(*a, **k):
        app.logger.debug("read_rules fallback called")
        return []

    def get_questions(*a, **k):
        app.logger.debug("get_questions fallback called")
        return []

    def clear_sheet(*a, **k):
        app.logger.warning("clear_sheet fallback called (no-op)")

try:
    from drive import from_google_drive, folders_and_images
except Exception:
    app.logger.warning("drive.py import failed — substituting fallbacks")

    def from_google_drive(*a, **k):
        return []

    def folders_and_images(*a, **k):
        return []

# -------------------------
# Routes
# -------------------------
@app.route("/")
@app.route("/home")
def home_page():
    verse, reference = random.choice(MOTIVATION_VERSES)
    return render_template("index.html", message=FOOTER_MESSAGE, verse=verse, reference=reference)


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
        app.logger.error("Content page error: %s\n%s", e, traceback.format_exc())
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], error=str(e))


@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    """
    POST:
      - If form includes 'question' -> treat as new question submission.
      - If form *doesn't* include 'question' but includes 'category' -> treat as a filter (legacy forms that POST).
    GET:
      - Renders page using optional ?categoryFilter=Category
    """
    try:
        # Accept filter submissions that were sent via POST (some templates used POST for filter).
        if request.method == "POST" and not request.form.get("question") and request.form.get("category"):
            # redirect to GET with categoryFilter so filtering works
            return redirect(url_for("questions_page", categoryFilter=request.form.get("category", "")))

        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            if submitted_question:
                username = (request.form.get("name") or "Anonymous").strip()
                choice_selected = (request.form.get("category") or "Other").strip()
                try:
                    append_row([choice_selected, username, submitted_question])
                    flash("✅ Question submitted!", "success")
                except Exception as e:
                    app.logger.error("Failed append_row: %s", e)
                    flash("❌ Could not submit question. Try later.", "danger")
            else:
                flash("⚠️ Please enter a question.", "warning")
            return redirect(url_for("questions_page"))

        # GET handling
        selected_category = request.args.get("categoryFilter", "").strip()
        try:
            all_questions = get_questions() or []
        except Exception as e:
            app.logger.error("get_questions failed: %s", e)
            all_questions = []

        # Ensure normalized rows (at least 3 items)
        normalized_questions = []
        for row in all_questions:
            try:
                r = list(row)
                while len(r) < 4:
                    r.append("")
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
            "Questions.html",
            questions=questions,
            message=FOOTER_MESSAGE,
            categories=categories_for_ui,
            selected_category=selected_category
        )

    except Exception as e:
        app.logger.error("Questions page error: %s\n%s", e, traceback.format_exc())
        flash("❌ Failed to load questions. Try again later.", "danger")
        return render_template("questions.html", questions=[], message=FOOTER_MESSAGE, categories=STATIC_CATEGORIES, selected_category="")

@app.route("/rules")
def rules_page():
    try:
        rules = read_rules() or []
    except Exception:
        rules = []
    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)


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


# --- Global error handler ---
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500


# --- App entry ---
if __name__ == "__main__":
    # Use Render environment port & bind 0.0.0.0
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
