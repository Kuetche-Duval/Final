# Unified app.py — JSON fallback storage by default (no nn_local.db unless USE_SQLITE=true)
# Based on your latest snippet with improvements:
import os
import io
import mimetypes
import json
import logging
import traceback
import sqlite3
import re
import time
import random
import base64
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, flash, send_file,
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

# Drive & Sheets helpers (optional; forgiving if missing)
try:
    import drive as gdrive
    DRIVE_API_AVAILABLE = getattr(gdrive, "GOOGLEAPI_AVAILABLE", False)
except Exception:
    gdrive = None
    DRIVE_API_AVAILABLE = False

try:
    import sheets as gsheets
    SHEETS_AVAILABLE = True
except Exception:
    gsheets = None
    SHEETS_AVAILABLE = False

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("newtons_nerds")

# Paths & config
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# lock to serialize subscription file writes
SUBS_LOCK = threading.Lock()

CODING_LOCAL_PATH = Path(os.environ.get("CODING_EXERCISES_PATH", DATA_DIR / "coding_exercises.json"))
HEALTH_LOCAL_PATH = Path(os.environ.get("HEALTH_DATA_PATH", DATA_DIR / "health_data.json"))
QUIZ_LOCAL_PATH = Path(os.environ.get("QUIZ_FORMS_PATH", DATA_DIR / "quiz_forms.json"))
NOTIFICATIONS_FILE = Path(os.environ.get("NOTIFICATIONS_FILE", DATA_DIR / "notifications.json"))
SUBSCRIPTIONS_FILE = Path(os.environ.get("SUBSCRIPTIONS_FILE", DATA_DIR / "subscriptions.json"))

# DB choice: by default use JSON files, unless USE_SQLITE env var is true
USE_SQLITE = str(os.environ.get("USE_SQLITE", "")).lower() in ("1", "true", "yes")
NN_LOCAL_DB = Path(os.environ.get("NN_LOCAL_DB", "nn_local.db")).resolve() if USE_SQLITE else None

MOTIVATIONAL_QUOTES_VERSES = [{"verse": "I can do all things through Christ who strengthens me.", "reference": "Philippians 4:13"}, {"verse": "If any of you lacks wisdom, let him ask God, who gives generously to all without reproach, and it will be given him.", "reference": "James 1:5"}, {"verse": "Trust in the Lord with all your heart, and do not lean on your own understanding. In all your ways acknowledge him, and he will make straight your paths.", "reference": "Proverbs 3:5-6"}, {"verse": "For I know the plans I have for you, declares the Lord, plans for welfare and not for evil, to give you a future and a hope.", "reference": "Jeremiah 29:11"}, {"verse": "Whatever you do, work heartily, as for the Lord and not for men.", "reference": "Colossians 3:23"}, {"verse": "Have I not commanded you? Be strong and courageous. Do not be frightened, and do not be dismayed, for the Lord your God is with you wherever you go.", "reference": "Joshua 1:9"}, {"verse": "Do not be anxious about anything, but in everything by prayer and supplication with thanksgiving let your requests be made known to God.", "reference": "Philippians 4:6"}, {"verse": "For God gave us a spirit not of fear but of power and love and self-control.", "reference": "2 Timothy 1:7"}, {"verse": "But they who wait for the Lord shall renew their strength; they shall mount up with wings like eagles; they shall run and not be weary; they shall walk and not faint.", "reference": "Isaiah 40:31"}, {"verse": "Let no one despise you for your youth, but set the believers an example in speech, in conduct, in love, in faith, in purity.", "reference": "1 Timothy 4:12"}]
get_random_quote = lambda arr: random.choice(arr)
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

ADMIN_KEY = os.environ.get("ADMIN_KEY", "") or ""

def ensure_google_credentials_from_base64(
    env_var_name: str,
    output_path: str,
    required_fields=("client_email", "private_key")
):
    """
    Decode a base64-encoded Google service account JSON stored in an env var
    and write it to output_path if it does not already exist.
    """
    if os.path.exists(output_path):
        return output_path

    b64 = os.environ.get(env_var_name, "").strip()
    if not b64:
        raise RuntimeError(f"Missing base64 credentials env var: {env_var_name}")

    try:
        raw = base64.b64decode(b64).decode("utf-8")
        data = json.loads(raw)

        # sanity check
        for f in required_fields:
            if f not in data:
                raise RuntimeError(f"Invalid credentials JSON: missing {f}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info("Google credentials written to %s", output_path)
        return output_path

    except Exception as e:
        raise RuntimeError(f"Failed to decode base64 credentials: {e}")

# --- Ensure Google credentials from base64 ---
try:
    GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "service_account.json")
    ensure_google_credentials_from_base64(
        env_var_name="SERVICE_ACCOUNT_JSON_B64",
        output_path=GOOGLE_CREDS_PATH
    )
    os.environ["SERVICE_ACCOUNT_JSON_PATH"] = GOOGLE_CREDS_PATH
