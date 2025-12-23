# app.py — PART 1/4
import os
import re
import json
import time
import logging
import traceback
import sqlite3
import base64
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, make_response, abort
)
from dotenv import load_dotenv

# optional: pywebpush for push sending
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# optional requests for drive image proxy / fetching drive content
try:
    import requests
except Exception:
    requests = None

# Load environment variables from .env if present
load_dotenv()

# App init
app = Flask(__name__, static_folder="static", template_folder="templates")
_secret = os.environ.get("FLASK_SECRET_KEY")
if _secret:
    app.secret_key = _secret
else:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nn_app")

# Constants / config (safe defaults)
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Service account support (base64 or path)
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
        app.logger.info("Wrote service account JSON to %s", SERVICE_ACCOUNT_PATH)
    except Exception:
        app.logger.exception("Failed to write service account JSON from env.")

# VAPID config for web push
VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
    logger.warning("VAPID keys not set — push sending disabled until both keys are provided.")

# Admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# Sheets keys (optional)
QUESTIONS_SUBSCRIPTION_KEY = (os.environ.get("QUESTIONS_SUBSCRIPTION_KEY") or "").strip()
QUESTIONS_SHEET_KEY = (os.environ.get("QUESTIONS_SHEET_KEY") or "").strip()
TIMETABLE_SHEET_KEY = (os.environ.get("TIMETABLE_SHEET_KEY") or "").strip()
GROUP_RULES_SHEET_KEY = (os.environ.get("GROUP_RULES_SHEET_KEY") or "").strip()

# coding exercises config
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = (os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID") or "").strip()

# Local SQLite DB path (fallback / local persistent store)
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB).resolve()

# data dir for JSON fallbacks (admin_notifications.json etc)
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True, parents=True)
ADMIN_NOTIF_FILE = DATA_DIR / "admin_notifications.json"
USER_STATES_FILE = DATA_DIR / "user_states.json"

# Optional external modules: sheets.py (if provided)
SHEETS_AVAILABLE = False
sheets_mod = None
try:
    import sheets as sheets_mod_candidate
    sheets_mod = sheets_mod_candidate
    # check that functions we need exist (best-effort)
    if all(hasattr(sheets_mod, fn) for fn in ("save_subscription", "list_subscriptions", "get_questions", "append_row")):
        SHEETS_AVAILABLE = True
        app.logger.info("sheets.py loaded and will be used for Sheets-backed operations.")
    else:
        app.logger.warning("sheets.py loaded but missing some functions; falling back to local storage where needed.")
        SHEETS_AVAILABLE = False
except Exception:
    sheets_mod = None
    SHEETS_AVAILABLE = False
    app.logger.debug("sheets.py not available; using SQLite / local JSON fallback.")
# app.py — PART 2/4
# ensure sqlite DB created
def _init_local_db():
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        # subscriptions table
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
        # (legacy) notifications persistence table left for compatibility but not required
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
        app.logger.info("Local SQLite DB ensured at %s", str(DB_PATH))
    except Exception:
        app.logger.exception("Failed initializing local DB")

_init_local_db()

# small helpers
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

