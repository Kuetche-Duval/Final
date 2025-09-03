# app.py
import os
import base64
import json
from pathlib import Path
from flask import (
    Flask, render_template, url_for, redirect, request, flash
)
from dotenv import load_dotenv
import random
import logging
import traceback

load_dotenv()

# --- Service account handling ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64.strip())
        json.loads(data.decode("utf-8"))  # check valid JSON
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
        raise RuntimeError("Missing service account JSON")

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

# --- Imports ---
try:
    from sheets import (
        from_google_sheets, check_date, append_row,
        read_rules, get_questions, clear_sheet
    )
except ImportError as e:
    def fail(*a, **k): raise RuntimeError(f"sheets.py missing: {e}")
    from_google_sheets = check_date = append_row = read_rules = get_questions = clear_sheet = fail

try:
    from drive import from_google_drive, folders_and_images
except ImportError:
    def from_google_drive(*a, **k): return []
    def folders_and_images(*a, **k): return []

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
        return render_template("content.html", message=FOOTER_MESSAGE, sheet_content=[], sheet_title=[], today_date="", form_list=[], dr_values=[], error=str(e))

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    if request.method == "POST":
        submitted_question = (request.form.get("question") or "").strip()
        if submitted_question:
            username = (request.form.get("name") or "Anonymous").strip()
            choice_selected = (request.form.get("category") or "Other").strip()
            try:
                append_row([choice_selected, username, submitted_question])
                flash("✅ Question submitted!", "success")
            except Exception:
                flash("❌ Could not submit question. Try again later.", "danger")
        else:
            flash("⚠️ Please enter a question.", "warning")
        return redirect(url_for("questions_page"))

    selected_category = request.args.get("categoryFilter", "").strip()
    try:
        all_questions = get_questions() or []
    except Exception:
        all_questions = []

    derived_set = {str(q[0]).strip() for q in all_questions if q and len(q) > 0 and q[0]}
    categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]

    if selected_category:
        questions = [q for q in all_questions if len(q) > 0 and str(q[0]).strip() == selected_category]
    else:
        questions = all_questions

    return render_template(
        "questions.html",
        questions=questions,
        message=FOOTER_MESSAGE,
        categories=categories_for_ui,
        selected_category=selected_category
    )

@app.route("/about")
def about():
    return render_template("about.html", message=FOOTER_MESSAGE)

@app.route("/contact", methods=["GET", "POST"])
def contact():
    msg = None
    if request.method == "POST":
        content = request.form.get("message") or ""
        msg = "✅ Message sent!" if content else "❌ Please enter a message."
    return render_template("contact.html", message=FOOTER_MESSAGE, form_message=msg)

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