except Exception as e:
    logger.warning("Google credentials not initialized: %s", e)

# If sqlite desired, initialize DB
def _init_sqlite_db(db_path: Path):
    try:
        conn = sqlite3.connect(str(db_path))
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
        logger.info("SQLite DB initialized at %s", db_path)
    except Exception:
        logger.exception("DB init failed")

if USE_SQLITE and NN_LOCAL_DB:
    _init_sqlite_db(NN_LOCAL_DB)
else:
    logger.info("Using JSON storage for subscriptions / notifications (no sqlite). Set USE_SQLITE=true to enable SQLite.")

# Helpers

def initialize_drive_helpers():
    """
    Try to import and initialize drive and sheets helpers, returning a dict with status.
    This will not raise but will return reasons why Drive/Sheets are unavailable.
    """
    info = {"gdrive_available": False, "gsheets_available": False, "errors": []}
    # Check credentials file env var
    cred_path = os.environ.get("SERVICE_ACCOUNT_JSON_PATH") or os.environ.get("GOOGLE_CREDENTIALS_PATH")
    if not cred_path:
        info["errors"].append("No SERVICE_ACCOUNT_JSON_PATH or GOOGLE_CREDENTIALS_PATH set.")
        logger.warning("Google credentials path missing; Drive/Sheets disabled.")
        return info

    cp = Path(cred_path)
    if not cp.exists():
        info["errors"].append(f"Credentials file not found at {cred_path}")
        logger.warning("Credentials file not found at %s", cred_path)
        return info

    # Try to import drive helper
    try:
        import drive as local_drive
        # optional: call an init function if present
        if hasattr(local_drive, "initialize") and callable(local_drive.initialize):
            try:
                local_drive.initialize(str(cp))
            except Exception as e:
                logger.exception("drive.initialize failed: %s", e)
                info["errors"].append(f"drive.initialize failed: {e}")
            else:
                info["gdrive_available"] = True
        else:
            # still mark available; module can implement functions directly
            info["gdrive_available"] = True
        globals()['gdrive'] = local_drive
        globals()['DRIVE_API_AVAILABLE'] = True
        logger.info("Drive helper loaded from %s", getattr(local_drive, "__file__", "<module>"))
    except Exception as e:
        info["errors"].append(f"drive import failed: {e}")
        logger.warning("Drive helper import failed: %s", e)

    # Try to import sheets helper
    try:
        import sheets as local_sheets
        globals()['gsheets'] = local_sheets
        globals()['SHEETS_AVAILABLE'] = True
        info["gsheets_available"] = True
        logger.info("Sheets helper loaded from %s", getattr(local_sheets, "__file__", "<module>"))
    except Exception as e:
        info["errors"].append(f"sheets import failed: {e}")
        logger.warning("Sheets helper import failed: %s", e)

    return info

# Attempt Drive/Sheets initialization (guarded)
try:
    drive_init_info = initialize_drive_helpers()
    logger.info("Drive init: %s", drive_init_info)
except Exception as err:
    logger.exception("Drive initialization failed at runtime: %s", err)
    drive_init_info = {"gdrive_available": False, "gsheets_available": False, "errors": [str(err)]}

def validate_json_file(path: Path) -> tuple[bool, str]:
    """
    Quick JSON validity check. Returns (ok, error_message).
    """
    try:
        txt = path.read_text(encoding="utf-8")
        if not txt.strip():
            return False, "Empty file"
        json.loads(txt)
        return True, ""
    except Exception as e:
        return False, str(e)

