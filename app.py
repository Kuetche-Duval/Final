# Robust Flask app for Newton's Nerds — fixed to avoid tuple returns and registered quiz_forms endpoint.
import os
import json
import logging
import traceback
import sqlite3
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, make_response, abort, Response
)
from dotenv import load_dotenv

# Optional pywebpush
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("newtons_nerds")

# Paths & config
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

CODING_LOCAL_PATH = Path(os.environ.get("CODING_EXERCISES_PATH", DATA_DIR / "coding_exercises.json"))
HEALTH_LOCAL_PATH = Path(os.environ.get("HEALTH_DATA_PATH", DATA_DIR / "health_data.json"))
QUIZ_LOCAL_PATH = Path(os.environ.get("QUIZ_FORMS_PATH", DATA_DIR / "quiz_forms.json"))
NOTIFICATIONS_FILE = Path(os.environ.get("NOTIFICATIONS_FILE", DATA_DIR / "notifications.json"))

NN_LOCAL_DB = Path(os.environ.get("NN_LOCAL_DB", "nn_local.db")).resolve()

FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

ADMIN_KEY = os.environ.get("ADMIN_KEY", "") or ""

# Ensure SQLite DB and tables
def _init_db():
    try:
        conn = sqlite3.connect(str(NN_LOCAL_DB))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                endpoint TEXT PRIMARY KEY,
                subject TEXT,
                client_id TEXT,
                p256dh TEXT,
                auth TEXT,
                raw_json TEXT,
                created_at TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT,
                subject TEXT,
                title TEXT,
                body TEXT,
                data TEXT,
                created_at TEXT,
                delivered INTEGER DEFAULT 0,
                delivered_at TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_endpoint_delivered ON notifications(endpoint, delivered)")
        conn.commit()
        conn.close()
        logger.info("SQLite DB initialized at %s", NN_LOCAL_DB)
    except Exception:
        logger.exception("DB init failed")

_init_db()

def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    try:
        u = urlparse(endpoint)
        scheme = u.scheme or "https"
        netloc = u.netloc or (u.path.split("/")[0] if u.path else "")
        path = u.path or ""
        return f"{scheme}://{netloc}{path}".rstrip("/")
    except Exception:
        return endpoint.strip()

