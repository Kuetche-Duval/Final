"""
Final consolidated Flask application for Newton's Nerds.

This file integrates:
 - push subscription management (save/check/delete)
 - notification persistence and delivery (optional pywebpush)
 - questions (sheet-backed optional, local fallback)
 - coding exercises editor + admin save (local file or optional Drive)
 - admin announcement + admin question manager
 - small helpers: service-worker, drive_image placeholder, static icon fallback
 - exercise SQL runner (in-memory, safe-ish)
 - defensive checks and improved normalization for endpoints

Notes:
 - I preserved your templates as-is; this app expects those templates in templates/
 - If you use Google Drive / Sheets, provide sheets.py with the helper functions referenced,
   or leave it absent to use local fallbacks.
 - Ensure environment variables are set for ADMIN_KEY, VAPID keys (for push), etc.
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
    jsonify, send_from_directory, make_response, abort, Response, g
)
from dotenv import load_dotenv

# Optional push library
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# Optional Google API / sheets (if you provide sheets.py)
SHEETS_AVAILABLE = False
sheets_mod = None
try:
    import sheets as sheets_mod_candidate  # type: ignore
    sheets_mod = sheets_mod_candidate
    if all(hasattr(sheets_mod, fn) for fn in ("save_subscription", "list_subscriptions", "get_questions", "append_row", "delete_subscriptions_by_endpoint")):
        SHEETS_AVAILABLE = True
except Exception:
    sheets_mod = None
    SHEETS_AVAILABLE = False

# load .env
load_dotenv()

# app init
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

# secrets / config
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# VAPID (push)
VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()
if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
    app.logger.info("VAPID keys not set — push sending disabled until configured.")

# admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", "") or ""
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# service account (optional)
SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64", "")
SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
if SERVICE_ACCOUNT_B64:
    try:
        raw = base64.b64decode(SERVICE_ACCOUNT_B64)
        json.loads(raw.decode("utf-8"))  # validate
        with open(SERVICE_ACCOUNT_PATH, "wb") as fh:
            fh.write(raw)
        try:
            os.chmod(SERVICE_ACCOUNT_PATH, 0o600)
        except Exception:
            pass
        app.logger.info("Wrote service account to %s", SERVICE_ACCOUNT_PATH)
    except Exception:
        app.logger.exception("Failed to write service account json from env")

# coding exercises path
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = (os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID") or "").strip()

# sheets keys (optional)
QUESTIONS_SUBSCRIPTION_KEY = (os.environ.get("QUESTIONS_SUBSCRIPTION_KEY") or "").strip()
QUESTIONS_SHEET_KEY = (os.environ.get("QUESTIONS_SHEET_KEY") or "").strip()
TIMETABLE_SHEET_KEY = (os.environ.get("TIMETABLE_SHEET_KEY") or "").strip()
GROUP_RULES_SHEET_KEY = (os.environ.get("GROUP_RULES_SHEET_KEY") or "").strip()

# data directory & sqlite DB
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB).resolve()

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nn_app")

# --- DB init ---
def _init_local_db():
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

# --- helpers ---
def normalize_endpoint(endpoint: str) -> str:
    """Normalize URL-like push endpoints to a canonical form for storage & matching."""
    if not endpoint:
        return ""
    try:
        u = urlparse(endpoint)
        scheme = u.scheme or "https"
        netloc = u.netloc or (u.path.split("/")[0] if u.path else "")
        path = u.path or ""
        normalized = f"{scheme}://{netloc}{path}".rstrip("/")
        return normalized
    except Exception:
        return endpoint.strip()

def _is_valid_subscription_obj(sub) -> bool:
    if not sub or not isinstance(sub, dict):
        return False
    ep = sub.get("endpoint") or sub.get("url")
    return bool(ep and isinstance(ep, str))

def _normalize_subject(s: str) -> str:
    return (s or "Other").strip()

def admin_required(f):
    """Decorator to enforce ADMIN_KEY for endpoints that accept key via form or query."""
    @wraps(f)
    def inner(*args, **kwargs):
        provided = request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or ""
        if provided != ADMIN_KEY:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return inner

def render_template_safe(template_name, **kwargs):
    try:
        app.jinja_env.get_or_select_template(template_name)
        return render_template(template_name, **kwargs)
    except Exception as e:
        logger.warning("Template %s missing or failed to render: %s", template_name, e)
        try:
            app.jinja_env.get_or_select_template("error.html")
            return render_template("error.html", message=FOOTER_MESSAGE, error=f"Missing template: {template_name}"), 500
        except Exception:
            return f"<h1>Template missing: {template_name}</h1><pre>{str(e)}</pre>", 500

# --- persistence helpers ---
def _save_subscription(subject, client_id, subscription_obj) -> bool:
    subject = _normalize_subject(subject)
    try:
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

        endpoint = subscription_obj.get("endpoint") or subscription_obj.get("url")
        if not endpoint:
            logger.warning("Subscription missing endpoint; skipping save")
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
        logger.debug("Saved subscription to sqlite for %s", endpoint_norm)
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
        normalized = normalize_endpoint(endpoint)
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalized))
            conn.commit()
        logger.debug("Deleted local subscription(s) for endpoint %s (normalized=%s)", endpoint, normalized)
    except Exception:
        logger.exception("_delete_subscriptions_by_endpoint failed")

# notifications
def save_notification_for_endpoint(endpoint, subject, title, body, data_obj=None):
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
                      (endpoint_norm or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
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
        return changed
    except Exception:
        logger.exception("ack_notifications_by_ids failed")
        return 0

# --- push sending ---
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

    # persist notifications for offline clients (use normalized endpoints)
    for r in subs:
        try:
            sub = r.get("subscription") if isinstance(r, dict) else None
            endpoint = None
            if sub and isinstance(sub, dict):
                endpoint = sub.get("endpoint")
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
        return {"sent": 0, "errors": [d for d in detail if not d.get("ok")], "detail": detail}

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
                    detail.append({"endpoint": normalize_endpoint(endpoint), "ok": True})
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

# --- HTTP endpoints ---
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
                    if sub and subj:
                        if normalize_endpoint(sub.get("endpoint")) == normalize_endpoint(endpoint):
                            subjects_for_endpoint.append(_normalize_subject(subj))
                except Exception:
                    continue
            subjects_for_endpoint = sorted(list(dict.fromkeys(subjects_for_endpoint)))
        except Exception:
            subjects_for_endpoint = []
        resp = jsonify({"ok": ok, "subjects": subjects_for_endpoint})
        try:
            if endpoint:
                resp.set_cookie('nn_subscription', normalize_endpoint(endpoint), max_age=COOKIE_MAX_AGE, samesite='Lax')
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
                    if sub and subj and normalize_endpoint(sub.get("endpoint")) == normalize_endpoint(endpoint):
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

@app.route("/api/vapid")
def api_vapid():
    if not VAPID_PUBLIC_KEY:
        return jsonify({"ok": False, "error": "vapid-not-configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})

# Serve service worker
@app.route("/service-worker.js")
def service_worker():
    try:
        return send_from_directory(app.static_folder, "service-worker.js")
    except Exception:
        logger.exception("service-worker not found")
        abort(404)

# Static icons fallback route (to avoid noisy 404s)
@app.route("/static/icons/<path:filename>")
def icons_fallback(filename):
    # try static file first
    icons_dir = Path(app.static_folder) / "icons"
    candidate = icons_dir / filename
    if candidate.exists():
        return send_from_directory(str(candidate.parent), candidate.name)
    # return a minimal SVG placeholder
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" viewBox="0 0 192 192">
      <rect width="100%" height="100%" fill="#2b8cff"/>
      <text x="50%" y="50%" font-size="36" fill="#fff" text-anchor="middle" dominant-baseline="central">NN</text>
    </svg>"""
    resp = make_response(svg)
    resp.headers["Content-Type"] = "image/svg+xml"
    return resp