def load_coding_exercises() -> list:
    """
    Robust loader for coding exercises: returns list (possibly empty).
    Logs detailed error if file exists but is invalid.
    Use this in the /coding route instead of calling safe_load_json directly.
    """
      try:
        path = Path(os.environ.get("CODING_EXERCISES_PATH", DATA_DIR / "coding_exercises.json"))
        logger.debug("Loading coding exercises from %s", path)
        if not path.exists():
            logger.info("Coding exercises file not found at %s — returning empty list", str(path))
            return []
        ok, err = validate_json_file(path)
        if not ok:
            logger.error("Coding exercises JSON invalid: %s — path=%s", err, str(path))
            # optionally create a backup of the broken file for inspection
            try:
                backup = path.with_suffix(path.suffix + ".broken")
                if not backup.exists():
                    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                    logger.info("Backed up broken coding_exercises.json -> %s", str(backup))
            except Exception:
                logger.exception("Failed to backup broken coding_exercises.json")
            return []
        # load safely
        arr = json.loads(path.read_text(encoding="utf-8") or "[]")
        if not isinstance(arr, list):
            logger.warning("Expected list in coding_exercises.json, got %s", type(arr))
            return []
        return arr
    except Exception:
        logger.exception("Unexpected error loading coding exercises")
        return []

def get_admin_questions_context():
    """
    Returns (rows, subjects) prepared for admin_questions.html
    rows: list of dicts with keys index, subject, user, text, attempt
    subjects: sorted unique subject list
    """
    try:
        qfile = DATA_DIR / "questions_local.json"
        raw = []
        if qfile.exists():
            try:
                raw = json.loads(qfile.read_text(encoding="utf-8") or "[]")
            except Exception:
                logger.exception("admin: failed to parse questions_local.json; treating as empty")
                raw = []
        rows = []
        for idx, r in enumerate(raw):
            try:
                if isinstance(r, dict):
                    rows.append({
                        "index": idx,
                        "subject": r.get("subject",""),
                        "user": r.get("name", r.get("user","")),
                        "text": r.get("question", r.get("text","")),
                        "attempt": r.get("attempt","")
                    })
                else:
                    lst = list(r) if isinstance(r, (list, tuple)) else [r]
                    rows.append({
                        "index": idx,
                        "subject": lst[0] if len(lst) > 0 else "",
                        "user": lst[1] if len(lst) > 1 else "",
                        "text": lst[2] if len(lst) > 2 else "",
                        "attempt": lst[3] if len(lst) > 3 else ""
                    })
            except Exception:
                logger.exception("Skipping malformed question row at index %s", idx)
                continue
        subjects = sorted(list({row["subject"] for row in rows if row.get("subject")}))
        return rows, subjects
    except Exception:
        logger.exception("get_admin_questions_context failed")
        return [], []

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

def safe_load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default
        return json.loads(raw)
    except Exception:
        app.logger.error(f"Corrupted JSON detected, resetting: {path}")
        try:
            path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        except Exception:
            pass
        return default

def safe_write_json(path: Path, obj):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write to temp file then replace
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                json.dump(obj, tf, ensure_ascii=False, indent=2)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, str(path))
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        return True
    except Exception:
        logger.exception("Failed writing JSON: %s", path)
        return False


# JSON-backed subscriptions / notifications (fallback)
def _read_subscriptions_json():
    arr = safe_load_json(SUBSCRIPTIONS_FILE)or []
    if not isinstance(arr, list):
        return []
    return arr

def _write_subscriptions_json(arr):
    return safe_write_json(Path(SUBSCRIPTIONS_FILE), arr)

def _read_notifications_json():
    arr = safe_load_json(NOTIFICATIONS_FILE) or []
    if not isinstance(arr, list):
        return []
    return arr

def _write_notifications_json(arr):
    return safe_write_json(NOTIFICATIONS_FILE, arr)

