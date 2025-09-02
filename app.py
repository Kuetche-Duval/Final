import os
import base64
from pathlib import Path
from flask import Flask, render_template, url_for, redirect, Response, request
from dotenv import load_dotenv
import requests
import datetime
import json

# --- Load .env ---
load_dotenv()

# --- Decode service account from Base64 (if provided) and write to disk safely ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        # remove any whitespace/newlines, decode and validate JSON
        b64 = "".join(SERVICE_ACCOUNT_B64.split())
        data = base64.b64decode(b64)
        json.loads(data.decode("utf-8"))  # sanity check the JSON
        tmp = SERVICE_ACCOUNT_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        # atomically move into place
        Path(tmp).replace(SERVICE_ACCOUNT_PATH)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except PermissionError:
            # on some platforms (Windows) chmod may fail; ignore safely
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode/write service account: {ex}")
# If no base64 provided, ensure the file exists or let downstream fail with clear message
elif not os.path.exists(SERVICE_ACCOUNT_PATH):
    raise RuntimeError("Missing service account credentials and SERVICE_ACCOUNT_JSON_B64 not set.")

# --- Flask setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret")

# --- Globals ---
message = "Study hard, pray harder."
STATIC_CATEGORIES = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Other"
]

# --- Imports that rely on the service account file being present ---
# these modules should exist in your project (you provided earlier versions)
from sheets import from_google_sheets, check_date, append_row, read_rules, clear_sheet, get_questions, reload_time
from drive import from_google_drive, folders_and_images

# --- Routes ---
@app.route("/")
@app.route("/home")
def home_page():
    return render_template("final index.html", message="Smart for school, wise for eternity. ❤️")


@app.route("/content")
def content_page():
    try:
        ws_values = from_google_sheets()
        # defensive: if sheet is empty return an empty structure
        if not ws_values:
            ws_title = []
            ws_values = []
        else:
            # first row as title/header if present
            ws_title = ws_values[0] if len(ws_values) > 0 else []
            ws_values = ws_values[1:] if len(ws_values) > 1 else []

        dr_values = folders_and_images()
        form_list = from_google_drive(os.environ.get("FORMS_FOLDER_ID", ""), "application/vnd.google-apps.form")

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
        # Prefer returning a helpful error over crashing the whole app
        return f"Error loading content: {str(e)}", 500


@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        # Capture filter selection (categoryFilter) if present
        selected_category = (request.form.get("categoryFilter") or "").strip()

        # Handle new question submission (the new-question form posts name, category, question)
        if request.method == "POST" and request.form.get("question", "").strip():
            username = (request.form.get("name") or "Anonymous").strip()
            question_text = (request.form.get("question") or "").strip()
            choice_selected = (request.form.get("category") or "Other").strip() or "Other"

            # append as [category, username, question] - sheets.append_row will add timestamp
            append_row([choice_selected, username, question_text])

            # redirect to GET to avoid duplicate posts
            return redirect(url_for("questions_page"))

        # Optional: midnight clear behavior is intentionally disabled here because it was unreliable.
        # If you want to clear daily, implement a persistent last-cleared-date check instead.
        # if reload_time == datetime.time(0, 0):
        #     clear_sheet()

        # Fetch questions (get_questions() returns normalized rows with at least 4 columns)
        all_questions = get_questions() or []

        # extra safety: remove fully empty rows
        all_questions = [
            q for q in all_questions
            if isinstance(q, (list, tuple)) and any((cell is not None and str(cell).strip()) for cell in q)
        ]

        # Build set of derived categories from sheet (column 0), merge with STATIC_CATEGORIES
        derived_set = {
            str(q[0]).strip()
            for q in all_questions
            if len(q) > 0 and q[0]
        }

        extra_categories = [c for c in sorted(derived_set) if c not in STATIC_CATEGORIES]
        categories_for_ui = STATIC_CATEGORIES + extra_categories

        # Filter questions by selected category (empty string means show all)
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
        # Render a friendly page with no questions and an error message
        return render_template(
            "Questions.html",
            questions=[],
            message=message,
            categories=STATIC_CATEGORIES,
            selected_category="",
            error=f"Unable to load questions right now: {e}"
        ), 200


@app.route("/drive-image/<file_id>")
def drive_image(file_id):
    try:
        # lightweight proxy to serve Drive images by id
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = requests.get(url, stream=True, timeout=10)
        # content type depends on the file; we fallback to jpeg if missing
        content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.content, content_type=content_type)
    except Exception as e:
        return f"Image fetch error: {str(e)}", 500


@app.route("/rules")
def rules_page():
    try:
        return render_template("rules.html", rules=read_rules(), message=message)
    except Exception as e:
        return f"Error loading rules: {str(e)}", 500


# --- Entry point ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # debug=True is useful during development; set to False in production
    app.run(host="0.0.0.0", port=port, debug=True)
