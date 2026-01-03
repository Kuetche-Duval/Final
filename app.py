# Unified app.py — corrected version with schema/migration fixes and robust JSON fallback
# Notes:
# - SQLite schema for subscriptions now uses composite PK (endpoint, subject) and includes updated_at
# - initialize_drive_helpers() is defined before being called
# - single, corrected save_subscription/delete_subscription implementations (JSON + SQLite)
# - safe_write_json used where possible
# - created_at preserved on updates; updated_at added
# - basic migration helper included for sqlite schema changes
# - Warning: SUBS_LOCK protects only threads in the same process; for multi-process deployment use SQLite

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

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("newtons_nerds")

# Paths & config
# Use app.root_path so relative executions still find data dir
DATA_DIR = Path(app.root_path) / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# lock to serialize subscription file writes (thread-local only)
SUBS_LOCK = threading.Lock()

CODING_LOCAL_PATH = Path(os.environ.get("CODING_EXERCISES_PATH", DATA_DIR / "coding_exercises.json"))
HEALTH_LOCAL_PATH = Path(os.environ.get("HEALTH_DATA_PATH", DATA_DIR / "health_data.json"))
QUIZ_LOCAL_PATH = Path(os.environ.get("QUIZ_FORMS_PATH", DATA_DIR / "quiz_forms.json"))
NOTIFICATIONS_FILE = Path(os.environ.get("NOTIFICATIONS_FILE", DATA_DIR / "notifications.json"))
SUBSCRIPTIONS_FILE = Path(os.environ.get("SUBSCRIPTIONS_FILE", DATA_DIR / "subscriptions.json"))

# DB choice: by default use JSON files, unless USE_SQLITE env var is true
USE_SQLITE = str(os.environ.get("USE_SQLITE", "")).lower() in ("1", "true", "yes")
NN_LOCAL_DB = Path(os.environ.get("NN_LOCAL_DB", "nn_local.db")).resolve() if USE_SQLITE else None

MOTIVATIONAL_QUOTES_VERSES = [
    {"verse": "I can do all things through Christ who strengthens me.", "reference": "Philippians 4:13"},
    {"verse": "If any of you lacks wisdom, let him ask God, who gives generously to all without reproach, and it will be given him.", "reference": "James 1:5"},
    {"verse": "Trust in the Lord with all your heart, and do not lean on your own understanding. In all your ways acknowledge him, and he will make straight your paths.", "reference": "Proverbs 3:5-6"},
    {"verse": "For I know the plans I have for you, declares the Lord, plans for welfare and not for evil, to give you a future and a hope.", "reference": "Jeremiah 29:11"},
    {"verse": "Whatever you do, work heartily, as for the Lord and not for men.", "reference": "Colossians 3:23"},
    {"verse": "Have I not commanded you? Be strong and courageous. Do not be frightened, and do not be dismayed, for the Lord your God is with you wherever you go.", "reference": "Joshua 1:9"},
    {"verse": "Do not be anxious about anything, but in everything by prayer and supplication with thanksgiving let your requests be made known to God.", "reference": "Philippians 4:6"},
    {"verse": "For God gave us a spirit not of fear but of power and love and self-control.", "reference": "2 Timothy 1:7"},
    {"verse": "But they who wait for the Lord shall renew their strength; they shall mount up with wings like eagles; they shall run and not be weary; they shall walk and not faint.", "reference": "Isaiah 40:31"},
    {"verse": "Let no one despise you for your youth, but set the believers an example in speech, in conduct, in love, in faith, in purity.", "reference": "1 Timothy 4:12"}
]
get_random_quote = lambda arr: random.choice(arr)
FOOTER_MESSAGE = os.environ.get("FOOTER_MESSAGE", "Love one another ❤")
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

VAPID_PUBLIC_KEY = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
VAPID_PRIVATE_KEY = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

ADMIN_KEY = os.environ.get("ADMIN_KEY", "") or ""

# Drive & Sheets helper globals (populated by initialize_drive_helpers)
gdrive = None
gsheets = None
DRIVE_API_AVAILABLE = False
SHEETS_AVAILABLE = False

