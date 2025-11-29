#!/usr/bin/env python3
# app.py — Newton's Nerds (complete, consolidated app)
# Place at project root (same folder as templates/ static/ data/ etc.)

import os
import io
import json
import sqlite3
import traceback
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, send_from_directory, send_file, abort
)

# Optional push library
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception:
    webpush = None
    WebPushException = Exception
    PYWEBPUSH_AVAILABLE = False

# -------------------------
# Logging & App init
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("newtons_nerds")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["JSON_SORT_KEYS"] = False

# -------------------------
# Paths and files
# -------------------------
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CODING_JSON = DATA_DIR / "coding_exercises.json"
SUBSCRIPTIONS_FILE = DATA_DIR / "subscriptions.json"
QUESTIONS_FILE = DATA_DIR / "questions.json"
QUIZ_FORMS_FILE = DATA_DIR / "quiz_forms.json"
HEALTH_FILE = DATA_DIR / "health_data.json"
NOTIFICATIONS_STORE = DATA_DIR / "pending_notifications.json"

# ensure defaults exist
defaults = {
    CODING_JSON: "[]",
    SUBSCRIPTIONS_FILE: "{}",
    QUESTIONS_FILE: "[]",
    QUIZ_FORMS_FILE: "[]",
    HEALTH_FILE: "[]",
    NOTIFICATIONS_STORE: "[]",
}
for p, text in defaults.items():
    if not p.exists():
        try:
            p.write_text(text, encoding="utf-8")
        except Exception:
            logger.exception("Unable to create default file %s", p)

# constants
SQL_TABLE_ROW_LIMIT = int(os.environ.get("SQL_TABLE_ROW_LIMIT", 200))
MAX_PAYLOAD_BYTES = int(os.environ.get("MAX_PUSH_PAYLOAD_BYTES", 3000))
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# -------------------------
# JSON file helpers
# -------------------------
def _read_json_file(path: Path, default=None):
    try:
        if not path.exists():
            return default
        data = path.read_text(encoding="utf-8")
        return json.loads(data or "null")
    except Exception:
        logger.exception("Failed to read JSON from %s", path)
        return default