def safe_load_json(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except Exception:
        logger.exception("Failed loading JSON: %s", path)
        return None

def safe_write_json(path: Path, obj):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        logger.exception("Failed writing JSON: %s", path)
        return False

# render_template_safe: always returns a Response
def render_template_safe(template_name: str, status: int = 200, **context) -> Response:
    try:
        html = render_template(template_name, **context)
        return make_response(html, status)
    except Exception as e:
        logger.warning("Template %s failed to render: %s", template_name, e)
        try:
            html = render_template("error.html", message=FOOTER_MESSAGE, error=str(e), traceback=traceback.format_exc())
            return make_response(html, 500)
        except Exception:
            body = f"Template rendering error: {template_name}\n\n{e}\n\n{traceback.format_exc()}"
            return make_response(body, 500)

# Subscriptions persistence (SQLite)
def save_subscription(subject, client_id, subscription_obj):
    try:
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}
        endpoint = subscription_obj.get("endpoint") or subscription_obj.get("url")
        if not endpoint:
            logger.warning("Subscription missing endpoint")
            return False
        endpoint_norm = normalize_endpoint(endpoint)
        keys = subscription_obj.get("keys", {}) if isinstance(subscription_obj, dict) else {}
        p256 = keys.get("p256dh", "")
        auth = keys.get("auth", "")
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            c.execute("""REPLACE INTO subscriptions
                         (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (endpoint_norm, subject or "Other", client_id or "", p256, auth, json.dumps(subscription_obj), now))
            conn.commit()
        return True
    except Exception:
        logger.exception("save_subscription failed")
        return False

def list_subscriptions(subject=None):
    try:
        out = []
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            if subject:
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions WHERE lower(subject)=?", (subject.lower(),))
            else:
                c.execute("SELECT subject, client_id, raw_json, created_at FROM subscriptions")
            rows = c.fetchall()
        for subj, client_id, raw_json, created_at in rows:
            try:
                parsed = json.loads(raw_json) if raw_json else {}
            except Exception:
                parsed = {}
            out.append({"subject": subj, "client_id": client_id, "subscription": parsed, "created_at": created_at})
        return out
    except Exception:
        logger.exception("list_subscriptions failed")
        return []

def delete_subscriptions_by_endpoint(endpoint):
    try:
        if not endpoint:
            return
        normalized = normalize_endpoint(endpoint)
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalized))
            conn.commit()
    except Exception:
        logger.exception("delete_subscriptions_by_endpoint failed")

# Notifications persistence
def save_notification(endpoint, subject, title, body, data_obj=None, persist_file=True):
    try:
        if not endpoint:
            logger.debug("save_notification skipped: no endpoint")
            return None
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications (endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""",
                      (endpoint_norm, subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        if persist_file:
            try:
                arr = safe_load_json(NOTIFICATIONS_FILE) or []
                if not isinstance(arr, list):
                    arr = []
                arr.append({
                    "id": nid, "endpoint": endpoint_norm, "subject": subject, "title": title, "body": body,
                    "data": data_obj or {}, "created_at": now, "delivered": False
                })
                safe_write_json(NOTIFICATIONS_FILE, arr)
            except Exception:
                logger.exception("backup notifications file append failed")
        return nid
    except Exception:
        logger.exception("save_notification failed")
        return None

def fetch_pending_notifications(endpoint):
    try:
        if not endpoint:
            return []
        endpoint_norm = normalize_endpoint(endpoint)
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint_norm,))
            rows = c.fetchall()
        out = []
        for nid, subj, title, body, data_text, created_at in rows:
            try:
                data_obj = json.loads(data_text or "{}")
            except Exception:
                data_obj = {}
            out.append({"id": nid, "subject": subj, "title": title, "body": body, "data": data_obj, "created_at": created_at})
        return out
    except Exception:
        logger.exception("fetch_pending_notifications failed")
        return []

def ack_notifications(endpoint, ids):
    try:
        if not endpoint or not ids:
            return 0
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        changed = 0
        with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
            c = conn.cursor()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint_norm))
                    if c.rowcount:
                        changed += 1
                except Exception:
                    continue
            conn.commit()
        try:
            arr = safe_load_json(NOTIFICATIONS_FILE) or []
            if isinstance(arr, list):
                for it in arr:
                    if it.get("id") in ids:
                        it["delivered"] = True
                        it["delivered_at"] = now
                safe_write_json(NOTIFICATIONS_FILE, arr)
        except Exception:
            logger.exception("failed to update notifications backup file")
        return changed
    except Exception:
        logger.exception("ack_notifications failed")
        return 0

