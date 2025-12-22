# app.py — PART 1/5
import os
import json
import time
import base64
import logging
import traceback
import re
import sqlite3
import binascii
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, Response, make_response
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

# optional requests for drive image proxy
try:
    import requests
except Exception:
    requests = None

# Load env
load_dotenv()
logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# Constants / config
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
STATIC_CATEGORIES = ["Mathematics", "Biology", "Computer Science", "Further Mathematics", "Chemistry", "Physics", "Coding", "Other"]
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Service account (allow base64)
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
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# Admin keys
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
EXERCISES_ADMIN_KEY = os.environ.get("EXERCISES_ADMIN_KEY", ADMIN_KEY)

# Sheets keys (if you want to store subscriptions in a dedicated sheet)
QUESTIONS_SUBSCRIPTION_KEY = os.environ.get("QUESTIONS_SUBSCRIPTION_KEY", "").strip()
QUESTIONS_SHEET_KEY = os.environ.get("QUESTIONS_SHEET_KEY", "").strip()
TIMETABLE_SHEET_KEY = os.environ.get("TIMETABLE_SHEET_KEY", "").strip()
GROUP_RULES_SHEET_KEY = os.environ.get("GROUP_RULES_SHEET_KEY", "").strip()

# Coding exercises files
CODING_LOCAL_PATH = os.environ.get("CODING_EXERCISES_PATH", "coding_exercises.json")
CODING_DRIVE_FILE_ID = os.environ.get("CODING_EXERCISES_DRIVE_FILE_ID", "").strip()

# Local SQLite DB path (fallback)
NN_LOCAL_DB = os.environ.get("NN_LOCAL_DB", "nn_local.db")
DB_PATH = Path(NN_LOCAL_DB)

# Optional external modules: sheets.py and drive.py (we'll detect gracefully)
SHEETS_AVAILABLE = False
try:
    import sheets as sheets_mod
    # check that functions we need exist
    if all(hasattr(sheets_mod, fn) for fn in ("save_subscription", "list_subscriptions", "get_questions", "append_row")):
        SHEETS_AVAILABLE = True
        app.logger.info("sheets.py loaded and will be used for Sheets-backed operations.")
    else:
        app.logger.warning("sheets.py loaded but missing required functions; will fallback to local sqlite when needed.")
        SHEETS_AVAILABLE = False
except Exception:
    sheets_mod = None
    app.logger.info("sheets.py not available — using local sqlite fallback for subscriptions/notifications.")
# app.py — PART 2/5 (local DB init, helpers)
def _init_local_db():
    try:
        if not DB_PATH.parent.exists():
            try:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        # subscriptions table: endpoint unique
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
        # notifications queue
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

# Utilities
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
    # keys optionally present
    return True

def _normalize_subject(s):
    return (s or "Other").strip()
