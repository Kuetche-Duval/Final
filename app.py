"""
Robust Flask app for Newton's Nerds — updated to:
 - always return valid responses (no tuple-return pitfalls)
 - provide /favicon.ico and /quiz-forms routes to satisfy templates
 - use optional data JSON files in data/: coding_exercises.json, health_data.json, quiz_forms.json, notifications.json
 - persist notifications to both SQLite AND data/notifications.json (safe fallback/backup)
 - safer template rendering (render_template_safe returns a Response object)
 - drive/sheets integrations are optional (wrapped in try/except)
 - improved logging for template/url_for missing endpoint issues
"""
import os
import json
import time
import logging
import traceback
import sqlite3
import base64
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, make_response, abort, Response
)
from dotenv import load_dotenv

# optional libs
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# optional sheets/drive helpers (if you include sheets.py / drive.py)
sheets_mod = None
try:
    import sheets as sheets_mod  # type: ignore
except Exception:
    sheets_mod = None

load_dotenv()

# app init
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nn_app")

# constants & config
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB).resolve()

CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", str(DATA_DIR / "coding_exercises.json"))
HEALTH_LOCAL_PATH = os.environ.get("HEALTH_DATA_PATH", str(DATA_DIR / "health_data.json"))
QUIZ_LOCAL_PATH = os.environ.get("QUIZ_FORMS_PATH", str(DATA_DIR / "quiz_forms.json"))
NOTIFICATIONS_FILE = os.environ.get("NOTIFICATIONS_FILE", str(DATA_DIR / "notifications.json"))

FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# VAPID / push
VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

# admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", "") or ""
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# ensure DB
def _init_local_db():
    try:
        conn = sqlite3.connect(str(DB_PATH))
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
            )
        """)
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
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_endpoint_delivered ON notifications(endpoint, delivered)")
        conn.commit()
        conn.close()
        logger.info("SQLite DB ready at %s", DB_PATH)
    except Exception:
        logger.exception("DB init failed")

_init_local_db()

# helpers
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

def safe_load_json(path):
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8") or "null")
    except Exception:
        logger.exception("Failed loading JSON from %s", path)
        return None

def safe_write_json(path, obj):
    try:
        p = Path(path)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        logger.exception("Failed writing JSON to %s", path)
        return False

# Template-safe renderer that always returns a Response (avoids Flask tuple pitfalls)
def render_template_safe(template_name, status=200, **kwargs):
    try:
        # try to render
        html = render_template(template_name, **kwargs)
        return make_response(html, status)
    except Exception as e:
        logger.warning("Template %s missing or failed to render: %s", template_name, e)
        # fallback to error.html if present
        try:
            html = render_template("error.html", message=FOOTER_MESSAGE, error=f"Missing template: {template_name}", traceback=traceback.format_exc())
            return make_response(html, 500)
        except Exception:
            # last resort: simple plain text
            body = f"Template missing: {template_name}\n\n{str(e)}\n\n{traceback.format_exc()}"
            return make_response(body, 500)

# Persistence helpers: subscriptions & notifications (SQLite + optional file backup)
def _save_subscription(subject, client_id, subscription_obj) -> bool:
    subject = (subject or "Other").strip()
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
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""REPLACE INTO subscriptions
                         (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (endpoint_norm, subject, client_id or "", p256, auth, json.dumps(subscription_obj), now))
            conn.commit()
        logger.debug("Saved subscription %s", endpoint_norm)
        return True
    except Exception:
        logger.exception("_save_subscription failed")
        return False

def _list_subscriptions(subject=None):
    try:
        out = []
        with sqlite3.connect(str(DB_PATH)) as conn:
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
        logger.exception("_list_subscriptions failed")
        return []

def _delete_subscriptions_by_endpoint(endpoint):
    try:
        if not endpoint:
            return
        normalized = normalize_endpoint(endpoint)
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalized))
            conn.commit()
        logger.debug("Deleted subscription(s) for %s", normalized)
    except Exception:
        logger.exception("_delete_subscriptions_by_endpoint failed")

def save_notification_for_endpoint(endpoint, subject, title, body, data_obj=None, persist_file=True):
    """
    Save to SQLite and optionally append to notifications.json for backup.
    """
    try:
        if not endpoint:
            logger.debug("Not saving notification: missing endpoint")
            return None
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications (endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""",
                      (endpoint_norm, subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        # append to notifications.json backup
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
                logger.exception("Failed to append to notifications backup file")
        return nid
    except Exception:
        logger.exception("save_notification_for_endpoint failed")
        return None

def fetch_pending_notifications_for_endpoint(endpoint):
    try:
        if not endpoint:
            return []
        endpoint_norm = normalize_endpoint(endpoint)
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint_norm,))
            rows = c.fetchall()
        out = []
        for nid, subj, title, body, data_text, created_at in rows:
            try:
                data_obj = json.loads(data_text) if data_text else {}
            except Exception:
                data_obj = {}
            out.append({"id": nid, "subject": subj, "title": title, "body": body, "data": data_obj, "created_at": created_at})
        return out
    except Exception:
        logger.exception("fetch_pending_notifications_for_endpoint failed")
        return []

