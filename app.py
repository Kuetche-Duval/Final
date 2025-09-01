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

# --- Decode service account from Base64 ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

if SERVICE_ACCOUNT_B64:
    try:
        b64 = "".join(SERVICE_ACCOUNT_B64.split())
        data = base64.b64decode(b64)
        json.loads(data.decode("utf-8"))  # sanity check
        tmp = SERVICE_ACCOUNT_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        Path(tmp).replace(SERVICE_ACCOUNT_PATH)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except PermissionError:
            pass
    except Exception as ex:
        raise RuntimeError(f"Failed to decode/write service account: {ex}")
elif not os.path.exists(SERVICE_ACCOUNT_PATH):
    raise RuntimeError("Missing service account credentials and SERVICE_ACCOUNT_JSON_B64 not set.")

# --- Flask setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret")

# --- Globals ---
message = "Study hard, pray harder."
categories = [
    "Mathematics", "Biology", "Computer Science",
    "Further Mathematics", "Chemistry", "Physics", "Other"
]

# --- Imports that depend on service account ---
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
        ws_title = ws_values[0]
        ws_values = ws_values[1:]
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
        return f"Error loading content: {str(e)}", 500

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        # filter dropdown uses name "categoryFilter" in your template
        selected_category = request.form.get("categoryFilter", "")

        # handle new question submission
        if request.method == "POST":
            # if the user submitted a new question the form fields are name/category/question
            # note: categoryFilter also triggers POST when filter is used; above we already captured it
            username = request.form.get("name", "Anonymous").strip()
            questionText = request.form.get("question", "").strip()
            choiceSelected = request.form.get("category", "Other")

            if questionText:
                append_row([choiceSelected, username, questionText])
                # after append, redirect to GET so the new data appears (cache invalidated in append_row)
                return redirect(url_for("questions_page"))

        # Reload sheet at midnight (keep your existing behaviour)
        if reload_time == datetime.time(0, 0):
            clear_sheet()

        # fetch all questions (get_questions handles cache)
        all_questions = get_questions()
        # remove any empty rows (safety)
        all_questions = [q for q in all_questions if any(cell is not None and str(cell).strip() != "" for cell in q)]

        # build categories from sheet (column 0 is category)
        derived_categories = sorted({str(q[0]).strip() for q in all_questions if len(q) > 0 and q[0]})

        # filter by category — assuming sheet columns: 0=Category,1=Name,2=Question
        if selected_category and selected_category != "All":
            questions = [q for q in all_questions if len(q) > 0 and str(q[0]).strip() == selected_category.strip()]
        else:
            questions = all_questions

        return render_template(
            "Questions.html",
            questions=questions,
            message=message,
            categories=derived_categories or categories,  # fallback to your static list if sheet empty
            selected_category=selected_category
        )
    except Exception as e:
        # friendly error response (avoid crashing)
        return render_template(
            "Questions.html",
            questions=[],
            message=message,
            categories=categories,
            selected_category="",
            error=f"Unable to load questions right now: {e}"
        ), 200


@app.route("/drive-image/<file_id>")
def drive_image(file_id):
    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = requests.get(url, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"))
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
    app.run(host="0.0.0.0", port=port, debug=False)