# Push sending helper (optional)
def _send_push_single(subscription, payload):
    if not PYWEBPUSH_AVAILABLE:
        return {"ok": False, "error": "pywebpush-not-available"}
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return {"ok": False, "error": "vapid-not-configured"}
    try:
        webpush(subscription_info=subscription, data=json.dumps(payload), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
        return {"ok": True}
    except WebPushException as ex:
        status = getattr(ex, "response", None) and getattr(ex.response, "status_code", None)
        return {"ok": False, "error": str(ex), "status": status}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def send_push_to_subject(subject, question_text, attempt_text):
    subs = list_subscriptions(subject) or []
    if not subs:
        return {"sent": 0, "errors": ["no subscribers"], "detail": []}
    title = f"New message in {subject}"
    q_preview = (question_text or "")[:140]
    a_preview = (attempt_text or "")[:180]
    body = f"Q: {q_preview}\nA: {a_preview}"
    try:
        icon_url = url_for('static', filename='img/logo.png', _external=True)
    except Exception:
        icon_url = None
    payload_template = {"title": title, "body": body, "url": "/questions", "subject": subject, "icon": icon_url, "badge": icon_url, "preview": {"question": q_preview, "attempt": a_preview}}
    detail = []
    sent = 0
    for r in subs:
        sub = r.get("subscription") if isinstance(r, dict) else None
        ep = sub.get("endpoint") if sub else None
        save_notification(ep, subject, title, body, payload_template.get("preview"))
    if (not PYWEBPUSH_AVAILABLE) or (not VAPID_PRIVATE_KEY):
        for r in subs:
            sub = r.get("subscription") if isinstance(r, dict) else None
            ep = sub.get("endpoint") if sub else None
            detail.append({"endpoint": ep, "ok": False, "error": "sending-disabled"})
        return {"sent": 0, "errors": [d for d in detail if not d.get("ok")], "detail": detail}
    for r in subs:
        sub = r.get("subscription") if isinstance(r, dict) else None
        if not sub:
            detail.append({"endpoint": None, "ok": False, "error": "no-sub"})
            continue
        endpoint = sub.get("endpoint")
        if not endpoint:
            detail.append({"endpoint": None, "ok": False, "error": "no-endpoint"})
            continue
        payload = dict(payload_template)
        if len(json.dumps(payload).encode("utf-8")) > 2000:
            payload["preview"] = None
            payload["body"] = payload.get("body","")[:200] + "…"
        ok = False
        for attempt in range(2):
            res = _send_push_single(sub, payload)
            if res.get("ok"):
                ok = True
                sent += 1
                detail.append({"endpoint": normalize_endpoint(endpoint), "ok": True})
                try:
                    now = datetime.utcnow().replace(microsecond=0).isoformat()
                    with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                        c = conn.cursor()
                        c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, normalize_endpoint(endpoint)))
                        conn.commit()
                except Exception:
                    logger.exception("mark delivered failed")
                break
            else:
                status = res.get("status")
                err = res.get("error")
                logger.warning("Push failure to %s (status=%s): %s", endpoint, status, err)
                if status in (404, 410) or (err and "unsubscribed" in str(err).lower()):
                    try:
                        delete_subscriptions_by_endpoint(endpoint)
                    except Exception:
                        logger.exception("deleting stale subscription failed")
                    detail.append({"endpoint": normalize_endpoint(endpoint), "ok": False, "status": status, "error": err, "deleted": True})
                    ok = False
                    break
        if not ok:
            detail.append({"endpoint": normalize_endpoint(endpoint), "ok": False, "error": "final-failure"})
    return {"sent": sent, "errors": [d for d in detail if not d.get("ok")], "detail": detail}

# --- Routes ---

@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        logger.exception("service-worker not found")
        return abort(404)

@app.route("/favicon.ico")
def favicon():
    candidate = Path(app.static_folder) / "icons" / "icon-192.png"
    if candidate.exists():
        return send_from_directory(str(candidate.parent), candidate.name)
    logo = Path(app.static_folder) / "img" / "logo.png"
    if logo.exists():
        return send_from_directory(str(logo.parent), logo.name)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="100%" height="100%" fill="#2b8cff"/></svg>'
    return make_response(svg, 200, {"Content-Type": "image/svg+xml"})

@app.route("/drive-image/<path:file_id>")
def drive_image(file_id: str):
    logo = Path(app.static_folder) / "img" / "logo.png"
    if logo.exists():
        return send_from_directory(str(logo.parent), logo.name)
    return abort(404)

@app.route("/")
def home_page():
    return render_template_safe("index.html", message=FOOTER_MESSAGE)

