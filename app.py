# app.py — PART 1/5
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

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, make_response, abort
)
from dotenv import load_dotenv

# Optional push support
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# load .env if present
load_dotenv()

# App init
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
_secret = os.environ.get("FLASK_SECRET_KEY")
if _secret:
    app.secret_key = _secret
else:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nn_app")

# Constants
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Service account support (optional base64)
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
if SERVICE_ACCOUNT_B64:
    try:
        data = base64.b64decode(SERVICE_ACCOUNT_B64)
        json.loads(data.decode("utf-8"))  # validate
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
        logger.info("Wrote service account JSON to %s", SERVICE_ACCOUNT_PATH)
    except Exception:
        logger.exception("Failed to write service account JSON from env.")

# VAPID keys for push (optional)
VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()
if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
    logger.info("VAPID keys not fully configured. Push sending will be disabled until both are provided.")

# Admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# Optional Sheets keys (if you supply sheets.py later)
QUESTIONS_SUBSCRIPTION_KEY = (os.environ.get("QUESTIONS_SUBSCRIPTION_KEY") or "").strip()
QUESTIONS_SHEET_KEY = (os.environ.get("QUESTIONS_SHEET_KEY") or "").strip()
TIMETABLE_SHEET_KEY = (os.environ.get("TIMETABLE_SHEET_KEY") or "").strip()
GROUP_RULES_SHEET_KEY = (os.environ.get("GROUP_RULES_SHEET_KEY") or "").strip()

# coding exercises files
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = (os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID") or "").strip()

# Data dir and DB path
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB).resolve()

# Try to import sheets.py if the repo includes one (optional)
SHEETS_AVAILABLE = False
sheets_mod = None
try:
    import sheets as sheets_mod_candidate
    sheets_mod = sheets_mod_candidate
    if all(hasattr(sheets_mod, fn) for fn in ("save_subscription", "list_subscriptions", "get_questions", "append_row")):
        SHEETS_AVAILABLE = True
        logger.info("sheets.py loaded — Sheets-backed storage enabled where configured.")
    else:
        logger.info("sheets.py loaded but missing required helpers; falling back to local storage.")
        SHEETS_AVAILABLE = False
except Exception:
    sheets_mod = None
    SHEETS_AVAILABLE = False
    logger.debug("sheets.py not present; using local sqlite / data/ JSON fallback.")