def ack_notifications_by_ids(endpoint, ids):
    try:
        if not endpoint or not ids:
            return 0
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        changed = 0
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint_norm))
                    if c.rowcount:
                        changed += 1
                except Exception:
                    continue
            conn.commit()
        # reflect in notifications.json (best-effort)
        try:
            arr = safe_load_json(NOTIFICATIONS_FILE) or []
            if isinstance(arr, list):
                for it in arr:
                    if it.get("id") in ids:
                        it["delivered"] = True
                        it["delivered_at"] = now
                safe_write_json(NOTIFICATIONS_FILE, arr)
        except Exception:
            logger.exception("Failed updating notifications backup file")
        return changed
    except Exception:
        logger.exception("ack_notifications_by_ids failed")
        return 0

# push send helper (keeps icon in payload if possible)
def _send_push_to_single(sub, payload):
    if not PYWEBPUSH_AVAILABLE:
        return {"ok": False, "error": "pywebpush-not-available"}
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return {"ok": False, "error": "vapid-not-configured"}
    if not isinstance(sub, dict):
        return {"ok": False, "error": "invalid-sub"}
    try:
        webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
        return {"ok": True}
    except WebPushException as ex:
        status = None
        try:
            status = getattr(ex, "response", None) and getattr(ex.response, "status_code", None)
        except Exception:
            status = None
        return {"ok": False, "error": str(ex), "status": status}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def send_push_to_subject(subject, question_text, attempt_text):
    try:
        subs = _list_subscriptions(subject) or []
    except Exception:
        subs = []
    if not subs:
        return {"sent": 0, "errors": ["no subscribers"], "detail": []}

    title = f"New message in {subject}"
    q_preview = (question_text or "")[:140]
    a_preview = (attempt_text or "")[:180]
    body = f"Q: {q_preview}\nA: {a_preview}"

    # compute logo url if possible
    try:
        logo_url = url_for('static', filename='img/logo.png', _external=True)
    except Exception:
        logo_url = None

    payload_template = {"title": title, "body": body, "url": "/questions", "subject": subject, "icon": logo_url, "badge": logo_url, "preview": {"question": q_preview, "attempt": a_preview}}

    detail = []
    sent = 0

    # persist notifications for offline clients
    for r in subs:
        try:
            sub = r.get("subscription") if isinstance(r, dict) else None
            endpoint = sub.get("endpoint") if sub else None
            save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
        except Exception:
            continue

    if (not PYWEBPUSH_AVAILABLE) or (not VAPID_PRIVATE_KEY):
        for r in subs:
            sub = r.get("subscription") if isinstance(r, dict) else None
            ep = sub.get("endpoint") if sub else None
            detail.append({"endpoint": ep, "ok": False, "error": "sending-disabled"})
        return {"sent": 0, "errors": [d for d in detail if not d.get("ok")], "detail": detail}

    for r in subs:
        try:
            sub = r.get("subscription") if isinstance(r, dict) else None
            if not sub:
                detail.append({"endpoint": None, "ok": False, "error": "no-sub"})
                continue
            endpoint = sub.get("endpoint")
            if not endpoint:
                detail.append({"endpoint": None, "ok": False, "error": "no-endpoint"})
                continue

            payload = dict(payload_template)
            data_bytes = json.dumps(payload).encode("utf-8")
            if len(data_bytes) > 2000:
                payload["preview"] = None
                payload["body"] = payload.get("body","")[:200] + "…"

            ok = False
            for attempt in range(2):
                res = _send_push_to_single(sub, payload)
                if res.get("ok"):
                    ok = True
                    sent += 1
                    detail.append({"endpoint": normalize_endpoint(endpoint), "ok": True})
                    # mark delivered in sqlite
                    try:
                        now = datetime.utcnow().replace(microsecond=0).isoformat()
                        with sqlite3.connect(str(DB_PATH)) as conn:
                            c = conn.cursor()
                            c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, normalize_endpoint(endpoint)))
                            conn.commit()
                    except Exception:
                        logger.exception("Failed to mark delivered for %s", endpoint)
                    break
                else:
                    status = res.get("status")
                    err = res.get("error")
                    logger.warning("Push failure to %s (status=%s): %s", endpoint, status, err)
                    if status in (404, 410) or (err and "unsubscribed" in str(err).lower()):
                        try:
                            _delete_subscriptions_by_endpoint(endpoint)
                        except Exception:
                            logger.exception("Failed removing stale subscription %s", endpoint)
                        detail.append({"endpoint": normalize_endpoint(endpoint), "ok": False, "status": status, "error": err, "deleted": True})
                        ok = False
                        break
                    time.sleep(0.12)
            if not ok:
                detail.append({"endpoint": normalize_endpoint(endpoint), "ok": False, "error": "final-failure"})
        except Exception:
            logger.exception("Exception sending push to subscription row")
            detail.append({"endpoint": None, "ok": False, "error": "processing-exception"})

    return {"sent": sent, "errors": [d for d in detail if not d.get("ok")], "detail": detail}