def _write_json_file(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        logger.exception("Failed to write JSON to %s", path)
        try:
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception:
            logger.exception("Fallback write also failed for %s", path)
            return False

# -------------------------
# Subscriptions persistence
# -------------------------
def load_subscriptions() -> Dict[str, Dict]:
    subs = _read_json_file(SUBSCRIPTIONS_FILE, default={}) or {}
    if not isinstance(subs, dict):
        return {}
    return subs

def save_subscriptions(subs: Dict[str, Dict]) -> bool:
    return _write_json_file(SUBSCRIPTIONS_FILE, subs)

def normalize_endpoint(endpoint: Optional[str]) -> Optional[str]:
    if not endpoint:
        return endpoint
    try:
        p = urlparse(endpoint)
        base = f"{p.scheme}://{p.netloc}{p.path}"
        return base.rstrip('/')
    except Exception:
        return endpoint

def delete_subscriptions_by_endpoint(endpoint: str):
    try:
        subs = load_subscriptions()
        if not subs:
            return
        norm = normalize_endpoint(endpoint)
        keys_to_delete = []
        for k, v in list(subs.items()):
            k_norm = normalize_endpoint(k)
            stored_endpoint = None
            try:
                stored_endpoint = v.get("subscription", {}).get("endpoint")
            except Exception:
                stored_endpoint = None
            if k == endpoint or k_norm == norm or stored_endpoint == endpoint or normalize_endpoint(stored_endpoint) == norm:
                keys_to_delete.append(k)
        for k in keys_to_delete:
            logger.info("Removing subscription row for endpoint key=%s", k)
            subs.pop(k, None)
        if keys_to_delete:
            save_subscriptions(subs)
    except Exception:
        logger.exception("Error deleting subscriptions for %s", endpoint)

# -------------------------
# Notifications fallback store
# -------------------------
def save_notification_for_endpoint(endpoint: Optional[str], subject: str, title: str, body: str, preview: Optional[dict] = None):
    if not endpoint:
        return
    try:
        arr = _read_json_file(NOTIFICATIONS_STORE, default=[]) or []
        arr = list(arr)
        arr.append({
            "endpoint": endpoint,
            "subject": subject,
            "title": title,
            "body": body,
            "preview": preview,
            "created_at": datetime.utcnow().isoformat() + "Z"
        })
        _write_json_file(NOTIFICATIONS_STORE, arr)
    except Exception:
        logger.exception("Failed saving fallback notification for %s", endpoint)

# -------------------------
# Exercises & other data loaders
# -------------------------
def load_coding_exercises() -> List[dict]:
    try:
        arr = _read_json_file(CODING_JSON, default=[]) or []
        if isinstance(arr, list):
            return arr
        if isinstance(arr, dict) and "exercises" in arr:
            return arr["exercises"]
    except Exception:
        logger.exception("load_coding_exercises failed")
    return []

def _load_forms_data():
    try:
        arr = _read_json_file(QUIZ_FORMS_FILE, default=[]) or []
        return arr
    except Exception:
        logger.exception("quiz_forms.json load failed")
    return []

def _load_health_data():
    try:
        arr = _read_json_file(HEALTH_FILE, default=[]) or []
        if isinstance(arr, list):
            return arr
    except Exception:
        logger.exception("health_data.json load failed")
    return [
        {"slug": "aloe-vera", "name": "Aloe vera", "virtues": ["skin-soothing"], "uses": "Topical", "notes": ""},
        {"slug": "ginger", "name": "Ginger", "virtues": ["antiemetic"], "uses": "Tea", "notes": ""}
    ]

# Questions storage
def load_questions():
    try:
        arr = _read_json_file(QUESTIONS_FILE, default=[]) or []
        if isinstance(arr, list):
            return arr
    except Exception:
        logger.exception("Failed loading questions")
    return []

def save_question_item(item):
    q = load_questions()
    q.append(item)
    _write_json_file(QUESTIONS_FILE, q)

# -------------------------
# SQL helpers (fix & populate)
# -------------------------
def fix_common_sql_errors(sql: str) -> str:
    if not sql:
        return sql
    s = sql
    s = re.sub(r'CREATE\s+TABLE\s+([`"\[]?[\w-]+[`"\]]?)\s+IF\s+NOT\s+EXISTS', r'CREATE TABLE IF NOT EXISTS \1', s, flags=re.IGNORECASE)
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    s = s.strip()
    return s

def safe_create_and_populate_sqlite(schema_sql: str, sample_data) -> Tuple[sqlite3.Connection, List[str]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    populated = []
    if schema_sql:
        try:
            schema_sql = fix_common_sql_errors(schema_sql)
            cur.executescript(schema_sql)
        except Exception as e:
            # try to execute statements individually with fixes
            try:
                for stmt in [s.strip() for s in schema_sql.split(';') if s.strip()]:
                    try:
                        cur.execute(stmt)
                    except Exception:
                        stmt_fixed = fix_common_sql_errors(stmt)
                        try:
                            cur.execute(stmt_fixed)
                        except Exception:
                            logger.debug("schema statement failed: %s", stmt)
                conn.commit()
            except Exception:
                raise RuntimeError(f"Schema execution error: {e}")
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
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                    if cur.fetchone():
                        populated.append(table)
                    continue
                except Exception:
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
                    vals = []
                    if isinstance(r, dict):
                        for c in cols:
                            v = r.get(c, None)
                            vals.append(None if v is None else str(v))
                    else:
                        vals = [None] * len(cols)
                    cur.execute(insert_sql, vals)
                except Exception:
                    continue
            populated.append(table)
    conn.commit()
    return conn, populated

def build_tables_html_from_conn(conn: sqlite3.Connection) -> str:
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [r[0] for r in cur.fetchall()]
    except Exception:
        tables = []
    if not tables:
        return '<div class="small text-muted">No sample tables available for this exercise.</div>'
    html_parts = []
    for tbl in tables:
        try:
            cur.execute(f'SELECT * FROM "{tbl}" LIMIT {SQL_TABLE_ROW_LIMIT}')
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            tbl_html = []
            tbl_html.append(f'<div class="mb-2"><strong>Table: {tbl}</strong></div>')
            tbl_html.append('<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>')
            for c in cols:
                tbl_html.append(f'<th>{c}</th>')
            tbl_html.append('</tr></thead><tbody>')
            if not rows:
                tbl_html.append('<tr><td colspan="%d"><em>No rows</em></td></tr>' % (len(cols) or 1))
            else:
                for r in rows:
                    tbl_html.append('<tr>')
                    for c in cols:
                        try:
                            val = r[c]
                        except Exception:
                            val = None
                        tbl_html.append('<td>%s</td>' % ("" if val is None else str(val)))
                    tbl_html.append('</tr>')
            tbl_html.append('</tbody></table></div>')
            html_parts.append("".join(tbl_html))
        except Exception as e:
            html_parts.append(f'<div class="alert alert-warning small">Could not read table {tbl}: {e}</div>')
    return "".join(html_parts)

# -------------------------
# Push sending (robust)
# -------------------------
def send_push_to_subject(subject: str, question_text: str = "", attempt_text: str = "") -> Dict[str, Any]:
    subs = load_subscriptions() or {}
    targets = []
    for endpoint_key, entry in subs.items():
        try:
            if not isinstance(entry, dict):
                continue
            entry_sub = entry.get("subscription")
            entry_subjects = entry.get("subjects", [])
            if any((s or "").strip().lower() == (subject or "").strip().lower() for s in entry_subjects):
                if isinstance(entry_sub, dict):
                    targets.append(entry_sub)
                else:
                    try:
                        targets.append(json.loads(entry_sub))
                    except Exception:
                        targets.append({"endpoint": endpoint_key})
        except Exception:
            continue

    if not targets:
        logger.info("No subscribers for subject %s", subject)
        return {"sent": 0, "errors": ["no subscribers"], "detail": []}

    q_preview = (question_text or "").strip()
    a_preview = (attempt_text or "").strip()
    if len(q_preview) > 140:
        q_preview = q_preview[:137] + "…"
    if len(a_preview) > 180:
        a_preview = a_preview[:177] + "…"

    payload_template = {
        "title": f"New question in {subject}",
        "body": f"Q: {q_preview}\nA: {a_preview}",
        "url": "/questions",
        "tag": f"subject-{re.sub(r'\\s+','-', subject).lower()}",
        "preview": {"question": q_preview, "attempt": a_preview}
    }

    results = []
    sent = 0

    if (not PYWEBPUSH_AVAILABLE) or (not VAPID_PRIVATE_KEY):
        logger.warning("pywebpush or VAPID not configured. Persisting notifications instead of sending.")
        for sub in targets:
            try:
                endpoint = sub.get("endpoint") if isinstance(sub, dict) else None
                save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
                results.append({"endpoint": endpoint, "ok": False, "error": "pywebpush-not-configured"})
            except Exception:
                results.append({"endpoint": None, "ok": False, "error": "persist-failed"})
        return {"sent": 0, "errors": ["pywebpush/VAPID not available"], "detail": results}

    for sub in targets:
        endpoint = sub.get("endpoint")
        try:
            save_notification_for_endpoint(endpoint, subject, payload_template["title"], payload_template["body"], payload_template.get("preview"))
        except Exception:
            logger.debug("save_notification_for_endpoint failed for %s", endpoint)

        payload = dict(payload_template)
        parsed = urlparse(endpoint or "")
        domain = parsed.netloc.lower() if parsed.netloc else ""

        if "notify.windows.com" in domain or "wns" in domain or "windows" in domain:
            payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}

        data_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(data_bytes) > MAX_PAYLOAD_BYTES:
            payload["preview"] = None
            payload["body"] = (payload.get("body","")[:200] + "…")
            data_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')

        attempt = 0
        while attempt < 2:
            attempt += 1
            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_SUBJECT}
                )
                sent += 1
                results.append({"endpoint": endpoint, "ok": True})
                break
            except WebPushException as ex:
                try:
                    resp = getattr(ex, "response", None)
                    status = getattr(resp, "status_code", None) if resp else None
                    body_text = None
                    try:
                        if resp and hasattr(resp, "content"):
                            body_text = resp.content.decode("utf-8", errors="ignore")
                    except Exception:
                        body_text = None
                except Exception:
                    status = None
                    body_text = None
                err_text = str(ex)
                logger.warning("WebPushException endpoint=%s status=%s body=%s err=%s", endpoint, status, body_text, err_text)

                if status in (404, 410) or (body_text and "unsubscribed" in body_text.lower()) or ("unsubscribed" in err_text.lower()):
                    try:
                        delete_subscriptions_by_endpoint(endpoint)
                        logger.info("Deleted stale subscription endpoint %s", normalize_endpoint(endpoint))
                    except Exception:
                        logger.exception("Failed deleting stale subscription %s", endpoint)
                    results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text, "deleted": True})
                    break

                if status == 400 or ("400 Bad Request" in err_text):
                    if attempt < 2:
                        time.sleep(0.12)
                        payload = {"title": payload_template["title"], "body": (q_preview or a_preview)[:80], "url": "/questions"}
                        continue
                    else:
                        results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text})
                        break

                if attempt < 2:
                    time.sleep(0.12)
                    continue
                else:
                    results.append({"endpoint": endpoint, "ok": False, "status": status, "error": err_text})
                    break
            except Exception as ex:
                logger.exception("Unexpected error sending push to %s", endpoint)
                results.append({"endpoint": endpoint, "ok": False, "error": str(ex)})
                break

    logger.info("send_push_to_subject result: %s", {"sent": sent, "errors": [r for r in results if not r.get("ok")], "detail": results})
    return {"sent": sent, "errors": [r for r in results if not r.get("ok")], "detail": results}