# app.py — PART 3/5 (sheet/local save/list helpers)
# Save a subscription preferentially to the subscription sheet if configured and available,
# otherwise to local sqlite.
def _save_subscription(subject, client_id, subscription_obj):
    subject = _normalize_subject(subject)
    try:
        # If sheets are available AND QUESTIONS_SUBSCRIPTION_KEY provided, prefer that path
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                # sheets_mod.save_subscription(subject, client_id, subscription_obj)
                # We call the sheets helper and let it dedupe as implemented in your sheets.py
                sheets_mod.save_subscription(subject, client_id, subscription_obj)
                app.logger.debug("Saved subscription to Sheets for subject %s", subject)
                return True
            except Exception:
                app.logger.exception("sheets.save_subscription failed — falling back to local sqlite")
        # Local fallback: upsert into SQLite
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}
        endpoint = subscription_obj.get("endpoint")
        keys = subscription_obj.get("keys", {}) if isinstance(subscription_obj, dict) else {}
        p256 = keys.get("p256dh", "")
        auth = keys.get("auth", "")
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""REPLACE INTO subscriptions (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                         VALUES (?,?,?,?,?,?,?)""", (endpoint, subject, client_id or "", p256, auth, json.dumps(subscription_obj), now))
            conn.commit()
        app.logger.debug("Saved subscription to local sqlite for subject %s", subject)
        return True
    except Exception:
        app.logger.exception("_save_subscription failed")
        return False

# List subscriptions (prefer sheet if configured)
def _list_subscriptions(subject=None):
    try:
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                # sheets_mod.list_subscriptions(subject) expected to return list of dicts
                return sheets_mod.list_subscriptions(subject)
            except Exception:
                app.logger.exception("sheets.list_subscriptions failed — falling back to sqlite")
        # local sqlite fallback
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
        # Try sheets first if configured
        if SHEETS_AVAILABLE and QUESTIONS_SUBSCRIPTION_KEY:
            try:
                sheets_mod.delete_subscriptions_by_endpoint(endpoint)
                app.logger.debug("Deleted subscription from sheet for endpoint %s", endpoint)
                return
            except Exception:
                app.logger.exception("sheets.delete_subscriptions_by_endpoint failed — falling back to sqlite")
        # local sqlite delete
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint, normalize_endpoint(endpoint)))
            conn.commit()
        app.logger.debug("Deleted subscription(s) from local sqlite for endpoint %s", endpoint)
    except Exception:
        app.logger.exception("_delete_subscriptions_by_endpoint failed")
# app.py — PART 4/5 (notifications persistence & send helper + endpoints)
def save_notification_for_endpoint(endpoint, subject, title, body, data_obj=None):
    try:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with sqlite3.connect(str(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO notifications (endpoint, subject, title, body, data, created_at, delivered)
                         VALUES (?,?,?,?,?,?,0)""", (endpoint or "", subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
            nid = c.lastrowid
            conn.commit()
        return nid
    except Exception:
        app.logger.exception("save_notification_for_endpoint failed")
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
        app.logger.exception("fetch_pending_notifications_for_endpoint failed")
        return []

def ack_notifications_by_ids(endpoint, ids):
    if not endpoint or not ids:
        return 0
    try:
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
        app.logger.exception("ack_notifications_by_ids failed")
        return 0

# Send push to all subscribers of a subject (persist + attempt send)
def send_push_to_subject(subject, question_text, attempt_text):
    try:
        subs = _list_subscriptions(subject) or []
    except Exception:
        subs = []
    if not subs:
        return {"sent": 0, "errors": ["no subscribers"], "detail": []}

    title = f"New question in {subject}"
    q_preview = (question_text or "")[:140]
    a_preview = (attempt_text or "")[:180]
    body = f"Q: {q_preview}\nA: {a_preview}"
    payload_template = {"title": title, "body": body, "url": "/questions", "subject": subject, "preview": {"question": q_preview, "attempt": a_preview}}

    results = []
    sent = 0

    # persist notifications for each subscription
    for r in subs:
        try:
            sub = r.get("subscription") if isinstance(r, dict) else None
            if not sub and isinstance(r, dict) and "raw" in r:
                try:
                    sub = json.loads(r.get("raw") or "{}")
                except Exception:
                    sub = None
            endpoint = sub.get("endpoint") if sub else None
            try:
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
            except Exception:
                app.logger.debug("Could not persist notification for %s", endpoint)
        except Exception:
            continue

    # If we cannot send (no pywebpush or VAPID), return persisted result
    if (not PYWEBPUSH_AVAILABLE) or (not VAPID_PRIVATE_KEY):
        for r in subs:
            try:
                sub = r.get("subscription") if isinstance(r, dict) else None
                endpoint = sub.get("endpoint") if sub else None
            except Exception:
                endpoint = None
            results.append({"endpoint": endpoint, "ok": False, "error": "pywebpush-not-configured"})
        return {"sent": 0, "errors": ["pywebpush/VAPID not available"], "detail": results}

    # attempt sending
    for r in subs:
        try:
            sub = r.get("subscription") if isinstance(r, dict) else None
            if not sub:
                continue
            endpoint = sub.get("endpoint")
            if not endpoint:
                results.append({"endpoint": None, "ok": False, "error": "no-endpoint"})
                continue

            payload = dict(payload_template)
            parsed = urlparse(endpoint)
            domain = parsed.netloc.lower() if parsed.netloc else ""
            if "windows" in domain or "wns" in domain:
                payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}

            # trim large payloads
            data_bytes = json.dumps(payload).encode("utf-8")
            if len(data_bytes) > 2000:
                payload["preview"] = None
                payload["body"] = (payload.get("body","")[:200] + "…")

            attempt = 0
            while attempt < 2:
                attempt += 1
                try:
                    webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
                    sent += 1
                    results.append({"endpoint": endpoint, "ok": True})
                    # mark delivered
                    with sqlite3.connect(str(DB_PATH)) as conn:
                        c = conn.cursor()
                        now = datetime.utcnow().replace(microsecond=0).isoformat()
                        c.execute("UPDATE notifications SET delivered=1, delivered_at=? WHERE endpoint=? AND delivered=0", (now, endpoint))
                        conn.commit()
                    break
                except WebPushException as ex:
                    resp = getattr(ex, "response", None)
                    status = getattr(resp, "status_code", None) if resp else None
                    body_text = None
                    try:
                        if resp and hasattr(resp, "content"):
                            body_text = resp.content.decode("utf-8", errors="ignore")
                    except Exception:
                        body_text = None
                    err_text = str(ex)
                    app.logger.warning("WebPushException endpoint=%s status=%s err=%s", endpoint, status, err_text)
                    if status in (404, 410) or (body_text and "unsubscribed" in body_text.lower()):
                        try:
                            _delete_subscriptions_by_endpoint(endpoint)
                        except Exception:
                            pass
                        results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text, "deleted": True})
                        break
                    if attempt < 2:
                        time.sleep(0.12)
                        continue
                    else:
                        results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text})
                        break
                except Exception as ex:
                    app.logger.exception("Unexpected exception sending push to %s", endpoint)
                    results.append({"endpoint": endpoint, "ok": False, "error": str(ex)})
                    break
        except Exception:
            app.logger.exception("Error handling subscription for sending")
            results.append({"endpoint": None, "ok": False, "error": "processing-exception"})
    return {"sent": sent, "errors": [r for r in results if not r.get("ok")], "detail": results}