def admin_required(f):
    """Decorator to protect routes with the admin key"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        provided = request.args.get("key") or request.form.get("admin_key") or ""
        if provided != ADMIN_KEY:
            abort(403)  # Forbidden
        return f(*args, **kwargs)
    return decorated

# ----------------------------
# Helpers
# ----------------------------

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


# --- Drive / Sheets initialization ---
# --- Drive / Sheets initialization ---
def initialize_drive_helpers():
    """
    Import and initialize drive and sheets helpers once.
    Returns a dict with status and error messages.
    """
    global gdrive, gsheets, DRIVE_API_AVAILABLE, SHEETS_AVAILABLE
    info = {"gdrive_available": False, "gsheets_available": False, "errors": []}

    # Check credentials file
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

    # ----- Drive helper -----
    try:
        import drive as local_drive
        gdrive = local_drive
        if hasattr(gdrive, "initialize") and callable(gdrive.initialize):
            try:
                gdrive.initialize(str(cp))
            except Exception as e:
                logger.exception("drive.initialize failed: %s", e)
                info["errors"].append(f"drive.initialize failed: {e}")
            else:
                info["gdrive_available"] = True
                DRIVE_API_AVAILABLE = True
        else:
            # module exists but has no initialize; still usable
            info["gdrive_available"] = True
            DRIVE_API_AVAILABLE = True
        logger.info("Drive helper loaded from %s", getattr(gdrive, "__file__", "<module>"))
    except Exception as e:
        info["errors"].append(f"drive import failed: {e}")
        logger.warning("Drive helper import failed: %s", e)

    # ----- Sheets helper -----
    try:
        import sheets as local_sheets
        gsheets = local_sheets
        if hasattr(gsheets, "initialize") and callable(gsheets.initialize):
            try:
                gsheets.initialize(str(cp))
            except Exception as e:
                logger.exception("sheets.initialize failed: %s", e)
                info["errors"].append(f"sheets.initialize failed: {e}")
            else:
                info["gsheets_available"] = True
                SHEETS_AVAILABLE = True
        else:
            # module exists but has no initialize; still usable
            info["gsheets_available"] = True
            SHEETS_AVAILABLE = True
        logger.info("Sheets helper loaded from %s", getattr(gsheets, "__file__", "<module>"))
    except Exception as e:
        info["errors"].append(f"sheets import failed: {e}")
        logger.warning("Sheets helper import failed: %s", e)

    return info

# Ensure Google credentials from base64 if provided (this may create a service account file)
try:
    GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "service_account.json")
    try:
        ensure_google_credentials_from_base64(
            env_var_name="SERVICE_ACCOUNT_JSON_B64",
            output_path=GOOGLE_CREDS_PATH
        )
        os.environ["SERVICE_ACCOUNT_JSON_PATH"] = GOOGLE_CREDS_PATH
    except Exception as e:
        logger.warning("Google credentials not initialized: %s", e)
except Exception:
    logger.exception("Unexpected error while attempting to initialize google credentials")

# DB init and migration helpers
def _migrate_subscriptions_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(subscriptions)")
        cols = [r[1] for r in cur.fetchall()]
        # If table doesn't exist, nothing to do
        if not cols:
            return
        if "updated_at" in cols and "subject" in cols:
            return
        # Perform migration: create new table and copy data
        cur.executescript("""
        BEGIN TRANSACTION;
        CREATE TABLE IF NOT EXISTS subscriptions_new (
            endpoint TEXT NOT NULL,
            subject TEXT NOT NULL,
            client_id TEXT,
            p256dh TEXT,
            auth TEXT,
            raw_json TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (endpoint, subject)
        );
        INSERT OR IGNORE INTO subscriptions_new (endpoint, subject, client_id, p256dh, auth, raw_json, created_at)
          SELECT endpoint, COALESCE(subject, 'Other'), client_id, p256dh, auth, raw_json, created_at FROM subscriptions;
        DROP TABLE subscriptions;
        ALTER TABLE subscriptions_new RENAME TO subscriptions;
        COMMIT;
        """)
        conn.commit()
        logger.info("Migrated subscriptions schema to include subject/updated_at")
    except Exception:
        logger.exception("Migration of subscriptions schema failed; manual intervention may be required")


def _init_sqlite_db(db_path: Path):
    try:
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        # Create subscriptions table with composite PK (endpoint, subject)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                endpoint TEXT NOT NULL,
                subject TEXT NOT NULL,
                client_id TEXT,
                p256dh TEXT,
                auth TEXT,
                raw_json TEXT,
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (endpoint, subject)
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
        # Also attempt migrations if needed (safe no-op in most cases)
        try:
            _migrate_subscriptions_schema(conn)
        except Exception:
            logger.exception("Subscription schema migration attempt failed")
        conn.close()
        logger.info("SQLite DB initialized at %s", db_path)
    except Exception:
        logger.exception("DB init failed")

if USE_SQLITE and NN_LOCAL_DB:
    _init_sqlite_db(NN_LOCAL_DB)
else:
    logger.info("Using JSON storage for subscriptions / notifications (no sqlite). Set USE_SQLITE=true to enable SQLite.")

# JSON helpers

def validate_json_file(path: Path) -> tuple[bool, str]:
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
    """
    try:
        path = CODING_LOCAL_PATH if isinstance(CODING_LOCAL_PATH, Path) else Path(CODING_LOCAL_PATH)
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
                        "subject": r.get("subject", ""),
                        "user": r.get("name", r.get("user", "")),
                        "text": r.get("question", r.get("text", "")),
                        "attempt": r.get("attempt", "")
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

