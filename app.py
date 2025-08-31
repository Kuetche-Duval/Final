import os
import base64
from flask import Flask, render_template, url_for, redirect, Response, request
from dotenv import load_dotenv
import requests
import datetime

# Load .env automatically
load_dotenv()

# --- Secure secrets and credentials ---
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
if SERVICE_ACCOUNT_B64:
    with open(SERVICE_ACCOUNT_PATH, "wb") as f:
        f.write(base64.b64decode(SERVICE_ACCOUNT_B64))
    try:
        os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
    except PermissionError:
        pass  # Mobile (Pydroid/Termux) may not allow chmod

# --- Flask app setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret")  # Must be set in .env

# --- App globals ---
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
        if request.method == "POST":
            username = request.form.get("username", "Anonymous")
            questionText = request.form.get("questionText", "").strip()
            choiceSelected = request.form.get("category", "Other")

            if questionText:
                append_row([choiceSelected, username, questionText])
            return redirect(url_for("questions_page"))

        if reload_time == datetime.time(0, 0):
            clear_sheet()

        return render_template("Questions.html", questions=get_questions(), message=message, categories=categories)
    except Exception as e:
        return f"Error in questions page: {str(e)}", 500

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