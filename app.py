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

# Load environment
load_dotenv()

# --- Service account handling (decode Base64 -> file) ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        b64 = "".join(SERVICE_ACCOUNT_B64.split())
        data = base64.b64decode(b64)
        # sanity-check JSON before writing
        json.loads(data.decode("utf-8"))
        tmp = SERVICE_ACCOUNT_PATH + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        Path(tmp).replace(SERVICE_ACCOUNT_PATH)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            # Not critical — platforms vary (Windows)
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode/write service account: {ex}")
else:
    # If no b64 provided and file missing, fail early with helpful message.
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(
            "Missing service account credentials: set SERVICE_ACCOUNT_JSON_B64 or place "
            f"a JSON at {SERVICE_ACCOUNT_PATH}."
        )

# --- Flask setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
# Configure logger
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# --- Global constants / data ---
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

# --- Import sheets + drive with defensive guards ---
try:
    # Import only the functions we actually use (no clearing)
    from sheets import (
        from_google_sheets, check_date, append_row,
        read_rules, get_questions, CACHE_TTL_QUESTIONS
    )
except Exception as e:
    app.logger.exception("Failed to import sheets module.")
    def _missing_sheets(*args, **kwargs):
        raise RuntimeError(f"sheets module unavailable: {e}")
    from_google_sheets = check_date = append_row = read_rules = get_questions = _missing_sheets
    CACHE_TTL_QUESTIONS = 30

try:
    from drive import from_google_drive, folders_and_images
except Exception as e:
    app.logger.warning("drive.py import failed or not present: %s", e)
    def from_google_drive(*args, **kwargs):
        raise RuntimeError(f"drive module unavailable: {e}")
    def folders_and_images(*args, **kwargs):
        return []

# --- Routes ---
@app.route("/")
@app.route("/home")
def home_page():
    """Home page — choose a random verse for warmth."""
    verse, reference = random.choice(MOTIVATION_VERSES)
    try:
        return render_template(
            "final index.html",
            message="Smart for school, wise for eternity. ❤️",
            verse=verse,
            reference=reference
        )
    except Exception as e:
        app.logger.exception("Error rendering home page")
        return f"Template render error: {e}", 500


@app.route("/content")
def content_page():
    """Main content page: timetable (sheets) + drive forms + drive images list."""
    try:
        ws_values = from_google_sheets() or []
        if not ws_values:
            ws_title, ws_values = [], []
        else:
            ws_title = ws_values[0] if len(ws_values) > 0 else []
            ws_values = ws_values[1:] if len(ws_values) > 1 else []

        # Drive values - keep errors localized
        try:
            dr_values = folders_and_images()
        except Exception as ex:
            app.logger.warning("folders_and_images failed: %s", ex)
            dr_values = []

        try:
            form_list = from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form")
        except Exception as ex:
            app.logger.info("No drive forms or drive module problem: %s", ex)
            form_list = []

        return render_template(
            "Newton's Nerds other html.html",
            message=message,
            sheet_content=ws_values,
            sheet_title=ws_title,
            today_date=check_date(),
            form_list=form_list,
            dr_values=dr_values
        )
    except Exception as e:
        app.logger.exception("Error loading content page")
        # Render a helpful error page instead of crashing
        return render_template(
            "Newton's Nerds other html.html",
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
    """
    Display questions and accept new submissions.
    GET -> show questions
    POST -> handle new question then redirect (Post/Redirect/Get pattern)
    """
    try:
        # Handle submission first (PRG)
        if request.method == "POST":
            submitted_question = (request.form.get("question") or "").strip()
            if submitted_question:
                username = (request.form.get("name") or "Anonymous").strip()
                question_text = submitted_question
                choice_selected = (request.form.get("category") or "").strip() or "Other"

                # append row (category, username, question). append_row will include a timestamp.
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

        # GET: read optional category filter (from querystring preferred for idempotence)
        selected_category = (request.args.get("categoryFilter") or "").strip()

        # Fetch all questions
        all_questions = get_questions() or []

        # remove fully empty rows defensively
        all_questions = [
            q for q in all_questions
            if isinstance(q, (list, tuple)) and any((cell is not None and str(cell).strip()) for cell in q)
        ]

        # derive categories from sheet column 0
        derived_set = {
            str(q[0]).strip()
            for q in all_questions
            if len(q) > 0 and q[0]
        }

        extra_categories = [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]
        categories_for_ui = STATIC_CATEGORIES + extra_categories

        # apply filter if requested
        if selected_category:
            questions = [
                q for q in all_questions
                if len(q) > 0 and str(q[0]).strip() == selected_category
            ]
        else:
            questions = all_questions

        return render_template(
            "Questions.html",
            questions=questions,
            message=message,
            categories=categories_for_ui,
            selected_category=selected_category
        )

    except Exception as e:
        app.logger.exception("Error in questions_page")
        # Render the page with a friendly error message rather than failing completely
        return render_template(
            "Questions.html",
            questions=[],
            message=message,
            categories=STATIC_CATEGORIES,
            selected_category="",
            error=f"Unable to load questions right now. Please try again later."
        ), 200


@app.route("/drive-image/<file_id>")
def drive_image(file_id):
    """
    Lightweight proxy to fetch image bytes from Google Drive by file id.
    """
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


# --- Entry point ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("1", "true", "yes")
    # Use host 0.0.0.0 for container deployment compatibility
    app.run(host="0.0.0.0", port=port, debug=debug)