def render_template_safe(template_name: str, status: int = 200, **context) -> Response:
    try:
        html = render_template(template_name, **context)
        return make_response(html, status)
    except Exception as e:
        logger.warning("Template %s failed to render: %s", template_name, e)
        try:
            # fallback to error.html if present
            html = render_template("error.html", message=FOOTER_MESSAGE, error=str(e), traceback=traceback.format_exc())
            return make_response(html, 500)
        except Exception:
            # last-resort plaintext
            body = f"Template rendering error: {template_name}\n\n{e}\n\n{traceback.format_exc()}"
            return make_response(body, 500)

# JSON-backed subscriptions / notifications (fallback)
def _read_subscriptions_json():
    arr = safe_load_json(SUBSCRIPTIONS_FILE) or []
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

# -----------------------------
# Persistence helpers (single authoritative implementations)
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
                # REPLACE-like behavior but preserve created_at when present
                c.execute("SELECT created_at FROM subscriptions WHERE endpoint=? AND subject=?", (endpoint_norm, subject))
                row = c.fetchone()
                created_at = row[0] if row and row[0] else now
                c.execute(
                    """REPLACE INTO subscriptions
                       (endpoint, subject, client_id, p256dh, auth, raw_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        endpoint_norm,
                        subject,
                        client_id,
                        p256,
                        auth,
                        json.dumps(subscription_obj, ensure_ascii=False),
                        created_at,
                        now,
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
                os.replace(tmp_path, str(SUBSCRIPTIONS_FILE))
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


def save_notification(endpoint, subject, title, body, data_obj=None):
    try:
        if not endpoint:
            logger.debug("save_notification skipped: no endpoint")
            return None
        endpoint_norm = normalize_endpoint(endpoint)
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

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
        max_id = max([int(it.get("id", 0)) for it in arr], default=0)
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
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
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

# Push helper
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
            payload["body"] = payload.get("body", "")[:200] + "…"
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
                    # No-op here; notifications were persisted earlier
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

# -----------------------------
# Routes
# -----------------------------

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

@app.route("/rules", endpoint="rules_page")
@admin_required
def rules_page():
    """
    Serve the rules page.
    - Try gsheets.read_rules() if available
    - Else fall back to local JSON data/rules.json
    """
    rules = []

    # Use gsheets global
    if gsheets and SHEETS_AVAILABLE and hasattr(gsheets, "read_rules"):
        try:
            rules = gsheets.read_rules() or []
        except Exception:
            logger.exception("gsheets.read_rules failed")
            rules = []

    # fallback to JSON if no rules
    if not rules:
        try:
            rules = safe_load_json(Path("data/rules.json")) or []
        except Exception:
            rules = []

    if not isinstance(rules, list):
        rules = []

    return render_template("rules.html", rules=rules, message=FOOTER_MESSAGE)

@app.route("/quiz-forms", endpoint="quiz_forms")
def quiz_forms():
    forms = safe_load_json(QUIZ_LOCAL_PATH) or []
    return render_template("quiz_forms.html", forms=forms, message=FOOTER_MESSAGE)


@app.route("/quizzes")
def quizzes_redirect():
    return redirect(url_for("quiz_forms"))


@app.route("/content")
@admin_required
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
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            qfile = DATA_DIR / "questions_local.json"
            try:
                arr = json.loads(qfile.read_text(encoding="utf-8") or "[]") if qfile.exists() else []
            except Exception:
                arr = []
            arr.append([category, name, question, attempt, timestamp])
            try:
                safe_write_json(qfile, arr)
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
        
@app.route("/coding")
def coding_page():
    exercises = safe_load_json(CODING_LOCAL_PATH) or []
    return render_template_safe("coding.html", exercises=exercises, message=FOOTER_MESSAGE)

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
            if p.get("slug") == slug or p.get("name", "").lower().replace(" ", "-") == slug:
                plant = p
                break
        except Exception:
            continue
    if not plant:
        plant = {"name": slug.replace("-", " ").title(), "virtues": [], "uses": "No details available.", "notes": "", "slug": slug}
    return render_template("health_detail.html", plant=plant, message=FOOTER_MESSAGE)


@app.route("/admin/announcement", methods=["GET", "POST"])
@admin_required
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

# Admin exercises route
@app.route("/admin/exercises", methods=["GET", "POST"]) 
def admin_exercises():
    provided_key = request.args.get("key") or request.form.get("admin_key") or ""
    if request.method == "GET" and not provided_key:
        return render_template("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)
    if provided_key != ADMIN_KEY:
        flash("Unauthorized admin key.", "warning")
        return render_template("admin_exercises.html", key="", drive_file_id="", message=FOOTER_MESSAGE)
    if request.method == "POST":
        try:
            ex_id = (request.form.get("id") or "").strip()
            title = (request.form.get("title") or "").strip()
            language = (request.form.get("language") or "").strip()
            difficulty = (request.form.get("difficulty") or "").strip()
            description = (request.form.get("description") or "").strip()
            starter = (request.form.get("starter_code") or request.form.get("starter") or "").strip()
            solution = (request.form.get("solution_code") or request.form.get("solution") or "").strip()
            sample_raw = (request.form.get("sample_data") or "").strip()
            sample_data = None
            if sample_raw:
                try:
                    sample_data = json.loads(sample_raw)
                except Exception:
                    sample_data = None
            if not ex_id:
                ex_id = str(int(time.time() * 1000))
            arr = safe_load_json(CODING_LOCAL_PATH) or []
            if not isinstance(arr, list):
                arr = []
            existing_index = None
            for i, e in enumerate(arr):
                try:
                    if str(e.get("id")) == str(ex_id):
                        existing_index = i
                        break
                except Exception:
                    continue
            now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            data = {
                "id": ex_id,
                "title": title,
                "language": language,
                "difficulty": difficulty,
                "description": description,
                "starter": starter,
                "solution": solution,
                "sample_data": sample_data,
                "created_at": now
            }
            if existing_index is not None:
                orig = arr[existing_index] or {}
                data["created_at"] = orig.get("created_at", data["created_at"]) or data["created_at"]
                data["updated_at"] = now
                arr[existing_index] = data
                action = "updated"
            else:
                data["updated_at"] = now
                arr.append(data)
                action = "added"
            safe_write_json(CODING_LOCAL_PATH, arr)
            flash(f"Exercise {action} successfully.", "success")
        except Exception:
            logger.exception("Failed saving exercise")
            flash("Failed saving exercise", "danger")
        return render_template("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)
    return render_template("admin_exercises.html", key=provided_key, drive_file_id="", message=FOOTER_MESSAGE)

# Admin questions
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
            safe_write_json(qfile, new)
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
                safe_write_json(qfile, new)
                flash(f"Deleted questions for subject {subj}.", "success")
            except Exception:
                logger.exception("Failed deleting subject")
                flash("Failed deleting subject", "danger")
        return redirect(url_for("admin_questions", key=provided))
    return render_template("admin_questions.html", key=provided, questions=rows, subjects=subjects, message=FOOTER_MESSAGE)


# drive image route
@app.route("/drive-image/<path:file_id>")
def drive_image(file_id: str):
    """
    Serve an image stored on Google Drive by file_id.
    - If gdrive.fetch_drive_image_bytes is present and returns bytes, send them.
    - Otherwise fall back to static img/logo.png.
    """
    try:
        if gdrive and hasattr(gdrive, "fetch_drive_image_bytes"):
            try:
                data, content_type = gdrive.fetch_drive_image_bytes(file_id)
                if data:
                    return Response(data, mimetype=content_type)
            except Exception:
                logger.exception("drive_image via gdrive failed")
    except Exception:
        logger.exception("drive_image top-level error")

    # fallback to static logo
    logo = Path(app.static_folder) / "img" / "logo.png"
    if logo.exists():
        return send_from_directory(str(logo.parent), logo.name)
    return abort(404)


# exercise_table route (safe sqlite preview)
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


# subjects helper
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


# save-subscription route
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
        if (not subscription or not isinstance(subscription, dict)
                or not (subscription.get("endpoint") or subscription.get("url"))):
            return make_response(jsonify({"ok": False, "error": "subscription required"}), 400)
        endpoint = subscription.get("endpoint") or subscription.get("url")
        try:
            normalized_endpoint = normalize_endpoint(endpoint)
        except Exception:
            normalized_endpoint = endpoint
        ok = save_subscription(subject, client_id, subscription)
        subjects_for_endpoint = get_subjects_for_endpoint(normalized_endpoint)
        resp = make_response(jsonify({"ok": ok, "subjects": subjects_for_endpoint}), (200 if ok else 500))
        try:
            if ok and normalized_endpoint:
                resp.set_cookie('nn_subscription', normalized_endpoint, max_age=COOKIE_MAX_AGE, samesite='Lax', path='/')
        except Exception:
            pass
        return resp
    except Exception:
        logger.exception("save-subscription failed")
        return make_response(jsonify({"ok": False, "error": "server-error"}), 500)


# delete_subscription implementation
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
        if USE_SQLITE and NN_LOCAL_DB:
            with sqlite3.connect(str(NN_LOCAL_DB)) as conn:
                c = conn.cursor()
                c.execute(
                    "DELETE FROM subscriptions WHERE endpoint=? AND subject=?",
                    (endpoint_norm, subject)
                )
                conn.commit()
            return True
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
                if not (ep_norm == endpoint_norm and r.get("subject") == subject):
                    filtered.append(r)
            SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(SUBSCRIPTIONS_FILE.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(filtered, tf, ensure_ascii=False, indent=2)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_path, str(SUBSCRIPTIONS_FILE))
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


# unsubscribe route
@app.route("/unsubscribe", methods=["POST"]) 
def unsubscribe_route():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        subscription = payload.get("subscription") or payload
        subject = (payload.get("subject") or "Other")
        ok = delete_subscription(subject, subscription)
        resp = make_response(jsonify({"ok": ok}), (200 if ok else 500))
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
    try:
        return render_template("errors/404.html", message=FOOTER_MESSAGE), 404
    except Exception:
        return render_template("error.html", message=FOOTER_MESSAGE, error=str(e)), 404


@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
    if request.is_json or request.path.startswith("/api/"):
        return make_response(jsonify({"ok": False, "error": str(e)}), 500)
    # In production you may want to hide `str(e)` to avoid leaking internals
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


# Safe helper used by exercise_table
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


# Now attempt Drive/Sheets initialization (guarded)
try:
    drive_init_info = initialize_drive_helpers()
    logger.info("Drive init: %s", drive_init_info)
except Exception as err:
    logger.exception("Drive initialization failed at runtime: %s", err)
    drive_init_info = {"gdrive_available": False, "gsheets_available": False, "errors": [str(err)]}

# Show template status
_log_templates()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    logger.info("Starting app on port %s (debug=%s, use_sqlite=%s)", port, debug_mode, USE_SQLITE)
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