# app.py — PART 2/5 (local DB init, helpers)
def _init_local_db():
    """Ensure local sqlite DB and tables exist."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        logger.info("Local SQLite DB ensured at %s", str(DB_PATH))
    except Exception:
        logger.exception("Failed initializing local DB")

_init_local_db()

# helpers
def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    try:
        u = urlparse(endpoint)
        scheme = u.scheme or "https"
        netloc = u.netloc or u.path.split("/")[0]
        path = u.path or ""
        return f"{scheme}://{netloc}{path}".rstrip("/")
    except Exception:
        return endpoint.strip()

def _is_valid_subscription_obj(sub):
    if not sub or not isinstance(sub, dict):
        return False
    ep = sub.get("endpoint") or sub.get("url")
    if not ep or not isinstance(ep, str):
        return False
    return True

def _normalize_subject(s):
    return (s or "Other").strip()

# wrapper to safely render templates (avoid TemplateNotFound crashes)
def render_template_safe(template_name, **kwargs):
    try:
        # verify template exists by trying to load it
        app.jinja_env.get_or_select_template(template_name)
        return render_template(template_name, **kwargs)
    except Exception as e:
        logger.warning("Template %s missing or failed to render: %s", template_name, e)
        # If error.html exists, render it; else return plain fallback.
        try:
            app.jinja_env.get_or_select_template("error.html")
            return render_template("error.html", message=FOOTER_MESSAGE, error=f"Missing template: {template_name}"), 500
        except Exception:
            return f"<h1>Template missing: {template_name}</h1><pre>{str(e)}</pre>", 500
# app.py — PART 3/5 (subscriptions + notifications persistence + listing)
# Save subscription preferentially to Sheets (if configured), else to sqlite
def _save_subscription(subject, client_id, subscription_obj):
    subject = _normalize_subject(subject)
    try:
        # prefer Sheets if configured
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                sheets_mod.save_subscription(subject, client_id, subscription_obj)
                logger.debug("Saved subscription to Sheets for subject %s", subject)
                return True
            except Exception:
                logger.exception("sheets.save_subscription failed, falling back to sqlite")

        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}

        endpoint = subscription_obj.get("endpoint")
        if not endpoint:
            logger.warning("Subscription missing endpoint; skipping save")
            return False

        keys = subscription_obj.get("keys", {}) if isinstance(subscription_obj, dict) else {}
        p256 = keys.get("p256dh", "")
        auth = keys.get("auth", "")
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""REPLACE INTO subscriptions
                         (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (endpoint, subject, client_id or "", p256, auth, json.dumps(subscription_obj), now))
            conn.commit()
        logger.debug("Saved subscription to sqlite for %s", endpoint)
        return True
    except Exception:
        logger.exception("_save_subscription failed")
        return False

def _list_subscriptions(subject=None):
    try:
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                rows = sheets_mod.list_subscriptions(subject)
                out = []
                for r in (rows or []):
                    if isinstance(r, dict) and r.get("subscription"):
                        out.append({
                            "subject": r.get("subject"),
                            "client_id": r.get("client_id"),
                            "subscription": r.get("subscription"),
                            "created_at": r.get("created_at")
                        })
                return out
            except Exception:
                logger.exception("sheets.list_subscriptions failed; falling back to sqlite")

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
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                sheets_mod.delete_subscriptions_by_endpoint(endpoint)
                logger.debug("Deleted subscription from sheets for endpoint %s", endpoint)
                return
            except Exception:
                logger.exception("sheets.delete_subscriptions_by_endpoint failed; falling back to sqlite")
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalize_endpoint(endpoint)))
            conn.commit()
        logger.debug("Deleted local subscription(s) for endpoint %s", endpoint)
    except Exception:
        logger.exception("_delete_subscriptions_by_endpoint failed")

# notifications persistence
def save_notification_for_endpoint(endpoint, subject, title, body, data_obj=None):
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications (endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""",
                      (endpoint or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        return nid
    except Exception:
        logger.exception("save_notification_for_endpoint failed")
        return None

def fetch_pending_notifications_for_endpoint(endpoint):
    try:
        if not endpoint:
            return []
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT id, subject, title, body, data, created_at FROM notifications WHERE endpoint=? AND delivered=0 ORDER BY created_at ASC", (endpoint,))
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
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        changed = 0
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            for nid in ids:
                try:
                    c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE id=? AND endpoint=?", (now, nid, endpoint))
                    if c.rowcount:
                        changed += 1
                except Exception:
                    continue
            conn.commit()
        return changed
    except Exception:
        logger.exception("ack_notifications_by_ids failed")
        return 0
# app.py — PART 4/5 (push send, subscription endpoints, notifications API)
# low-level send single push (guarded)
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
            resp = getattr(ex, "response", None)
            status = getattr(resp, "status_code", None) if resp else None
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
    payload_template = {"title": title, "body": body, "url": "/questions", "subject": subject, "preview": {"question": q_preview, "attempt": a_preview}}

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

    # if sending disabled, return persisted info only
    if (not PYWEBPUSH_AVAILABLE) or (not VAPID_PRIVATE_KEY):
        for r in subs:
            try:
                sub = r.get("subscription") if isinstance(r, dict) else None
                ep = sub.get("endpoint") if sub else None
            except Exception:
                ep = None
            detail.append({"endpoint": ep, "ok": False, "error": "sending-disabled"})
        return {"sent": 0, "errors": ["sending-disabled"], "detail": detail}

    # attempt delivery
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
            parsed = urlparse(endpoint)
            domain = parsed.netloc.lower() if parsed.netloc else ""
            if "windows" in domain or "wns" in domain:
                payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}

            data_bytes = json.dumps(payload).encode("utf-8")
            if len(data_bytes) > 2000:
                payload["preview"] = None
                payload["body"] = (payload.get("body","")[:200] + "…")

            ok = False
            for attempt in range(2):
                res = _send_push_to_single(sub, payload)
                if res.get("ok"):
                    ok = True
                    sent += 1
                    detail.append({"endpoint": endpoint, "ok": True})
                    try:
                        now = datetime.utcnow().replace(microsecond=0).isoformat()
                        with sqlite3.connect(str(DB_PATH)) as conn:
                            c = conn.cursor()
                            c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint))
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
                        detail.append({"endpoint": endpoint, "ok": False, "status": status, "error": err, "deleted": True})
                        ok = False
                        break
                    time.sleep(0.12)
            if not ok:
                detail.append({"endpoint": endpoint, "ok": False, "error": "final-failure"})
        except Exception:
            logger.exception("Exception sending push to subscription row")
            detail.append({"endpoint": None, "ok": False, "error": "processing-exception"})

    return {"sent": sent, "errors": [d for d in detail if not d.get("ok")], "detail": detail}