# Subscription persistence (sqlite or json)
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

        # =========================
        # SQLITE BACKEND (unchanged)
        # =========================
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute(
                    """REPLACE INTO subscriptions
                       (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        endpoint_norm,
                        subject or "Other",
                        client_id or "",
                        p256,
                        auth,
                        json.dumps(subscription_obj),
                        now,
                    ),
                )
                conn.commit()
            return True

        # =========================
        # JSON BACKEND (FIXED)
        # =========================
        with SUBS_LOCK:
            arr = _read_subscriptions_json() or []

            # remove existing with same endpoint + subject
            arr = [
                r for r in arr
                if not (
                    r.get("subscription", {}).get("endpoint")
                    and normalize_endpoint(r.get("subscription", {}).get("endpoint")) == endpoint_norm
                    and r.get("subject") == (subject or "Other")
                )
            ]

            arr.append({
                "endpoint": endpoint_norm,
                "subject": subject or "Other",
                "client_id": client_id or "",
                "keys": {"p256dh": p256, "auth": auth},
                "subscription": subscription_obj,
                "created_at": now
            })

            _write_subscriptions_json(arr)

        return True

    except Exception:
        logger.exception("save_subscription failed")
        return False

def list_subscriptions(subject=None):
    try:
        if USE_SQLITE and NN_LOCAL_DB:
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

        arr = _read_subscriptions_json()
        out = []
        for r in arr:
            try:
                if subject and r.get("subject") != subject:
                    continue
                out.append({"subject": r.get("subject"), "client_id": r.get("client_id"), "subscription": r.get("subscription"), "created_at": r.get("created_at")})
            except Exception:
                continue
        return out
    except Exception:
        logger.exception("list_subscriptions failed")
        return []

def delete_subscriptions_by_endpoint(endpoint):
    try:
        if not endpoint:
            return
        endpoint_norm = normalize_endpoint(endpoint)
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute("DELETE FROM subscriptions WHERE endpoint=? OR endpoint=?", (endpoint_norm, endpoint))
                conn.commit()
            return
        arr = _read_subscriptions_json()
        new = [r for r in arr if not (r.get("subscription", {}).get("endpoint") and normalize_endpoint(r.get("subscription", {}).get("endpoint")) == endpoint_norm)]
        _write_subscriptions_json(new)
    except Exception:
        logger.exception("delete_subscriptions_by_endpoint failed")

# Notifications persistence (sqlite or json)
def save_notification(endpoint, subject, title, body, data_obj=None):
    try:
        if not endpoint:
            logger.debug("save_notification skipped: no endpoint")
            return None
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat()

        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute("""INSERT INTO notifications (endpoint, subject, title, body, data, created_at, delivered)
                             VALUES (?,?,?,?,?,?,0)""",
                          (endpoint_norm, subject or "", title or "", body or "", json.dumps(data_obj or {}), now))
                nid = c.lastrowid
                conn.commit()
            return nid

        arr = _read_notifications_json()
        # generate id
        max_id = max([it.get("id", 0) for it in arr], default=0)
        nid = int(max_id) + 1
        arr.append({
            "id": nid,
            "endpoint": endpoint_norm,
            "subject": subject or "",
            "title": title or "",
            "body": body or "",
            "data": data_obj or {},
            "created_at": now,
            "delivered": False
        })
        _write_notifications_json(arr)
        return nid
    except Exception:
        logger.exception("save_notification failed")
        return None

def fetch_pending_notifications(endpoint):
    try:
        if not endpoint:
            return []
        endpoint_norm = normalize_endpoint(endpoint)
        if USE_SQLITE and NN_LOCAL_DB:
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

        arr = _read_notifications_json()
        out = [it for it in arr if normalize_endpoint(it.get("endpoint")) == endpoint_norm and not it.get("delivered")]
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

        if USE_SQLITE and NN_LOCAL_DB:
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
            return changed

        arr = _read_notifications_json()
        updated = False
        for it in arr:
            if it.get("id") in ids and normalize_endpoint(it.get("endpoint")) == endpoint_norm and not it.get("delivered"):
                it["delivered"] = True
                it["delivered_at"] = now
                changed += 1
                updated = True
        if updated:
            _write_notifications_json(arr)
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
        icon_url = url_for('static', filename='img/icon-192.png', _external=True)
    except Exception:
        icon_url = None
    payload_template = {"title": title, "body": body, "url": "/questions", "subject": subject, "icon": icon_url, "badge": icon_url, "preview": {"question": q_preview, "attempt": a_preview}}
    detail = []
    sent = 0
    # persist notifications
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
                    # mark delivered in storage
                    ack_notifications(endpoint, [it.get("id") for it in []])  # noop; we already persisted
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
    candidate = Path(app.static_folder) / "img" / "icon-192.png"
    if candidate.exists():
        return send_from_directory(str(candidate.parent), candidate.name)
    logo = Path(app.static_folder) / "img" / "logo.png"
    if logo.exists():
        return send_from_directory(str(logo.parent), logo.name)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="100%" height="100%" fill="#2b8cff"/></svg>'
    return make_response(svg, 200, {"Content-Type": "image/svg+xml"})

@app.route("/")
def home_page():
    return render_template("index.html", message=FOOTER_MESSAGE, quote=get_random_quote(MOTIVATIONAL_QUOTES_VERSES))
# Add this route to app.py (near other page routes)

@app.route("/rules", endpoint="rules_page")
def rules_page():
    """
    Serve the rules page. Try reading from a sheets helper if available,
    otherwise fall back to a local JSON file data/rules.json, or an empty list.
    """
    rules = []
    # try sheets.py first (if the app includes it)
    try:
        import sheets as sheets_mod  # optional dependency
        if hasattr(sheets_mod, "read_rules"):
            try:
                rules = sheets_mod.read_rules() or []
            except Exception:
                # sheet read failed; fallback below
                rules = []
    except Exception:
        # sheets not available — fall back to local JSON
        try:
            rules = safe_load_json(Path("data/rules.json")) or []
        except Exception:
            rules = []

    # Ensure rules is a list for template safety
    if not isinstance(rules, list):
        rules = []

    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)

# Explicit quiz_forms endpoint so templates using url_for('quiz_forms') succeed
@app.route("/quiz-forms", endpoint="quiz_forms")
def quiz_forms():
    forms = safe_load_json(QUIZ_LOCAL_PATH) or []
    return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)

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
    return render_template("content.html",
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

@app.route("/api/vapid")
def api_vapid():
    if not VAPID_PUBLIC_KEY:
        return jsonify({"ok": False, "error": "vapid-not-configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})

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
        return render_template("questions.html", questions=questions, categories=categories_for_ui, selected_category=selected_category, message=FOOTER_MESSAGE)
    except Exception:
        logger.exception("questions_page failed")
        flash("Could not load questions", "danger")
        return render_template("questions.html", questions=[], categories=["Other"], selected_category="", message=FOOTER_MESSAGE)
        
@app.route('/coding')
def coding_page():
    exercises = load_coding_exercises()
    return render_template('coding.html', exercises=exercises, message=FOOTER_MESSAGE)

@app.route("/health")
def health_index():
    plants = safe_load_json(HEALTH_LOCAL_PATH) or []
    return render_template("health.html", plants=plants, message=FOOTER_MESSAGE)

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
    return render_template("health_detail.html", plant=plant, message=FOOTER_MESSAGE)

@app.route("/admin/announcement", methods=["GET", "POST"])
def admin_announcement():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
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
                return render_template("admin_announcement.html", key=provided_key, result=results, message=FOOTER_MESSAGE)
            else:
                res = send_push_to_subject(subject, title, body)
                flash("Announcement attempted.", "success")
                return render_template("admin_announcement.html", key=provided_key, result=res, message=FOOTER_MESSAGE)
        except Exception:
            logger.exception("admin_announcement failed")
            flash("Failed to send announcement.", "danger")
            return render_template("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)
    return render_template("admin_announcement.html", key=provided_key, message=FOOTER_MESSAGE)

@app.route("/admin/exercises", methods=["GET", "POST"])
def admin_exercises():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
    # If GET without key, show empty admin entry view (same UX as other admin routes)
    if request.method == "GET" and not provided_key:
        return render_template("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)

    # Unauthorized key -> same behaviour as other admin routes
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)

    # POST: create or update (append if new) exercise
    if request.method == "POST":
        try:
            # Collect form fields (keep original names used in your admin HTML)
            ex_id = (request.form.get("id") or "").strip()
            title = (request.form.get("title") or "").strip()
            language = (request.form.get("language") or "").strip()
            difficulty = (request.form.get("difficulty") or "").strip()
            description = (request.form.get("description") or "").strip()
            starter = (request.form.get("starter_code") or request.form.get("starter") or "").strip()
            solution = (request.form.get("solution_code") or request.form.get("solution") or "").strip()

            # sample_data may be JSON text (optional)
            sample_raw = (request.form.get("sample_data") or "").strip()
            sample_data = None
            if sample_raw:
                try:
                    sample_data = json.loads(sample_raw)
                except Exception:
                    # tolerate plain text or malformed JSON by keeping None
                    sample_data = None

            # generate id if not provided (ms timestamp, string)
            if not ex_id:
                ex_id = str(int(time.time() * 1000))

            # normalize exercise object
            data = {
                "id": ex_id,
                "title": title,
                "language": language,
                "difficulty": difficulty,
                "description": description,
                "starter": starter,
                "solution": solution,
                "sample_data": sample_data,
                "created_at": datetime.utcnow().replace(microsecond=0).isoformat()
            }

            # load existing exercises (JSON fallback)
            arr = safe_load_json(CODING_LOCAL_PATH) or []
            if not isinstance(arr, list):
                arr = []

            # If an exercise with same id exists -> update it; otherwise append (append behavior requested)
            existing_index = None
            for i, e in enumerate(arr):
                try:
                    if str(e.get("id")) == str(data.get("id")):
                        existing_index = i
                        break
                except Exception:
                    continue

            if existing_index is not None:
                arr[existing_index] = data
                action = "updated"
            else:
                arr.append(data)
                action = "added"

            # persist
            safe_write_json(CODING_LOCAL_PATH, arr)
            flash(f"Exercise {action} successfully.", "success")
        except Exception:
            logger.exception("Failed saving exercise")
            flash("Failed saving exercise", "danger")

        return render_template("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)

    # Fallback (shouldn't be reached) - render page
    return render_template("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)

@app.route("/admin/questions", methods=["GET", "POST"])
def admin_questions():
    provided = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided:
        return render_template("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)
    if provided != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template("admin_questions.html", key="", questions=[], subjects=[], message=FOOTER_MESSAGE)

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

    return render_template("admin_questions.html", key=provided, questions=rows, subjects=subjects, message=FOOTER_MESSAGE)

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

@app.route("/drive-image/<path:file_id>")
def drive_image(file_id: str):
    """
    Serve an image stored on Google Drive by file_id.
    - If Drive API is available and the service account works, fetch the bytes + mimeType and return them.
    - Otherwise fall back to static img/logo.png or icon-192.png.
    """
    # Quick sanity: if the client accidentally passed a filename (e.g. "logo.png"), serve static file directly.
    static_candidate = Path(app.static_folder) / "img" / file_id
    if static_candidate.exists():
        return send_from_directory(str(static_candidate.parent), static_candidate.name)

    # If drive helper available, try to fetch metadata and bytes
    if DRIVE_API_AVAILABLE and gdrive:
        try:
            # get metadata (mimeType, name)
            meta = {}
            try:
                meta = gdrive.get_file_metadata(file_id) or {}
            except Exception:
                meta = {}

            # download file bytes
            data = b""
            try:
                data = gdrive.download_file(file_id) or b""
            except Exception:
                data = b""

            if data:
                # decide mime type from metadata, fallback to guess by name, then to image/png
                mime = (meta.get("mimeType") or mimetypes.guess_type(meta.get("name", ""))[0] or None)
                if not mime:
                    # try to guess image types heuristically (many drive images will be jpeg/png/webp)
                    mime = "image/png"

                file_name = meta.get("name") or f"{file_id}"
                bio = io.BytesIO(data)
                bio.seek(0)

                # set caching headers: browser can cache images for an hour (adjust as needed)
                resp = send_file(bio, mimetype=mime, download_name=file_name, as_attachment=False, conditional=True)
                resp.cache_control.max_age = 3600
                resp.cache_control.public = True
                return resp
            else:
                logger.warning("Drive returned empty bytes for file_id=%s", file_id)
        except Exception:
            logger.exception("drive_image: error fetching %s from Drive", file_id)

    # final fallback(s): try to serve icon-192 or logo
    fallback = Path(app.static_folder) / "img" / "icon-192.png"
    if not fallback.exists():
        fallback = Path(app.static_folder) / "img" / "logo.png"
    if fallback.exists():
        return send_from_directory(str(fallback.parent), fallback.name)

    # If nothing available, return 404
    return abort(404)

@app.route("/exercise-table/<path:name>", methods=["GET", "POST"])
def exercise_table(name: str):
    if request.method == "GET":
        return make_response(jsonify({"ok": True, "endpoint": f"/exercise-table/{name}", "note": "POST SQL to this URL using form-data or JSON with key 'schema'"}), 200)
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
# -----------------------------
# Helper: get subjects for an endpoint (returns list of subject strings)
# -----------------------------
def get_subjects_for_endpoint(endpoint_norm):
    subjects = []
    try:
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute("SELECT DISTINCT subject FROM subscriptions WHERE endpoint = ?", (endpoint_norm,))
                rows = c.fetchall()
                subjects = [r[0] for r in rows if r and r[0]]
            return sorted(list(dict.fromkeys(subjects)))
        # JSON backend:
        with SUBS_LOCK:
            if SUBSCRIPTIONS_FILE.exists():
                try:
                    with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
                        arr = json.load(f) or []
                        if not isinstance(arr, list):
                            arr = []
                except Exception:
                    logger.exception("Failed reading subscriptions.json in get_subjects_for_endpoint")
                    arr = []
            else:
                arr = []
        for r in arr:
            if not isinstance(r, dict):
                continue
            subj = r.get("subject") or "Other"
            sub = r.get("subscription") or {}
            ep = sub.get("endpoint") or sub.get("url")
            try:
                if ep and normalize_endpoint(ep) == endpoint_norm:
                    subjects.append(subj)
            except Exception:
                continue
        return sorted(list(dict.fromkeys(subjects)))
    except Exception:
        logger.exception("get_subjects_for_endpoint failed")
        return []

# -----------------------------
# Route: /save-subscription (keeps the same route name)
# This route now delegates to save_subscription() and returns subjects for the endpoint.
# -----------------------------
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

        # Basic validation
        if (not subscription or not isinstance(subscription, dict)
                or not (subscription.get("endpoint") or subscription.get("url"))):
            return make_response(jsonify({"ok": False, "error": "subscription required"}), 400)

        endpoint = subscription.get("endpoint") or subscription.get("url")
        try:
            normalized_endpoint = normalize_endpoint(endpoint)
        except Exception:
            normalized_endpoint = endpoint

        # Save using existing helper (this will handle SQLite or JSON)
        ok = save_subscription(subject, client_id, subscription)

        # Build subjects list for this endpoint (fresh)
        subjects_for_endpoint = get_subjects_for_endpoint(normalized_endpoint)

        resp = make_response(jsonify({"ok": ok, "subjects": subjects_for_endpoint}), (200 if ok else 500))
        try:
            if ok and normalized_endpoint:
                # set cookie only on successful save
                resp.set_cookie('nn_subscription', normalized_endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax', path='/')
        except Exception:
            pass
        return resp
    except Exception:
        logger.exception("save-subscription failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)

# -----------------------------
# Persistence helper: save_subscription (sqlite OR json)
# This is the corrected version we discussed earlier.
# -----------------------------
def save_subscription(subject, client_id, subscription_obj):
    try:
        # Normalize incoming subscription object
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}

        if not isinstance(subscription_obj, dict):
            logger.warning("Invalid subscription object")
            return False

        endpoint = subscription_obj.get("endpoint") or subscription_obj.get("url")
        if not endpoint:
            logger.warning("Subscription missing endpoint")
            return False

        endpoint_norm = normalize_endpoint(endpoint)
        subject = subject or "Other"
        client_id = client_id or ""

        keys = subscription_obj.get("keys") or {}
        p256 = keys.get("p256dh", "")
        auth = keys.get("auth", "")

        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        # SQLITE BACKEND
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                # Ensure table exists schema should include updated_at (adjust if necessary)
                c.execute(
                    """REPLACE INTO subscriptions
                       (endpoint, subject, client_id, p256dh, auth, raw_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,COALESCE(
                           (SELECT created_at FROM subscriptions WHERE endpoint=? AND subject=?),
                           ?
                       ), ?)""",
                    (
                        endpoint_norm,
                        subject,
                        client_id,
                        p256,
                        auth,
                        json.dumps(subscription_obj, ensure_ascii=False),
                        endpoint_norm,
                        subject,
                        now,
                        now
                    )
                )
                conn.commit()
            return True

        # JSON BACKEND (atomic + locked)
        with SUBS_LOCK:
            arr = []
            try:
                if SUBSCRIPTIONS_FILE.exists():
                    with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
                        arr = json.load(f) or []
                        if not isinstance(arr, list):
                            arr = []
            except Exception:
                logger.exception("Failed reading subscriptions.json, starting fresh")
                arr = []

            new_arr = []
            replaced = False

            for r in arr:
                if not isinstance(r, dict):
                    continue

                sub = r.get("subscription") or {}
                ep = sub.get("endpoint") or sub.get("url")

                try:
                    ep_norm = normalize_endpoint(ep) if ep else None
                except Exception:
                    ep_norm = None

                # Same endpoint + subject → replace
                if ep_norm == endpoint_norm and r.get("subject") == subject:
                    new_arr.append({
                        "endpoint": endpoint_norm,
                        "subject": subject,
                        "client_id": client_id,
                        "keys": {"p256dh": p256, "auth": auth},
                        "subscription": subscription_obj,
                        "created_at": r.get("created_at", now),
                        "updated_at": now
                    })
                    replaced = True
                else:
                    new_arr.append(r)

            if not replaced:
                new_arr.append({
                    "endpoint": endpoint_norm,
                    "subject": subject,
                    "client_id": client_id,
                    "keys": {"p256dh": p256, "auth": auth},
                    "subscription": subscription_obj,
                    "created_at": now,
                    "updated_at": now
                })

            # Atomic write
            SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(SUBSCRIPTIONS_FILE.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(new_arr, tf, ensure_ascii=False, indent=2)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_path, SUBSCRIPTIONS_FILE)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        return True

    except Exception:
        logger.exception("save_subscription failed")
        return False

# -----------------------------
# Deletion helper: delete_subscription (symmetric to save_subscription)
# -----------------------------
def delete_subscription(subject, subscription_obj):
    try:
        if isinstance(subscription_obj, str):
            try:
                subscription_obj = json.loads(subscription_obj)
            except Exception:
                subscription_obj = {"endpoint": subscription_obj}

        if not isinstance(subscription_obj, dict):
            return False

        endpoint = subscription_obj.get("endpoint") or subscription_obj.get("url")
        if not endpoint:
            return False

        endpoint_norm = normalize_endpoint(endpoint)
        subject = subject or "Other"

        # SQLITE BACKEND
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute(
                    "DELETE FROM subscriptions WHERE endpoint=? AND subject=?",
                    (endpoint_norm, subject)
                )
                conn.commit()
            return True

        # JSON BACKEND
        with SUBS_LOCK:
            arr = []
            try:
                if SUBSCRIPTIONS_FILE.exists():
                    with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
                        arr = json.load(f) or []
                        if not isinstance(arr, list):
                            arr = []
            except Exception:
                logger.exception("Failed reading subscriptions.json in delete_subscription")
                arr = []

            filtered = []
            for r in arr:
                try:
                    sub = r.get("subscription") or {}
                    ep = sub.get("endpoint") or sub.get("url")
                    ep_norm = normalize_endpoint(ep) if ep else None
                except Exception:
                    ep_norm = None

                # keep entries that do NOT match the endpoint+subject
                if not (ep_norm == endpoint_norm and r.get("subject") == subject):
                    filtered.append(r)

            # Atomic write of filtered list
            SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(SUBSCRIPTIONS_FILE.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(filtered, tf, ensure_ascii=False, indent=2)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_path, SUBSCRIPTIONS_FILE)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        return True

    except Exception:
        logger.exception("delete_subscription failed")
        return False

# -----------------------------
# Route: /unsubscribe (symmetric server endpoint)
# This is optional to call from client; it deletes the subscription and clears cookie.
# -----------------------------
@app.route("/unsubscribe", methods=["POST"])
def unsubscribe_route():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = payload.get("subscription") or payload
        subject = (payload.get("subject") or "Other")

        ok = delete_subscription(subject, subscription)

        resp = make_response(jsonify({"ok": ok}), (200 if ok else 500))
        # clear cookie (best-effort)
        try:
            resp.delete_cookie("nn_subscription", path='/')
        except Exception:
            pass
        return resp
    except Exception:
        logger.exception("unsubscribe failed")
        return make_response(jsonify({"ok": False}), 500)
             
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
    try:
        endpoint = (
            request.args.get("endpoint", "").strip()
            or (request.cookies.get("nn_subscription") or "").strip()
        )

        # IMPORTANT FIX: never return 400 here
        if not endpoint:
            return jsonify({"ok": True, "items": []}), 200

        items = fetch_pending_notifications(endpoint)
        return jsonify({"ok": True, "items": items}), 200

    except Exception as e:
        app.logger.exception("notifications_pending failed")
        return jsonify({"ok": False, "items": []}), 200


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
    # try to render a 404 template if present
    try:
        return render_template("errors/404.html", message=FOOTER_MESSAGE), 404
    except Exception:
        return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 404

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
    if request.is_json or request.path.startswith("/api/"):
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)
    return render_template("error.html", status=500, message=FOOTER_MESSAGE, error=str(e)), 500

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
    logger.info("Starting app on port %s (debug=%s, use_sqlite=%s)", port, debug_mode, USE_SQLITE)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