# -------------------------
# Routes
# -------------------------
@app.route("/")
def home_page():
    return render_template("index.html", message="Welcome to Newton's Nerds", FOOTER_MESSAGE="© 2025 Newton's Nerds")

# Content page (timetable/drive/forms)
@app.route("/content")
def content_page():
    try:
        sheet_content = []
        sheet_title = []
        dr_values = []
        form_list = []
        today_date = datetime.utcnow().date().isoformat()
        timetable_today = []

        try:
            import sheets  # optional
            if hasattr(sheets, "get_timetable"):
                sheet_title, sheet_content = sheets.get_timetable()
                timetable_today = []
                for row in sheet_content:
                    if not row:
                        continue
                    timetable_today.append({
                        "day": row[0] if len(row) > 0 else "",
                        "time": row[1] if len(row) > 1 else "",
                        "subject": row[2] if len(row) > 2 else "",
                        "notes": row[4] if len(row) > 4 else ""
                    })
        except Exception:
            logger.debug("sheets.py not available or failed")

        try:
            import drive
            if hasattr(drive, "list_forms"):
                form_list = drive.list_forms() or []
        except Exception:
            form_list = []

        try:
            import drive
            if hasattr(drive, "list_gallery_items"):
                dr_values = drive.list_gallery_items() or []
        except Exception:
            dr_values = []

        return render_template("content.html",
                               message="Content",
                               sheet_content=sheet_content,
                               sheet_title=sheet_title,
                               today_date=today_date,
                               form_list=form_list,
                               dr_values=dr_values,
                               timetable_today=timetable_today,
                               FOOTER_MESSAGE="© 2025 Newton's Nerds")
    except Exception as e:
        logger.exception("Content page error: %s", e)
        return render_template("content.html",
                               message="Content",
                               sheet_content=[],
                               sheet_title=[],
                               today_date="",
                               form_list=[],
                               dr_values=[],
                               timetable_today=[],
                               error=str(e),
                               FOOTER_MESSAGE="© 2025 Newton's Nerds"), 500