# HTTP endpoints for subscription + notifications
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
        if not subscription or not _is_valid_subscription_obj(subscription):
            return jsonify({"ok": False, "error": "subscription required"}), 400
        ok = _save_subscription(subject, client_id, subscription)
        endpoint = subscription.get("endpoint")
        subjects_for_endpoint = []
        try:
            subs = _list_subscriptions()
            for r in subs:
                try:
                    sub = r.get("subscription") if isinstance(r, dict) else None
                    subj = r.get("subject") if isinstance(r, dict) else None
                    if sub and sub.get("endpoint") == endpoint and subj:
                        subjects_for_endpoint.append(_normalize_subject(subj))
                except Exception:
                    continue
            subjects_for_endpoint = sorted(list(dict.fromkeys(subjects_for_endpoint)))
        except Exception:
            subjects_for_endpoint = []
        resp = jsonify({"ok": ok, "subjects": subjects_for_endpoint})
        try:
            if endpoint:
                resp.set_cookie('nn_subscription', endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax')
        except Exception:
            pass
        return resp, (200 if ok else 500)
    except Exception:
        logger.exception("save-subscription failed: %s", traceback.format_exc())
        return jsonify({"ok": False, "error": "server-error"}), 500

@app.route("/subscriptions/check", methods=["GET"])
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        out_subjects = []
        try:
            subs = _list_subscriptions()
            for r in subs:
                try:
                    sub = r.get("subscription") if isinstance(r, dict) else None
                    subj = r.get("subject") if isinstance(r, dict) else None
                    if sub and sub.get("endpoint") == endpoint and subj:
                        out_subjects.append(_normalize_subject(subj))
                except Exception:
                    continue
        except Exception:
            logger.exception("subscriptions_check list failed")
        return jsonify({"ok": True, "subjects": sorted(list(dict.fromkeys(out_subjects)))})
    except Exception:
        logger.exception("subscriptions_check top-level failed")
        return jsonify({"ok": False, "error": "server-error"}), 500

@app.route("/notifications/pending", methods=["GET"])
def notifications_pending():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        items = fetch_pending_notifications_for_endpoint(endpoint)
        return jsonify({"ok": True, "items": items})
    except Exception:
        logger.exception("notifications_pending failed")
        return jsonify({"ok": False, "error": "server-error"}), 500

@app.route("/notifications/ack", methods=["POST"])
def notifications_ack():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        endpoint = (payload.get("endpoint") or "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        ids = payload.get("ids") or []
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        if not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids must be a list"}), 400
        n = ack_notifications_by_ids(endpoint, ids)
        return jsonify({"ok": True, "count": n})
    except Exception:
        logger.exception("notifications_ack failed")
        return jsonify({"ok": False, "error": "server-error"}), 500

# expose vapid public key
@app.route("/api/vapid")
def api_vapid():
    if not VAPID_PUBLIC_KEY:
        return jsonify({"ok": False, "error": "vapid-not-configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})
# app.py — PART 5/5 (pages, exercise runner, admin announcement, error handlers, run)
# Serve service worker if present
@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        logger.exception("service-worker not found")
        abort(404)

# Core pages (ensure endpoints referenced in templates)
@app.route("/")
def home_page():
    return render_template_safe("index.html", message=FOOTER_MESSAGE)

# alias for templates using url_for('index')
@app.route("/index", endpoint="index")
def index_alias():
    try:
        return home_page()
    except Exception:
        logger.exception("index_alias failed")
        return render_template_safe("index.html", message=FOOTER_MESSAGE)

@app.route("/content")
def content_page():
    return render_template_safe("content.html", message=FOOTER_MESSAGE)

# quizzes list page and quiz forms
@app.route("/quizzes")
def quizzes_page():
    return render_template_safe("quizzes.html", message=FOOTER_MESSAGE)

@app.route("/quiz-forms")
def quiz_forms():
    # ensure endpoint name used in templates is 'quiz_forms'
    return render_template_safe("quiz_forms.html", message=FOOTER_MESSAGE)

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
                if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
                    sheets_mod.append_row([category, name, question, attempt, timestamp])
                else:
                    qpath = DATA_DIR / "questions_local.json"
                    try:
                        arr = json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else []
                    except Exception:
                        arr = []
                    arr.append([category, name, question, attempt, timestamp])
                    qpath.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("Failed appending question; continuing")

            flash("Question submitted. Thank you!", "success")
            try:
                send_push_to_subject(category, question, attempt)
            except Exception:
                logger.exception("send_push_to_subject failed")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite="Lax")
                resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite="Lax")
            except Exception:
                pass
            return resp

        # GET
        raw = []
        try:
            if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
                raw = sheets_mod.get_questions()
            else:
                qpath = DATA_DIR / "questions_local.json"
                raw = json.loads(qpath.read_text(encoding="utf-8") or "[]") if qpath.exists() else []
        except Exception:
            logger.exception("Failed loading questions")
            raw = []

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
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "")
        if selected_category:
            questions = [q for q in questions if str(q[0]).strip() == selected_category]
        return render_template_safe("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception:
        logger.exception("questions_page exception")
        flash("Could not load questions", "danger")
        return render_template_safe("questions.html", questions=[], categories=STATIC_CATEGORIES, selected_category="", message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)

@app.route("/coding")
def coding_page():
    exercises = []
    try:
        p = Path(CODING_LOCAL_PATH)
        if p.exists():
            exercises = json.loads(p.read_text(encoding="utf-8") or "[]")
    except Exception:
        logger.exception("Failed loading coding_exercises.json")
    return render_template_safe("coding.html", exercises=exercises, message=FOOTER_MESSAGE)

@app.route("/health")
def health_index():
    return render_template_safe("health.html", message=FOOTER_MESSAGE)

@app.route("/health/<path:slug>")
def health_detail(slug):
    # Render health_detail.html with slug; templates should load content by slug or show fallback
    return render_template_safe("health_detail.html", slug=slug, message=FOOTER_MESSAGE)

@app.route("/rules")
def rules_page():
    rules = []
    try:
        if SHEETS_AVAILABLE and GROUP_RULES_SHEET_KEY:
            rules = sheets_mod.read_rules()
    except Exception:
        logger.exception("Failed reading group rules")
    return render_template_safe("rules.html", rules=rules, message=FOOTER_MESSAGE)

@app.route("/admin/questions")
def admin_questions():
    return render_template_safe("admin_questions.html", message=FOOTER_MESSAGE)

# admin announcement route (protected by ADMIN_KEY)
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
                    try:
                        sub = r.get("subscription") if isinstance(r, dict) else None
                        if not sub:
                            continue
                        ep = sub.get("endpoint")
                        if ep and ep not in uniq:
                            uniq[ep] = sub
                    except Exception:
                        continue
                for sub in uniq.values():
                    try:
                        save_notification_for_endpoint(sub.get("endpoint"), subject, title, body, {"announcement": True})
                        if PYWEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY:
                            res = _send_push_to_single(sub, {"title": title, "body": body, "url": "/"})
                            results.append({"endpoint": sub.get("endpoint"), **res})
                        else:
                            results.append({"endpoint": sub.get("endpoint"), "ok": False, "error": "push-not-configured"})
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

# Safe multi-statement SQL runner helper
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
        except Exception:
            # fallback split
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

# exercise-table route (accepts POST - form or JSON body)
@app.route("/exercise-table/<path:name>", methods=["POST", "GET"])
def exercise_table(name):
    if request.method == "GET":
        return jsonify({"ok": True, "endpoint": f"/exercise-table/{name}", "note": "POST SQL to this URL using form-data or JSON with key 'schema'"}), 200

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
        return jsonify({"ok": False, "error": "schema (SQL) required in 'schema' form field or JSON body"}), 400

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
        return jsonify({"ok": True, "name": name, "tables": tables, "preview": preview}), 200
    except Exception as e:
        logger.exception("exercise_table execution error")
        return jsonify({"ok": False, "error": str(e)}), 500

# admin-run-sql endpoint
@app.route("/admin/run-sql", methods=["POST"])
def admin_run_sql():
    provided = request.args.get("key") or request.form.get("admin_key") or ""
    if provided != EXERCISES_ADMIN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    schema = payload.get("schema") or payload.get("sql") or ""
    sample = payload.get("sample") or {}
    try:
        conn, tables = _safe_create_and_populate_sqlite(schema, sample)
        rows_preview = {}
        for t in tables:
            try:
                cur = conn.execute(f"SELECT * FROM \"{t}\" LIMIT 20")
                rows_preview[t] = [dict(row) for row in cur.fetchall()]
            except Exception:
                rows_preview[t] = []
        conn.close()
        return jsonify({"ok": True, "tables": tables, "preview": rows_preview})
    except Exception:
        logger.exception("admin_run_sql failed")
        return jsonify({"ok": False, "error": "execution-failed"}), 500

# Error handlers
@app.errorhandler(404)
def handle_404(e):
    try:
        return render_template_safe("errors/404.html", message=FOOTER_MESSAGE), 404
    except Exception:
        return render_template_safe("error.html", message=FOOTER_MESSAGE, error=str(e)), 404

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    if request.path.startswith("/api/") or request.path.startswith("/admin/run-sql") or request.is_json:
        return jsonify({"ok": False, "error": str(e)}), 500
    return render_template_safe("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

# At startup: log which templates are present / missing (helps debugging)
def _log_template_inventory():
    needed = [
        "base.html", "index.html", "content.html", "questions.html", "quiz_forms.html",
        "quizzes.html", "coding.html", "health.html", "health_detail.html", "rules.html",
        "error.html", "admin_announcement.html", "admin_questions.html", "errors/404.html"
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

# Run
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    logger.info("Starting app on port %s (debug=%s)", port, debug_mode)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)