# Subscription storage helpers (Sheets preferred, SQLite fallback)
def _save_subscription(subject, client_id, subscription_obj):
    """
    Save a subscription. Prefer using sheets_mod.save_subscription when available and
    QUESTIONS_SUBSCRIPTION_KEY is set. Otherwise upsert into local sqlite subscriptions table.
    """
    subject = _normalize_subject(subject)
    try:
        # prefer Sheets if available and configured
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                sheets_mod.save_subscription(subject, client_id, subscription_obj)
                app.logger.debug("Saved subscription to Sheets for subject %s", subject)
                return True
            except Exception:
                app.logger.exception("sheets.save_subscription failed; falling back to sqlite")

        # normalize subscription object
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}

        endpoint = subscription_obj.get("endpoint")
        if not endpoint:
            app.logger.warning("Subscription missing endpoint, skipping save")
            return False

        keys = subscription_obj.get("keys", {}) if isinstance(subscription_obj, dict) else {}
        p256 = keys.get("p256dh", "")
        auth = keys.get("auth", "")
        now = datetime.utcnow().replace(microsecond=0).isoformat()

        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            # REPLACE will insert or overwrite existing endpoint row
            c.execute("""REPLACE INTO subscriptions (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (endpoint, subject, client_id or "", p256, auth, json.dumps(subscription_obj), now))
            conn.commit()
        app.logger.debug("Saved subscription to local sqlite for subject %s endpoint %s", subject, endpoint)
        return True
    except Exception:
        app.logger.exception("_save_subscription failed")
        return False

def _list_subscriptions(subject=None):
    """
    Return a list of dicts with keys: subject, client_id, subscription, created_at
    If sheets is configured, attempt to read from it first.
    """
    try:
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                rows = sheets_mod.list_subscriptions(subject)
                # normalize to expected dict format
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
                app.logger.exception("sheets.list_subscriptions failed; falling back to sqlite")

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
        app.logger.exception("_list_subscriptions failed")
        return []

def _delete_subscriptions_by_endpoint(endpoint):
    try:
        if not endpoint:
            return
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                sheets_mod.delete_subscriptions_by_endpoint(endpoint)
                app.logger.debug("Deleted subscription from sheets for endpoint %s", endpoint)
                return
            except Exception:
                app.logger.exception("sheets.delete_subscriptions_by_endpoint failed; falling back to sqlite")
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalize_endpoint(endpoint)))
            conn.commit()
        app.logger.debug("Deleted subscription(s) from sqlite for endpoint %s", endpoint)
    except Exception:
        app.logger.exception("_delete_subscriptions_by_endpoint failed")
# app.py — PART 3/4
# ADMIN notifications stored as JSON in /data/admin_notifications.json
def _load_admin_notifications():
    try:
        if not ADMIN_NOTIF_FILE.exists():
            ADMIN_NOTIF_FILE.write_text("[]", encoding="utf-8")
            return []
        data = json.loads(ADMIN_NOTIF_FILE.read_text(encoding="utf-8") or "[]")
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        logger.exception("_load_admin_notifications failed")
        return []

def _save_admin_notifications(notifs):
    try:
        tmp = ADMIN_NOTIF_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(notifs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ADMIN_NOTIF_FILE)
        return True
    except Exception:
        logger.exception("_save_admin_notifications failed")
        return False

def _add_admin_notification(title, body, meta=None):
    try:
        notifs = _load_admin_notifications()
        next_id = 1
        if notifs:
            try:
                next_id = max(int(n.get("id", 0)) for n in notifs) + 1
            except Exception:
                next_id = (len(notifs) + 1)
        item = {
            "id": int(next_id),
            "title": title,
            "body": body,
            "meta": meta or {},
            "created_at": datetime.utcnow().replace(microsecond=0).isoformat()
        }
        notifs.append(item)
        _save_admin_notifications(notifs)
        return item
    except Exception:
        logger.exception("_add_admin_notification failed")
        return None

# user_states.json maps endpoint -> last_seen_id (int)
def _load_user_states():
    try:
        if not USER_STATES_FILE.exists():
            USER_STATES_FILE.write_text("{}", encoding="utf-8")
            return {}
        data = json.loads(USER_STATES_FILE.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        logger.exception("_load_user_states failed")
        return {}

def _save_user_states(states):
    try:
        tmp = USER_STATES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(USER_STATES_FILE)
        return True
    except Exception:
        logger.exception("_save_user_states failed")
        return False

def _get_pending_admin_notifications_for_endpoint(endpoint):
    if not endpoint:
        return []
    try:
        notifs = _load_admin_notifications()
        states = _load_user_states()
        last_seen = int(states.get(endpoint, 0))
        pending = [n for n in notifs if int(n.get("id", 0)) > last_seen]
        return pending
    except Exception:
        logger.exception("_get_pending_admin_notifications_for_endpoint failed")
        return []

def _ack_admin_notifications_for_endpoint(endpoint, ids=None):
    """
    If ids provided, set last_seen to max(ids)
    Otherwise mark all as seen (set last_seen to last notification id)
    """
    try:
        if not endpoint:
            return False
        notifs = _load_admin_notifications()
        if not notifs:
            return True
        states = _load_user_states()
        if ids and isinstance(ids, list) and ids:
            try:
                maxid = max(int(i) for i in ids if isinstance(i, (int, str)) and str(i).strip().isdigit())
            except Exception:
                maxid = 0
        else:
            try:
                maxid = max(int(n.get("id", 0)) for n in notifs)
            except Exception:
                maxid = 0
        if maxid <= 0:
            return True
        states[endpoint] = int(maxid)
        return _save_user_states(states)
    except Exception:
        logger.exception("_ack_admin_notifications_for_endpoint failed")
        return False

# send push to all subscriptions (used for admin announcements)
def _send_push_to_all(payload):
    results = []
    try:
        subs = _list_subscriptions() or []
    except Exception:
        subs = []
    if not subs:
        return {"sent": 0, "detail": []}
    sent = 0
    for r in subs:
        try:
            sub = r.get("subscription")
            if not sub:
                continue
            res = _send_push_to_single(sub, payload)
            ok = res.get("ok")
            if ok:
                sent += 1
            results.append({"endpoint": sub.get("endpoint"), **res})
            # if stale and server signaled deletion, remove subscription
            if res.get("status") in (404, 410) or ("unsubscribed" in str(res.get("error", "")).lower()):
                try:
                    _delete_subscriptions_by_endpoint(sub.get("endpoint"))
                except Exception:
                    pass
        except Exception:
            logger.exception("failed sending push to row")
            results.append({"endpoint": None, "ok": False, "error": "exception"})
    return {"sent": sent, "detail": results}

# --------- Push utilities (single sender) ----------
def _send_push_to_single(sub, payload):
    """
    Try to send a push to a single subscription dict using pywebpush.
    Returns dict describing result. Errors are captured but not raised.
    """
    if not PYWEBPUSH_AVAILABLE:
        return {"ok": False, "error": "pywebpush-not-available"}
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return {"ok": False, "error": "vapid-not-configured"}

    if not isinstance(sub, dict):
        return {"ok": False, "error": "invalid-sub"}

    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT}
        )
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
# app.py — PART 4/4
# -------------------------
# HTTP endpoints for client JS (subscriptions + notifications)
# -------------------------
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
        # return list of subjects for endpoint
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
        items = _get_pending_admin_notifications_for_endpoint(endpoint)
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
        # If ids is provided, ack those; otherwise mark all as seen
        ok = _ack_admin_notifications_for_endpoint(endpoint, ids if isinstance(ids, list) else None)
        return jsonify({"ok": True, "result": ok})
    except Exception:
        logger.exception("notifications_ack failed")
        return jsonify({"ok": False, "error": "server-error"}), 500

# route to expose vapid public key to browser (name kept for compatibility)
@app.route("/api/vapid")
def api_vapid():
    if not VAPID_PUBLIC_KEY:
        return jsonify({"ok": False, "error": "vapid-not-configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})

# Serve service-worker file from static (if present)
@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        logger.exception("service-worker not found")
        abort(404)

# -------- core pages ----------
@app.route("/")
def home_page():
    return render_template("index.html", message=FOOTER_MESSAGE)

@app.route("/content")
def content_page():
    return render_template("content.html", message=FOOTER_MESSAGE)

@app.route("/quizzes")
def quiz_forms():
    return render_template("quizzes.html", message=FOOTER_MESSAGE)

@app.route("/coding")
def coding_page():
    # load coding exercises from local file (or Google Drive if implemented)
    exercises = []
    try:
        p = Path(CODING_LOCAL_PATH)
        if p.exists():
            exercises = json.loads(p.read_text(encoding="utf-8") or "[]")
    except Exception:
        logger.exception("Failed loading local coding_exercises.json")
        exercises = []
    return render_template("coding.html", exercises=exercises, message=FOOTER_MESSAGE)

@app.route("/health")
def health_index():
    return render_template("health.html", message=FOOTER_MESSAGE)

@app.route("/rules")
def rules_page():
    rules = []
    try:
        if SHEETS_AVAILABLE and GROUP_RULES_SHEET_KEY:
            try:
                rules = sheets_mod.read_rules()
            except Exception:
                logger.exception("Failed reading group rules from sheets")
                rules = []
    except Exception:
        rules = []
    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)

# Questions route: GET shows list, POST appends (sheets preferred) — note: no push on question submit
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
            # append to primary storage (Sheets preferred)
            try:
                if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
                    sheets_mod.append_row([category, name, question, attempt, timestamp])
                else:
                    # fallback local persistence into data/questions_local.json
                    qpath = DATA_DIR / "questions_local.json"
                    try:
                        arr = json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else []
                    except Exception:
                        arr = []
                    arr.append([category, name, question, attempt, timestamp])
                    qpath.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("Failed appending question to primary storage; continuing anyway")
            flash("Question submitted. Thank you!", "success")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite="Lax")
                resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite="Lax")
            except Exception:
                pass
            return resp

        # GET: load questions
        questions = []
        raw = []
        try:
            if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
                raw = sheets_mod.get_questions()
            else:
                qpath = DATA_DIR / "questions_local.json"
                if qpath.exists():
                    raw = json.loads(qpath.read_text(encoding="utf-8") or "[]")
                else:
                    raw = []
        except Exception:
            logger.exception("Failed loading questions; showing empty list.")
            raw = []
        # Normalize rows to length 5 and filter subscription marker rows
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
        # categories for UI
        derived = {str(q[0]).strip() for q in questions if q and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "")
        if selected_category:
            questions = [q for q in questions if str(q[0]).strip() == selected_category]
        return render_template("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception:
        logger.exception("questions_page exception")
        flash("Could not load questions", "danger")
        return render_template("questions.html", questions=[], categories=STATIC_CATEGORIES, selected_category="", message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)

# Admin announcement UI and action (protected by ADMIN_KEY) — stores announcements in /data JSON and attempts push delivery
@app.route("/admin/announcement", methods=["GET", "POST"])
def admin_announcement():
    # key may be passed as ?key= or form field admin_key
    provided_key = request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or ""
    if request.method == "GET" and not provided_key:
        return render_template("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if request.method == "POST":
        subject = (request.form.get("subject") or "All").strip()
        title = (request.form.get("title") or "Announcement").strip()
        body = (request.form.get("body") or "").strip()
        results = []
        try:
            # persist globally
            item = _add_admin_notification(title, body, {"subject": subject})
            if not item:
                flash("Failed persisting announcement.", "danger")
                return render_template("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)
            # send pushes (best-effort)
            payload = {"title": title, "body": body, "url": "/", "id": item["id"], "announcement": True}
            push_res = _send_push_to_all(payload)
            flash(f"Announcement queued. push_sent={push_res.get('sent',0)}", "success")
            return render_template("admin_announcement.html", key=provided_key, result={"persisted": item, "push": push_res}, message=FOOTER_MESSAGE)
        except Exception:
            logger.exception("admin_announcement failed")
            flash("Failed to send announcement.", "danger")
            return render_template("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)
    return render_template("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)

# Small utility: safe multi-statement SQL executor for exercises (returns in-memory sqlite connection)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
        except Exception:
            # fallback: try splitting; conservative splitting by semicolon
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

# Generic admin SQL runner endpoint (protected) - optional; careful in production!
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
        # return a limited preview of tables as JSON
        rows_preview = {}
        for t in tables:
            try:
                cur = conn.execute(f"SELECT * FROM \"{t}\" LIMIT 20")
                rows_preview[t] = [dict(row) for row in cur.fetchall()]
            except Exception:
                rows_preview[t] = []
        return jsonify({"ok": True, "tables": tables, "preview": rows_preview})
    except Exception:
        logger.exception("admin_run_sql failed")
        return jsonify({"ok": False, "error": "execution-failed"}), 500

# Handlers for general exceptions — render friendly pages for human visitors
@app.errorhandler(404)
def handle_404(e):
    return render_template("errors/404.html", message=FOOTER_MESSAGE), 404

@app.errorhandler(Exception)
def handle_exception(e):
    # If request looks like an API, return JSON
    logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    if request.path.startswith("/api/") or request.path.startswith("/admin/run-sql") or request.is_json:
        return jsonify({"ok": False, "error": str(e)}), 500
    # render generic error page
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

# Run
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)