# --- HTTP Endpoints ---

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
        ok = _save_subscription(subject, client_id, subscription)
        endpoint = subscription.get("endpoint")
        subjects_for_endpoint = []
        try:
            subs = _list_subscriptions()
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
        logger.exception("save-subscription failed: %s", traceback.format_exc())
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return make_response(jsonify({"ok": False, "error": "endpoint required"}), 400)
        out_subjects = []
        try:
            subs = _list_subscriptions()
            for r in subs:
                sub = r.get("subscription") if isinstance(r, dict) else None
                subj = r.get("subject") if isinstance(r, dict) else None
                if sub and subj and normalize_endpoint(sub.get("endpoint")) == normalize_endpoint(endpoint):
                    out_subjects.append((subj or "Other"))
        except Exception:
            logger.exception("subscriptions_check list failed")
        return make_response(jsonify({"ok": True, "subjects": sorted(list(dict.fromkeys(out_subjects)))}), 200)
    except Exception:
        logger.exception("subscriptions_check top-level failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.route("/notifications/pending", methods=["GET"])
def notifications_pending():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return make_response(jsonify({"ok": False, "error": "endpoint required"}), 400)
        items = fetch_pending_notifications_for_endpoint(endpoint)
        return make_response(jsonify({"ok": True, "items": items}), 200)
    except Exception:
        logger.exception("notifications_pending failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

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
        n = ack_notifications_by_ids(endpoint, ids)
        return make_response(jsonify({"ok": True, "count": n}), 200)
    except Exception:
        logger.exception("notifications_ack failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

@app.route("/api/vapid")
def api_vapid():
    if not VAPID_PUBLIC_KEY:
        return make_response(jsonify({"ok": False, "error": "vapid-not-configured"}), 503)
    return make_response(jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY}), 200)

# service worker + static helpers
@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        logger.exception("service-worker not found")
        return abort(404)

@app.route("/favicon.ico")
def favicon():
    # ensure returning a valid response
    favicon_path = Path(app.static_folder) / "img" / "logo.png"
    if favicon_path.exists():
        return send_from_directory(str(favicon_path.parent), favicon_path.name)
    # fallback to icons
    icon_path = Path(app.static_folder) / "icons" / "icon-192.svg"
    if icon_path.exists():
        return send_from_directory(str(icon_path.parent), icon_path.name)
    # last resort: small inline svg
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="100%" height="100%" fill="#2b8cff"/></svg>'
    return make_response(svg, 200, {"Content-Type": "image/svg+xml"})

@app.route("/static/icons/<path:filename>")
def icons_fallback(filename):
    path = Path(app.static_folder) / "icons" / filename
    if path.exists():
        return send_from_directory(str(path.parent), path.name)
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><rect width="100%" height="100%" fill="#2b8cff"/><text x="50%" y="50%" font-size="36" fill="#fff" text-anchor="middle" dominant-baseline="central">NN</text></svg>"""
    resp = make_response(svg)
    resp.headers["Content-Type"] = "image/svg+xml"
    return resp

@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    # try sheets_mod/drive helper if available
    try:
        if sheets_mod and hasattr(sheets_mod, "fetch_drive_image_bytes"):
            data, content_type = sheets_mod.fetch_drive_image_bytes(file_id)
            if data:
                return Response(data, mimetype=content_type)
    except Exception:
        logger.exception("drive_image via sheets_mod failed")
    # fallback to static logo
    path = Path(app.static_folder) / "img" / "logo.png"
    if path.exists():
        return send_from_directory(str(path.parent), path.name)
    return abort(404)

# Pages and routes (ensure quiz_forms exists since templates call it)
@app.route("/")
def home_page():
    return render_template_safe("index.html", message=FOOTER_MESSAGE)

@app.route("/index", endpoint="index")
def index_alias():
    return home_page()

@app.route("/quizzes")
def quizzes_page():
    # legacy path; redirect to quiz_forms if available
    try:
        return redirect(url_for("quiz_forms"))
    except Exception:
        return render_template_safe("quizzes.html", message=FOOTER_MESSAGE)

@app.route("/quiz-forms")
def quiz_forms():
    # provide form_list from data/quiz_forms.json if present
    form_list = safe_load_json(QUIZ_LOCAL_PATH) or []
    return render_template_safe("quiz_forms.html", forms=form_list, message=FOOTER_MESSAGE)

@app.route("/content")
def content_page():
    """
    Robust content loader: uses data JSON files where present and falls back to empty lists.
    """
    try:
        timetable_today = []
        sheet_title = None
        sheet_content = None
        # gallery/dr_values and form_list from data files if present
        dr_values = []
        form_list = safe_load_json(QUIZ_LOCAL_PATH) or []
        hero_id = os.environ.get("HERO_IMAGE_ID", "")
        notes_drive_url = os.environ.get("NOTES_DRIVE_URL", "")
        # if health_data file exists, load to pass to templates
        health_items = safe_load_json(HEALTH_LOCAL_PATH) or []
        # try sheets_mod optional helpers
        if sheets_mod:
            try:
                if hasattr(sheets_mod, "get_timetable_for_today"):
                    timetable_today = sheets_mod.get_timetable_for_today() or []
                if hasattr(sheets_mod, "get_timetable_sheet_content"):
                    sheet_title, sheet_content = sheets_mod.get_timetable_sheet_content() or (None, None)
                if hasattr(sheets_mod, "list_gallery"):
                    dr_values = sheets_mod.list_gallery() or dr_values
            except Exception:
                logger.exception("sheets_mod helpers failed for content_page; continuing with local data")
        return render_template_safe("content.html",
                                    message=FOOTER_MESSAGE,
                                    timetable_today=timetable_today,
                                    sheet_title=sheet_title,
                                    sheet_content=sheet_content,
                                    dr_values=dr_values,
                                    form_list=form_list,
                                    notes_drive_url=notes_drive_url,
                                    hero_image_id=hero_id,
                                    hero_title=os.environ.get("HERO_TITLE", "Stay Consistent"),
                                    hero_subtitle=os.environ.get("HERO_SUBTITLE", "Success = small efforts repeated daily."))
    except Exception:
        logger.exception("content_page failed")
        return render_template_safe("content.html", message=FOOTER_MESSAGE)

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
            try:
                # save to data/questions_local.json
                qfile = Path(DATA_DIR) / "questions_local.json"
                arr = json.loads(qfile.read_text(encoding="utf-8")) if qfile.exists() else []
                arr.append([category, name, question, attempt, timestamp])
                qfile.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("Failed appending question; continuing")
            flash("Question submitted. Thank you!", "success")
            try:
                send_push_to_subject(category, question, attempt)
            except Exception:
                logger.exception("send_push_to_subject failed")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite='Lax')
                resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite='Lax')
            except Exception:
                pass
            return resp

        # GET
        qfile = Path(DATA_DIR) / "questions_local.json"
        raw = json.loads(qfile.read_text(encoding="utf-8") or "[]") if qfile.exists() else []
        questions = []
        for r in (raw or []):
            try:
                row = list(r)
                while len(row) < 5:
                    row.append("")
                if len(row) > 0 and str(row[0]).strip() == "__subscription__":
                    continue
                questions.append(row)
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
        logger.exception("questions_page exception")
        flash("Could not load questions", "danger")
        return render_template_safe("questions.html", questions=[], categories=["Other"], selected_category="", message=FOOTER_MESSAGE)

@app.route("/coding")
def coding_page():
    exercises = safe_load_json(CODING_LOCAL_PATH) or []
    return render_template_safe("coding.html", exercises=exercises, message=FOOTER_MESSAGE)

@app.route("/quiz_forms")
def quiz_forms():
    # wrapper that returns the template; ensure it's accessible by url_for('quiz_forms')
    form_list = safe_load_json(QUIZ_LOCAL_PATH) or []
    return render_template_safe("quiz_forms.html", forms=form_list, message=FOOTER_MESSAGE)

# admin pages (announcement, exercises, questions manager)
@app.route("/admin/announcement", methods=["GET", "POST"])
def admin_announcement():
    provided_key = request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or ""
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
            if subject.lower() in ("all", "everyone", "any"):
                subs = _list_subscriptions()
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
                        save_notification_for_endpoint(sub.get("endpoint"), subject, title, body, {"announcement": True})
                        if PYWEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY:
                            res = _send_push_to_single(sub, {"title": title, "body": body, "url": "/"})
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
        target = request.form.get("target") or "local"
        try:
            if target == "drive" and sheets_mod and hasattr(sheets_mod, "upload_coding_exercises"):
                sheets_mod.upload_coding_exercises(data)
                flash("Saved to Drive file (via sheets_mod).", "success")
            else:
                path = Path(CODING_LOCAL_PATH)
                arr = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                arr = [e for e in arr if str(e.get("id")) != str(data.get("id"))]
                arr.append(data)
                path.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
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

    qfile = Path(DATA_DIR) / "questions_local.json"
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

# exercise-table and admin-run-sql (unchanged safe impl)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
def exercise_table(name):
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
        logger.exception("exercise_table execution error")
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)

# admin debug endpoints
@app.route("/admin/debug/subscriptions")
def admin_debug_subs():
    key = request.args.get("key") or ""
    if key != ADMIN_KEY:
        return make_response(jsonify({"ok": False, "error": "unauthorized"}), 401)
    subs = _list_subscriptions()
    return make_response(jsonify({"ok": True, "count": len(subs), "subscriptions": subs}), 200)

@app.route("/admin/debug/notifications")
def admin_debug_notifications():
    key = request.args.get("key") or ""
    if key != ADMIN_KEY:
        return make_response(jsonify({"ok": False, "error": "unauthorized"}), 401)
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT id, endpoint, subject, title, body, data, created_at, delivered, delivered_at FROM notifications ORDER BY created_at DESC LIMIT 200")
            rows = c.fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "endpoint": r[1], "subject": r[2], "title": r[3], "body": r[4],
                "data": json.loads(r[5]) if r[5] else {}, "created_at": r[6], "delivered": bool(r[7]), "delivered_at": r[8]
            })
        return make_response(jsonify({"ok": True, "count": len(out), "notifications": out}), 200)
    except Exception:
        logger.exception("admin_debug_notifications failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

# error handlers that return Responses
@app.errorhandler(404)
def handle_404(e):
    try:
        return render_template_safe("errors/404.html", message=FOOTER_MESSAGE, status=404)
    except Exception:
        return render_template_safe("error.html", message=FOOTER_MESSAGE, error=str(e))

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    if request.path.startswith("/api/") or request.path.startswith("/admin/run-sql") or request.is_json:
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)
    return render_template_safe("error.html", message=FOOTER_MESSAGE, error=str(e))

# on startup: log which templates are present (for debugging)
def _log_template_inventory():
    needed = [
        "base.html", "index.html", "content.html", "questions.html", "quiz_forms.html",
        "coding.html", "health.html", "health_detail.html", "rules.html",
        "error.html", "admin_announcement.html", "admin_questions.html", "errors/404.html",
        "admin_exercises.html"
    ]
    present = []
    missing = []
    for t in needed:
        try:
            app.jinja_env.get_or_select_template(t)
            present.append(t)
        except Exception:
            missing.append(t)
    logger.info("Templates present: %s", present)
    if missing:
        logger.warning("Templates missing (render_template_safe will fallback): %s", missing)

_log_template_inventory()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    logger.info("Starting app on port %s (debug=%s)", port, debug_mode)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)