# Routes: save-subscription, subscriptions/check, notifications endpoints
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
    except Exception as e:
        app.logger.exception("save-subscription failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

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
            app.logger.exception("subscriptions_check list failed")
        return jsonify({"ok": True, "subjects": sorted(list(dict.fromkeys(out_subjects)))})
    except Exception as e:
        app.logger.exception("subscriptions_check top-level failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/notifications/pending", methods=["GET"])
def notifications_pending():
    try:
        endpoint = request.args.get("endpoint", "").strip() or (request.cookies.get('nn_subscription') or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400
        items = fetch_pending_notifications_for_endpoint(endpoint)
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        app.logger.exception("notifications_pending failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

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
    except Exception as e:
        app.logger.exception("notifications_ack failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
# app.py — PART 5/5 (core pages, questions submit, admin announcement, SQL runner, entry)
# Basic pages — home and questions
@app.route("/")
def home_page():
    return render_template("index.html", message=FOOTER_MESSAGE)

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
            # append_row -> prefer sheets get_questions implementation if provided in sheets_mod
            try:
                if SHEETS_AVAILABLE:
                    sheets_mod.append_row([category, name, question, attempt, timestamp])
                else:
                    # fallback: append to QUESTIONS_SHEET_KEY using sqlite or saved local file
                    # Here we rely on sheets_mod.get_questions fallback earlier
                    append_fallback = getattr(sheets_mod, "append_row", None)
                    if callable(append_fallback):
                        append_fallback([category, name, question, attempt, timestamp])
                    else:
                        # minimal local persistence: save to local 'data/questions_local.json'
                        p = Path("data/questions_local.json")
                        p.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            arr = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
                        except Exception:
                            arr = []
                        arr.append([category, name, question, attempt, timestamp])
                        p.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                app.logger.exception("Failed to append question to primary storage; still continuing.")
            flash("Question submitted. Thank you!", "success")
            # Persist notifications and try to send
            try:
                send_push_to_subject(category, question, attempt)
            except Exception:
                app.logger.exception("send_push_to_subject failed")
            resp = redirect(url_for("questions_page"))
            try:
                resp.set_cookie("nn_user", name, max_age=COOKIE_MAX_AGE, samesite="Lax")
                resp.set_cookie("nn_last_category", category, max_age=COOKIE_MAX_AGE, samesite="Lax")
            except Exception:
                pass
            return resp
        # GET
        # load questions (prefer sheets_mod.get_questions())
        questions = []
        try:
            if SHEETS_AVAILABLE:
                raw = sheets_mod.get_questions()
            else:
                raw = []
            # normalize rows to length 5 and filter out subscription marker rows
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
        except Exception:
            app.logger.exception("Failed loading questions; showing empty list.")
            questions = []
        # build categories
        derived = {str(q[0]).strip() for q in questions if q and q[0]}
        categories_for_ui = STATIC_CATEGORIES + [c for c in sorted(derived) if c not in STATIC_CATEGORIES]
        selected_category = request.args.get("categoryFilter", "")
        if selected_category:
            questions = [q for q in questions if str(q[0]).strip() == selected_category]
        return render_template("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception as e:
        app.logger.exception("questions_page exception: %s", e)
        flash("Could not load questions", "danger")
        return render_template("questions.html", questions=[], categories=STATIC_CATEGORIES, selected_category="", message=FOOTER_MESSAGE, vapid_public_key=VAPID_PUBLIC_KEY)

# Admin announcement route (send to all or by subject)
@app.route("/admin/announcement", methods=["GET", "POST"])
def admin_announcement():
    key = request.args.get("key") or request.form.get("admin_key") or request.form.get("key") or ""
    if request.method == "GET" and not key:
        return render_template("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template("admin_announcement.html", key="", message=FOOTER_MESSAGE)
    if request.method == "POST":
        subject = (request.form.get("subject") or "All").strip()
        title = (request.form.get("title") or "Announcement").strip()
        body = (request.form.get("body") or "").strip()
        details = []
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
                            try:
                                webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body, "url": "/"}), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT})
                                details.append({"endpoint": sub.get("endpoint"), "ok": True})
                            except Exception as ex:
                                details.append({"endpoint": sub.get("endpoint"), "ok": False, "error": str(ex)})
                        else:
                            details.append({"endpoint": sub.get("endpoint"), "ok": False, "error": "push-not-configured"})
                    except Exception:
                        details.append({"endpoint": None, "ok": False, "error": "exception"})
                flash(f"Announcement queued/sent to {len(details)} endpoints.", "success")
                return render_template("admin_announcement.html", key=key, result=details, message=FOOTER_MESSAGE)
            else:
                res = send_push_to_subject(subject, title, body)
                flash("Announcement attempted.", "success")
                return render_template("admin_announcement.html", key=key, result=res, message=FOOTER_MESSAGE)
        except Exception as e:
            app.logger.exception("admin_announcement failed: %s", e)
            flash("Failed to send announcement.", "danger")
            return render_template("admin_announcement.html", key=key, message=FOOTER_MESSAGE)
    return render_template("admin_announcement.html", key=key, message=FOOTER_MESSAGE)

# Exercise SQL runner (kept simple, multi-statement enabled)
def _safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if schema_sql:
        try:
            cur.executescript(schema_sql)
        except Exception:
            # attempt split fallback
            stmts = [s.strip() for s in re.split(r';\s*(?=\b)', schema_sql) if s.strip()]
            for s in stmts:
                try:
                    cur.executescript(s)
                except Exception:
                    try:
                        cur.execute(s)
                    except Exception:
                        continue
    # populate sample_data if dict
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

# Minimal error handler and launch
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    if request.path.startswith("/api/") or request.path.startswith("/exercise-table"):
        return jsonify({"ok": False, "error": str(e)}), 500
    return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)