# Explicit quiz_forms endpoint so templates using url_for('quiz_forms') succeed
@app.route("/quiz-forms", endpoint="quiz_forms")
def quiz_forms():
    forms = safe_load_json(QUIZ_LOCAL_PATH) or []
    return render_template_safe("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)

@app.route("/quizzes")
def quizzes_redirect():
    return redirect(url_for("quiz_forms"))

@app.route("/content")
def content_page():
    timetable_today = []
    sheet_title = None
    sheet_content = None
    dr_values = []
    form_list = safe_load_json(QUIZ_LOCAL_PATH) or []
    hero_image_id = os.environ.get("HERO_IMAGE_ID", "")
    notes_drive_url = os.environ.get("NOTES_DRIVE_URL", "")
    return render_template_safe("content.html",
                                message=FOOTER_MESSAGE,
                                timetable_today=timetable_today,
                                sheet_title=sheet_title,
                                sheet_content=sheet_content,
                                dr_values=dr_values,
                                form_list=form_list,
                                notes_drive_url=notes_drive_url,
                                hero_image_id=hero_image_id,
                                hero_title=os.environ.get("HERO_TITLE", "Stay Consistent"),
                                hero_subtitle=os.environ.get("HERO_SUBTITLE", "Success = small efforts repeated daily."))

@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        if request.method == "POST":
            question = (request.form.get("question") or "").strip()
            attempt = (request.form.get("attempt") or "").strip()
            if not question or not attempt:
                flash("Please provide both question and your attempt.", "warning")
                return redirect(url_for("questions_page"))
            name = (request.form.get("name") or "Anonymous").strip()
            category = (request.form.get("category") or "Other").strip()
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            qfile = DATA_DIR / "questions_local.json"
            try:
                arr = json.loads(qfile.read_text(encoding="utf-8") or "[]") if qfile.exists() else []
            except Exception:
                arr = []
            arr.append([category, name, question, attempt, timestamp])
            try:
                qfile.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("writing questions file failed")
            flash("Question submitted. Thank you!", "success")
            try:
                send_push_to_subject(category, question, attempt)
            except Exception:
                logger.exception("send_push_to_subject failed")
            resp = redirect(url_for("questions_page"))
            resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite='Lax')
            resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite='Lax')
            return resp

        qfile = DATA_DIR / "questions_local.json"
        raw = json.loads(qfile.read_text(encoding="utf-8") or "[]") if qfile.exists() else []
        questions = []
        for row in raw:
            try:
                lst = list(row) if isinstance(row, (list, tuple)) else [row]
                while len(lst) < 5:
                    lst.append("")
                if lst and str(lst[0]).strip() == "__subscription__":
                    continue
                questions.append(lst)
            except Exception:
                continue
        derived = {str(q[0]).strip() for q in questions if q and q[0]}
        STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "")
        if selected_category:
            questions = [q for q in questions if str(q[0]).strip() == selected_category]
        return render_template_safe("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE)
    except Exception:
        logger.exception("questions_page failed")
        flash("Could not load questions", "danger")
        return render_template_safe("questions.html", questions=[], categories=["Other"], selected_category="", message=FOOTER_MESSAGE)

@app.route("/coding")
def coding_page():
    exercises = safe_load_json(CODING_LOCAL_PATH) or []
    return render_template_safe("coding.html", exercises=exercises, message=FOOTER_MESSAGE)

@app.route("/health")
def health_index():
    plants = safe_load_json(HEALTH_LOCAL_PATH) or []
    return render_template_safe("health.html", plants=plants, message=FOOTER_MESSAGE)

@app.route("/health/<path:slug>")
def health_detail(slug: str):
    plants = safe_load_json(HEALTH_LOCAL_PATH) or []
    plant = None
    for p in plants:
        try:
            if p.get("slug") == slug or p.get("name","").lower().replace(" ", "-") == slug:
                plant = p
                break
        except Exception:
            continue
    if not plant:
        plant = {"name": slug.replace("-", " ").title(), "virtues": [], "uses": "No details available.", "notes": "", "slug": slug}
    return render_template_safe("health_detail.html", plant=plant, message=FOOTER_MESSAGE)

@app.route("/admin/announcement", methods=["GET", "POST"])
def admin_announcement():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided_key:
        return render_template_safe("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template_safe("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if request.method == "POST":
        subject = (request.form.get("subject") or "All").strip()
        title = (request.form.get("title") or "Announcement").strip()
        body = (request.form.get("body") or "").strip()
        results = []
        try:
            if subject.lower() in ("all","everyone","any"):
                subs = list_subscriptions()
                uniq = {}
                for r in subs:
                    sub = r.get("subscription") if isinstance(r, dict) else None
                    if not sub:
                        continue
                    ep = sub.get("endpoint")
                    if ep:
                        epn = normalize_endpoint(ep)
                        if epn and epn not in uniq:
                            uniq[epn] = sub
                for sub in uniq.values():
                    try:
                        save_notification(sub.get("endpoint"), subject, title, body, {"announcement": True})
                        if PYWEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY:
                            res = _send_push_single(sub, {"title": title, "body": body, "url": "/"})
                            results.append({"endpoint": normalize_endpoint(sub.get("endpoint")), **res})
                        else:
                            results.append({"endpoint": normalize_endpoint(sub.get("endpoint")), "ok": False, "error": "push-not-configured"})
                    except Exception:
                        results.append({"endpoint": None, "ok": False, "error": "exception"})
                flash(f"Announcement queued/sent to {len(results)} endpoints.", "success")
                return render_template_safe("admin_announcement.html", key=provided_key, result=results, message=FOOTER_MESSAGE)
            else:
                res = send_push_to_subject(subject, title, body)
                flash("Announcement attempted.", "success")
                return render_template_safe("admin_announcement.html", key=provided_key, result=res, message=FOOTER_MESSAGE)
        except Exception:
            logger.exception("admin_announcement failed")
            flash("Failed to send announcement.", "danger")
            return render_template_safe("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)
    return render_template_safe("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)

@app.route("/admin/exercises", methods=["GET", "POST"])
def admin_exercises():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided_key:
        return render_template_safe("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template_safe("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)
    if request.method == "POST":
        data = {
            "id": request.form.get("id"),
            "title": request.form.get("title"),
            "language": request.form.get("language"),
            "difficulty": request.form.get("difficulty"),
            "description": request.form.get("description"),
            "starter": request.form.get("starter_code"),
            "solution": request.form.get("solution_code"),
            "sample_data": None
        }
        sample = request.form.get("sample_data")
        if sample:
            try:
                data["sample_data"] = json.loads(sample)
            except Exception:
                data["sample_data"] = None
        try:
            arr = safe_load_json(CODING_LOCAL_PATH) or []
            arr = [e for e in arr if str(e.get("id")) != str(data.get("id"))]
            arr.append(data)
            safe_write_json(CODING_LOCAL_PATH, arr)
            flash("Saved to local coding_exercises.json", "success")
        except Exception:
            logger.exception("Failed saving exercise")
            flash("Failed saving exercise", "danger")
        return render_template_safe("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)
    return render_template_safe("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)

@app.route("/admin/questions", methods=["GET", "POST"])
def admin_questions():
    provided = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided:
        return render_template_safe("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)
    if provided != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template_safe("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)

    qfile = DATA_DIR / "questions_local.json"
    raw = json.loads(qfile.read_text(encoding="utf-8") or "[]") if qfile.exists() else []
    rows = []
    for idx, r in enumerate(raw):
        try:
            if isinstance(r, dict):
                rows.append({"index": idx, "subject": r.get("subject",""), "user": r.get("name", r.get("user","")), "text": r.get("question", r.get("text","")), "attempt": r.get("attempt","")})
            else:
                lst = list(r) if isinstance(r, (list, tuple)) else [r]
                rows.append({"index": idx, "subject": lst[0] if len(lst)>0 else "", "user": lst[1] if len(lst)>1 else "", "text": lst[2] if len(lst)>2 else "", "attempt": lst[3] if len(lst)>3 else ""})
        except Exception:
            continue
    subjects = sorted(list({r["subject"] for r in rows if r.get("subject")}))

    action = request.form.get("action") or ""
    if request.method == "POST" and action == "delete_selected":
        selected = request.form.getlist("selected")
        selected_idx = set(int(x) for x in selected if str(x).isdigit())
        new = [r for i,r in enumerate(raw) if i not in selected_idx]
        try:
            qfile.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
            flash(f"Deleted {len(selected_idx)} rows.", "success")
        except Exception:
            logger.exception("Failed deleting selected")
            flash("Failed deleting selected", "danger")
        return redirect(url_for("admin_questions", key=provided))
    if request.method == "POST" and request.form.get("action") == "delete_subject":
        subj = request.form.get("subject_to_delete") or ""
        if subj:
            new = [r for r in raw if not ((isinstance(r, (list,tuple)) and str(r[0])==str(subj)) or (isinstance(r, dict) and str(r.get("subject",""))==str(subj)))]
            try:
                qfile.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
                flash(f"Deleted questions for subject {subj}.", "success")
            except Exception:
                logger.exception("Failed deleting subject")
                flash("Failed deleting subject", "danger")
        return redirect(url_for("admin_questions", key=provided))

    return render_template_safe("admin_questions.html", key=provided, questions=rows, subjects=subjects, message=FOOTER_MESSAGE)

def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    import sqlite3 as _sqlite
    conn = _sqlite.connect(":memory:")
    conn.row_factory = _sqlite.Row
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
        except Exception:
            stmts = [s.strip() for s in re.split(r';\s*(?=\b)', schema_sql) if s.strip()]
            for s in stmts:
                try:
                    cur.executescript(s)
                except Exception:
                    try:
                        cur.execute(s)
                    except Exception:
                        continue
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
                continue
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
                    vals = [str(r.get(c, "")) if isinstance(r, dict) else "" for c in cols]
                    cur.execute(insert_sql, vals)
                except Exception:
                    continue
    conn.commit()
    return conn, [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

@app.route("/exercise-table/<path:name>", methods=["GET", "POST"])
def exercise_table(name: str):
    if request.method == "GET":
        return render_template_safe("error.html", error="Use POST with SQL schema to preview", message=FOOTER_MESSAGE)
    payload_schema = ""
    payload_sample = None
    if request.is_json:
        try:
            j = request.get_json(silent=True) or {}
            payload_schema = j.get("schema") or j.get("sql") or ""
            payload_sample = j.get("sample") or j.get("sample_data") or {}
        except Exception:
            payload_schema = ""
            payload_sample = None
    if not payload_schema:
        payload_schema = (request.form.get("schema") or request.form.get("sql") or "").strip()
    if not payload_schema:
        try:
            raw = (request.get_data(as_text=True) or "").strip()
            if raw:
                if raw.lstrip().startswith("{") or raw.lstrip().startswith("["):
                    try:
                        j = json.loads(raw)
                        payload_schema = j.get("schema") or j.get("sql") or ""
                        payload_sample = j.get("sample") or {}
                    except Exception:
                        payload_schema = raw
                else:
                    payload_schema = raw
        except Exception:
            pass
    if not payload_schema:
        return make_response(jsonify({"ok": False, "error": "schema (SQL) required"}), 400)
    if payload_sample is None:
        try:
            sample_text = (request.form.get("sample") or request.form.get("sample_data") or "").strip()
            payload_sample = json.loads(sample_text) if sample_text else {}
        except Exception:
            payload_sample = {}
    try:
        conn, tables = _safe_create_and_populate_sqlite(payload_schema, payload_sample)
        preview = {}
        for t in tables:
            try:
                cur = conn.execute(f"SELECT * FROM \"{t}\" LIMIT 50")
                preview[t] = [dict(r) for r in cur.fetchall()]
            except Exception:
                preview[t] = []
        conn.close()
        return make_response(jsonify({"ok": True, "name": name, "tables": tables, "preview": preview}), 200)
    except Exception as e:
        logger.exception("exercise_table error")
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)

@app.route("/save-subscription", methods=["POST"])
def save_subscription_route():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = payload.get("subscription") or payload.get("push") or payload
        if isinstance(subscription, str):
            try:
                subscription = json.loads(subscription)
            except Exception:
                subscription = {"endpoint": subscription}
        subject = (payload.get("subject") or request.args.get("subject") or "Other")
        client_id = payload.get("client_id") or ""
        if not subscription or not isinstance(subscription, dict) or not (subscription.get("endpoint") or subscription.get("url")):
            return make_response(jsonify({"ok": False, "error": "subscription required"}), 400)
        ok = save_subscription(subject, client_id, subscription)
        endpoint = subscription.get("endpoint")
        subjects_for_endpoint = []
        try:
            subs = list_subscriptions()
            for r in subs:
                sub = r.get("subscription") if isinstance(r, dict) else None
                subj = r.get("subject") if isinstance(r, dict) else None
                if sub and subj and normalize_endpoint(sub.get("endpoint")) == normalize_endpoint(endpoint):
                    subjects_for_endpoint.append((subj or "Other"))
            subjects_for_endpoint = sorted(list(dict.fromkeys(subjects_for_endpoint)))
        except Exception:
            subjects_for_endpoint = []
        resp = make_response(jsonify({"ok": ok, "subjects": subjects_for_endpoint}), (200 if ok else 500))
        try:
            if endpoint:
                resp.set_cookie('nn_subscription', normalize_endpoint(endpoint), max_age=COOKIE_MAX_AGE, samesite='Lax')
        except Exception:
            pass
        return resp
    except Exception:
        logger.exception("save-subscription failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return make_response(jsonify({"ok": False, "error": "endpoint required"}), 400)
        out_subjects = []
        try:
            subs = list_subscriptions()
            for r in subs:
                sub = r.get("subscription") if isinstance(r, dict) else None
                subj = r.get("subject") if isinstance(r, dict) else None
                if sub and subj and normalize_endpoint(sub.get("endpoint")) == normalize_endpoint(endpoint):
                    out_subjects.append((subj or "Other"))
        except Exception:
            logger.exception("subscriptions_check failed")
        return make_response(jsonify({"ok": True, "subjects": sorted(list(dict.fromkeys(out_subjects)))}), 200)
    except Exception:
        logger.exception("subscriptions_check top-level failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.route("/notifications/pending", methods=["GET"])
def notifications_pending():
    endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
    if not endpoint:
        return make_response(jsonify({"ok": False, "error": "endpoint required"}), 400)
    items = fetch_pending_notifications(endpoint)
    return make_response(jsonify({"ok": True, "items": items}), 200)

@app.route("/notifications/ack", methods=["POST"])
def notifications_ack():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        endpoint = (payload.get("endpoint") or "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        ids = payload.get("ids") or []
        if not endpoint:
            return make_response(jsonify({"ok": False, "error": "endpoint required"}), 400)
        if not isinstance(ids, list):
            return make_response(jsonify({"ok": False, "error": "ids must be a list"}), 400)
        n = ack_notifications(endpoint, ids)
        return make_response(jsonify({"ok": True, "count": n}), 200)
    except Exception:
        logger.exception("notifications_ack failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.errorhandler(404)
def handle_404(e):
    return render_template_safe("errors/404.html", status=404, message=FOOTER_MESSAGE)

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
    if request.is_json or request.path.startswith("/api/"):
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)
    return render_template_safe("error.html", status=500, message=FOOTER_MESSAGE, error=str(e))

def _log_templates():
    needed = ["base.html", "index.html", "content.html", "questions.html", "quiz_forms.html",
              "coding.html", "health.html", "health_detail.html", "rules.html",
              "error.html", "admin_announcement.html", "admin_questions.html", "errors/404.html",
              "admin_exercises.html"]
    present, missing = [], []
    for t in needed:
        try:
            app.jinja_env.get_or_select_template(t)
            present.append(t)
        except Exception:
            missing.append(t)
    logger.info("Templates present: %s", present)
    if missing:
        logger.warning("Templates missing: %s", missing)

_log_templates()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    logger.info("Starting app on port %s (debug=%s)", port, debug_mode)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