# drive image helper route (templates reference this)
@app.route("/drive-image/<path:file_id>")
def drive_image(file_id):
    """
    Attempt to serve an image by file_id. If Drive integration is not configured,
    return a static placeholder image (img/logo.png).
    """
    # If sheets_mod provides a method to fetch drive images, try it (optional)
    try:
        if sheets_mod and hasattr(sheets_mod, "fetch_drive_image_bytes"):
            data, content_type = sheets_mod.fetch_drive_image_bytes(file_id)
            if data:
                return Response(data, mimetype=content_type)
    except Exception:
        logger.exception("drive_image via sheets_mod failed")

    # Fallback to app static logo
    return send_from_directory(app.static_folder, "img/logo.png")

# --- pages ---
@app.route("/")
def home_page():
    # supply minimal context expected by templates
    try:
        return render_template_safe("index.html", message=FOOTER_MESSAGE, verse=os.environ.get("HERO_VERSE", ""), reference=os.environ.get("HERO_REFERENCE", ""))
    except Exception:
        return render_template_safe("index.html", message=FOOTER_MESSAGE)

@app.route("/index", endpoint="index")
def index_alias():
    return home_page()

@app.route("/content")
def content_page():
    """
    Robust content page: try Sheets/Drive where available, otherwise provide
    safe fallbacks for all variables content.html references.
    """
    try:
        # defaults / safe fallbacks
        timetable_today = []
        sheet_title = None
        sheet_content = None
        dr_values = []        # gallery drive rows
        form_list = []        # google forms list
        notes_drive_url = os.environ.get("NOTES_DRIVE_URL", "")
        hero_image_id = os.environ.get("HERO_IMAGE_ID", "")
        hero_title = os.environ.get("HERO_TITLE", "Stay Consistent")
        hero_subtitle = os.environ.get("HERO_SUBTITLE", "Success = small efforts repeated daily.")

        # Try to load from sheets if configured (non-fatal)
        if SHEETS_AVAILABLE and TIMETABLE_SHEET_KEY:
            try:
                # optional helper names — wrapped so missing funcs don't break
                if hasattr(sheets_mod, "get_timetable_for_today"):
                    timetable_today = sheets_mod.get_timetable_for_today() or []
                if hasattr(sheets_mod, "get_timetable_sheet_content"):
                    sheet_title, sheet_content = sheets_mod.get_timetable_sheet_content() or (None, None)
                if hasattr(sheets_mod, "read_rules"):
                    # not needed here but safe example
                    pass
            except Exception:
                logger.exception("Failed loading timetable or sheet content from sheets_mod; continuing with defaults.")

        # Try to load gallery / forms if helper functions exist
        if SHEETS_AVAILABLE:
            try:
                if hasattr(sheets_mod, "list_gallery"):
                    dr_values = sheets_mod.list_gallery() or []
                if hasattr(sheets_mod, "list_forms"):
                    form_list = sheets_mod.list_forms() or []
            except Exception:
                logger.exception("Failed loading gallery/forms from sheets_mod; continuing with defaults.")

        return render_template_safe("content.html",
                                    message=FOOTER_MESSAGE,
                                    timetable_today=timetable_today,
                                    sheet_title=sheet_title,
                                    sheet_content=sheet_content,
                                    dr_values=dr_values,
                                    form_list=form_list,
                                    notes_drive_url=notes_drive_url,
                                    hero_image_id=hero_image_id,
                                    hero_title=hero_title,
                                    hero_subtitle=hero_subtitle)
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
                resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite='Lax')
                resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite='Lax')
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
        STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "")
        if selected_category:
            questions = [q for q in questions if str(q[0]).strip() == selected_category]

        return render_template_safe("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception:
        logger.exception("questions_page exception")
        flash("Could not load questions", "danger")
        return render_template_safe("questions.html", questions=[], categories=["Other"], selected_category="", message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)

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

@app.route("/rules")
def rules_page():
    rules = []
    try:
        if SHEETS_AVAILABLE and GROUP_RULES_SHEET_KEY:
            try:
                rules = sheets_mod.read_rules()
            except Exception:
                logger.exception("Failed reading group rules")
    except Exception:
        logger.exception("rules_page failed")
    return render_template_safe("rules.html", rules=rules, message=FOOTER_MESSAGE)

@app.route("/health")
def health_index():
    plants = []
    # optionally load from sheets_mod
    return render_template_safe("health.html", plants=plants, message=FOOTER_MESSAGE)

@app.route("/health/<path:slug>")
def health_detail(slug):
    # Render a detail using slug; fallback placeholder
    plant = {"name": slug.replace("-", " ").title(), "virtues": [], "uses": "No details available.", "notes": "", "slug": slug}
    return render_template_safe("health_detail.html", plant=plant, message=FOOTER_MESSAGE)

# --- Admin: Announcement ---
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
                        if ep:
                            epn = normalize_endpoint(ep)
                            if epn and epn not in uniq:
                                uniq[epn] = sub
                    except Exception:
                        continue
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

# --- Admin: Coding exercises editor ---
@app.route("/admin/exercises", methods=["GET", "POST"])
def admin_exercises():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided_key:
        return render_template_safe("admin_exercises.html", key="", drive_file_id=CODING_DRIVE_FILE_ID, message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template_safe("admin_exercises.html", key="", drive_file_id=CODING_DRIVE_FILE_ID, message=FOOTER_MESSAGE)
    if request.method == "POST":
        # Gather form
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
            if target == "drive" and CODING_DRIVE_FILE_ID and SHEETS_AVAILABLE and hasattr(sheets_mod, "upload_coding_exercises"):
                # optional drive upload hook in sheets_mod
                sheets_mod.upload_coding_exercises(data, CODING_DRIVE_FILE_ID)
                flash("Saved to Drive file (via sheets_mod).", "success")
            else:
                # local save: read existing file, update or append by id
                path = Path(CODING_LOCAL_PATH)
                arr = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                # remove existing with same id
                arr = [e for e in arr if str(e.get("id")) != str(data.get("id"))]
                arr.append(data)
                path.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
                flash("Saved to local coding_exercises.json", "success")
        except Exception:
            logger.exception("Failed saving exercise")
            flash("Failed saving exercise", "danger")
        return render_template_safe("admin_exercises.html", key=provided_key, drive_file_id=CODING_DRIVE_FILE_ID, message=FOOTER_MESSAGE)
    # fallback GET
    return render_template_safe("admin_exercises.html", key=provided_key, drive_file_id=CODING_DRIVE_FILE_ID, message=FOOTER_MESSAGE)

# --- Admin: Manage questions ---
@app.route("/admin/questions", methods=["GET", "POST"])
def admin_questions():
    provided = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided:
        return render_template_safe("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)
    if provided != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template_safe("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)

    # load questions into normalized list of dict rows with index
    qpath = DATA_DIR / "questions_local.json"
    raw = []
    try:
        if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
            raw = sheets_mod.get_questions()
        else:
            raw = json.loads(qpath.read_text(encoding="utf-8") or "[]") if qpath.exists() else []
    except Exception:
        logger.exception("Failed loading questions for admin")
        raw = []

    rows = []
    for idx, r in enumerate(raw or []):
        try:
            if isinstance(r, dict):
                rows.append({"index": idx, "subject": r.get("subject",""), "user": r.get("name", r.get("user","")), "text": r.get("question", r.get("text","")), "attempt": r.get("attempt","")})
            else:
                lst = list(r) if isinstance(r, (list, tuple)) else [r]
                rows.append({"index": idx, "subject": lst[0] if len(lst)>0 else "", "user": lst[1] if len(lst)>1 else "", "text": lst[2] if len(lst)>2 else "", "attempt": lst[3] if len(lst)>3 else ""})
        except Exception:
            continue

    subjects = sorted(list({r["subject"] for r in rows if r.get("subject")}))

    # handle actions
    action = request.form.get("action") or ""
    if request.method == "POST" and action == "delete_selected":
        selected = request.form.getlist("selected")
        selected_idx = set(int(x) for x in selected if str(x).isdigit())
        new = [r for i,r in enumerate(raw) if i not in selected_idx]
        try:
            if SHEETS_AVAILABLE and QUESTIONS_SHEET_KEY:
                # no granular delete in this generic codepath; fallback to overwrite local file
                pass
            qpath.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
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
                qpath.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
                flash(f"Deleted questions for subject {subj}.", "success")
            except Exception:
                logger.exception("Failed deleting subject")
                flash("Failed deleting subject", "danger")
        return redirect(url_for("admin_questions", key=provided))

    return render_template_safe("admin_questions.html", key=provided, questions=rows, subjects=subjects, message=FOOTER_MESSAGE)

# --- Exercise runner (safe-ish in-memory sqlite) ---
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
        except Exception:
            # fallback split statements
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
                # CORRECTED: existence check uses '=' operator with placeholder
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

# admin run-sql (protected)
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

# --- admin debug: list subscriptions & pending notifications (protected) ---
@app.route("/admin/debug/subscriptions")
def admin_debug_subs():
    key = request.args.get("key") or ""
    if key != ADMIN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    subs = _list_subscriptions()
    return jsonify({"ok": True, "count": len(subs), "subscriptions": subs})

@app.route("/admin/debug/notifications")
def admin_debug_notifications():
    key = request.args.get("key") or ""
    if key != ADMIN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
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
        return jsonify({"ok": True, "count": len(out), "notifications": out})
    except Exception:
        logger.exception("admin_debug_notifications failed")
        return jsonify({"ok": False, "error": "server-error"}), 500

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

# Startup template inventory (helpful)
def _log_template_inventory():
    needed = [
        "base.html", "index.html", "content.html", "questions.html", "quiz_forms.html",
        "quizzes.html", "coding.html", "health.html", "health_detail.html", "rules.html",
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

# run
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    logger.info("Starting app on port %s (debug=%s)", port, debug_mode)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)