# Drive image proxy
@app.route("/drive-image/<file_id>")
def drive_image(file_id):
    try:
        import drive
        if hasattr(drive, "get_file_stream"):
            stream = drive.get_file_stream(file_id)
            if stream is None:
                raise RuntimeError("drive.get_file_stream returned None")
            if isinstance(stream, (bytes, bytearray)):
                return send_file(io.BytesIO(stream), mimetype="image/png")
            if hasattr(stream, "read"):
                return send_file(stream, mimetype="image/png")
    except Exception:
        logger.debug("drive_image: drive retrieval failed for %s", file_id)
    fallback = ROOT / "static" / "img" / "placeholder-hero.png"
    if fallback.exists():
        return send_file(fallback)
    abort(404)

# Coding page
@app.route("/coding")
def coding_page():
    try:
        exercises = load_coding_exercises()
        forms = _load_forms_data()
        return render_template("coding.html", exercises=exercises, forms=forms, FOOTER_MESSAGE="© 2025 Newton's Nerds", vapid_public_key=VAPID_PUBLIC_KEY)
    except Exception as e:
        logger.exception("coding_page failed: %s", e)
        flash("Failed to load coding page.", "danger")
        return redirect(url_for("home_page"))

@app.route("/exercise-table/<exercise_id>", methods=["GET", "POST"])
def exercise_table_route(exercise_id):
    try:
        exercises = load_coding_exercises()
        ex = next((e for e in exercises if str(e.get("id")) == str(exercise_id)), None)
        if not ex:
            return jsonify({"ok": False, "error": "exercise not found"}), 404

        schema_sql = ex.get("schema_sql") or ex.get("schema") or ""
        sample_data = ex.get("sample_data") or ex.get("sampleData") or {}

        req_sql = ""
        if request.method == "POST":
            try:
                payload = request.get_json(silent=True) or {}
                req_sql = (payload.get("sql") or "").strip()
            except Exception:
                req_sql = ""
        if not req_sql:
            req_sql = ex.get("example_query") or ex.get("exampleQuery") or ex.get("starter") or ex.get("starter_sql") or ""

        try:
            conn, tables = safe_create_and_populate_sqlite(schema_sql, sample_data)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed preparing sample DB: {e}"}), 500

        if req_sql:
            try:
                req_sql_fixed = fix_common_sql_errors(req_sql)
                conn.executescript(req_sql_fixed)
                conn.commit()
            except sqlite3.OperationalError as oe:
                logger.debug("SQL execution error: %s", oe)
                cur_html = build_tables_html_from_conn(conn)
                return jsonify({"ok": False, "html": cur_html, "error": str(oe)}), 400
            except Exception as e:
                logger.exception("SQL execution failed: %s", e)
                cur_html = build_tables_html_from_conn(conn)
                return jsonify({"ok": False, "html": cur_html, "error": str(e)}), 500

        html = build_tables_html_from_conn(conn)
        conn.close()
        return jsonify({"ok": True, "html": html})
    except Exception as e:
        logger.exception("exercise_table_route failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# Questions page & submission
@app.route("/questions", methods=["GET", "POST"])
def questions_page():
    try:
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            category = (request.form.get("category") or "").strip()
            question = (request.form.get("question") or "").strip()
            attempt = (request.form.get("attempt") or "").strip()
            if not question or not attempt or not category:
                flash("Please provide a category, your question and your attempt.", "warning")
                return redirect(url_for("questions_page"))
            timestr = datetime.utcnow().isoformat()
            author = name if name else "Anonymous"
            item = [category, author, question, attempt, timestr]
            save_question_item(item)
            try:
                res = send_push_to_subject(category, question_text=question, attempt_text=attempt)
                logger.info("Push trigger result: %s", res)
            except Exception:
                logger.exception("Failed to send push notifications on question submit")
            flash("Question submitted — thank you!", "success")
            return redirect(url_for("questions_page"))

        questions = load_questions()
        categories = sorted({q[0] for q in questions if q and len(q) > 0} | {"Mathematics", "Physics", "Chemistry", "Biology", "Computer Science", "Other"})
        community_links = {}
        vapid_public_key = VAPID_PUBLIC_KEY or ""
        selected_category = request.args.get("categoryFilter", "")
        return render_template("questions.html",
                               questions=questions,
                               categories=categories,
                               community_links=community_links,
                               vapid_public_key=vapid_public_key,
                               selected_category=selected_category,
                               FOOTER_MESSAGE="© 2025 Newton's Nerds")
    except Exception as e:
        logger.exception("questions_page failed: %s", e)
        flash("Failed to load questions page.", "danger")
        return redirect(url_for("home_page"))

# Subscription endpoints
@app.route("/subscriptions/save", methods=["POST"])
def save_subscription_route():
    try:
        payload = request.get_json(force=True)
        sub = payload.get("subscription") if payload else None
        subject = (payload.get("subject") or "").strip() if payload else ""
        client_id = payload.get("client_id") if payload else ""
        if not sub or not isinstance(sub, dict):
            return jsonify({"ok": False, "error": "invalid subscription object"}), 400
        endpoint = sub.get("endpoint")
        if not endpoint:
            return jsonify({"ok": False, "error": "subscription missing endpoint"}), 400
        key = endpoint
        subs = load_subscriptions()
        entry = subs.get(key, {"subjects": [], "subscription": sub, "created_at": datetime.utcnow().isoformat()})
        existing = [s.lower() for s in (entry.get("subjects") or [])]
        if subject and subject.lower() not in existing:
            entry_subjects = list((entry.get("subjects") or [])) + [subject]
        else:
            entry_subjects = list((entry.get("subjects") or []))
        entry["subjects"] = entry_subjects
        entry["subscription"] = sub
        entry["updated_at"] = datetime.utcnow().isoformat()
        entry["client_id"] = client_id or entry.get("client_id")
        subs[key] = entry
        save_subscriptions(subs)
        return jsonify({"ok": True, "subjects": entry["subjects"]})
    except Exception as e:
        logger.exception("save_subscription_route failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/subscriptions/check")
def subscriptions_check():
    try:
        endpoint = request.args.get("endpoint") or ""
        if not endpoint:
            return jsonify({"ok": False, "error": "missing endpoint"}), 400
        subs = load_subscriptions()
        if endpoint in subs:
            return jsonify({"ok": True, "subjects": subs[endpoint].get("subjects", [])})
        norm = normalize_endpoint(endpoint)
        for k, v in subs.items():
            if normalize_endpoint(k) == norm:
                return jsonify({"ok": True, "subjects": v.get("subjects", [])})
            stored = None
            try:
                stored = v.get("subscription", {}).get("endpoint")
            except Exception:
                stored = None
            if stored and normalize_endpoint(stored) == norm:
                return jsonify({"ok": True, "subjects": v.get("subjects", [])})
        return jsonify({"ok": True, "subjects": []})
    except Exception:
        logger.exception("subscriptions_check failed")
        return jsonify({"ok": False, "error": "server error"}), 500

@app.route("/subscriptions/delete", methods=["POST"])
def subscriptions_delete():
    try:
        payload = request.get_json(force=True)
        endpoint = (payload.get("endpoint") or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "missing endpoint"}), 400
        delete_subscriptions_by_endpoint(endpoint)
        return jsonify({"ok": True})
    except Exception:
        logger.exception("subscriptions_delete failed")
        return jsonify({"ok": False, "error": "server error"}), 500

