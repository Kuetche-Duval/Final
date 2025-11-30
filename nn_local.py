#!/usr/bin/env python3
"""
create_db.py
Creates nn_local.db (or provided path) and the required tables for subscriptions + notifications.
Usage:
  python create_db.py                    # creates ./nn_local.db
  python create_db.py /path/to/my.db     # creates /path/to/my.db
"""
import sys
import sqlite3
from pathlib import Path

DEFAULT = "nn_local.db"
db_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)

print("Creating DB at:", db_path.resolve())
if not db_path.parent.exists():
    print("Creating parent dirs:", db_path.parent)
    db_path.parent.mkdir(parents=True, exist_ok=True)

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
);
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
);
""")

try:
    c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_endpoint_delivered ON notifications(endpoint, delivered);")
except Exception:
    pass

conn.commit()
conn.close()
print("DB created and tables ensured.")