# Admin debug endpoints
@app.route("/admin/subscriptions")
def admin_list_subscriptions():
    subs = load_subscriptions()
    return jsonify(subs)

@app.route("/admin/pending_notifications")
def admin_list_pending_notifications():
    arr = _read_json_file(NOTIFICATIONS_STORE, default=[]) or []
    return jsonify(arr)

# Health pages
@app.route("/health")
def health_index():
    try:
        items = _load_health_data()
        return render_template("health.html", plants=items, message="Health", FOOTER_MESSAGE="© 2025 Newton's Nerds")
    except Exception as e:
        logger.exception("health_index failed: %s", e)
        flash("Failed to load health advice page.", "danger")
        return redirect(url_for("home_page"))

@app.route("/health/<slug>")
def health_detail(slug):
    try:
        items = _load_health_data()
        for p in items:
            if p.get("slug") == slug:
                return render_template("health_detail.html", plant=p, message="Health", FOOTER_MESSAGE="© 2025 Newton's Nerds")
        flash("Item not found.", "warning")
        return redirect(url_for("health_index"))
    except Exception as e:
        logger.exception("health_detail failed: %s", e)
        flash("Failed to load detail.", "danger")
        return redirect(url_for("health_index"))

# Quizzes page
@app.route("/quizzes")
def quiz_forms():
    try:
        forms = _load_forms_data()
        return render_template("quiz_forms.html", forms=forms, message="Quizzes", FOOTER_MESSAGE="© 2025 Newton's Nerds")
    except Exception as e:
        logger.exception("Forms index failed, %s", e)
        flash("Failed to load Quiz Page.", "danger")
        return redirect(url_for("home_page"))

# -------------------------
# Error handler
# -------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Unhandled Exception: %s\n%s", e, traceback.format_exc())
    try:
        return render_template("error.html", message="An internal error occurred", error=str(e), FOOTER_MESSAGE="© 2025 Newton's Nerds"), 500
    except Exception:
        return f"Internal Server Error: {e}", 500

# If you want to explicitly serve static files via a route, this will work; Flask already serves /static
@app.route("/static/<path:filename>")
def static_proxy(filename):
    static_dir = ROOT / "static"
    return send_from_directory(static_dir, filename)

# -------------------------
# Run server
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
