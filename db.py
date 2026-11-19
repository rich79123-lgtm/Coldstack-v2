"""SQLite storage. One connection per call keeps it thread-safe for the background workers."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH, SETTING_DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    email TEXT NOT NULL, first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
    title TEXT DEFAULT '', company TEXT DEFAULT '', company_domain TEXT DEFAULT '',
    industry TEXT DEFAULT '', company_size TEXT DEFAULT '', location TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '', custom TEXT DEFAULT '{}', source TEXT DEFAULT '',
    provider_status TEXT DEFAULT '',
    verify_status TEXT DEFAULT 'pending',   -- pending | valid | risky | invalid
    verify_detail TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(list_id, email)
);
CREATE INDEX IF NOT EXISTS idx_leads_verify ON leads(verify_status);

CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY, reason TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inboxes (
    id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, from_name TEXT DEFAULT '',
    smtp_host TEXT, smtp_port INTEGER, smtp_security TEXT DEFAULT 'ssl', smtp_user TEXT, smtp_pass TEXT,
    imap_host TEXT, imap_port INTEGER DEFAULT 993, imap_user TEXT, imap_pass TEXT,
    imap_last_uid INTEGER DEFAULT -1,
    daily_limit INTEGER DEFAULT 30, ramp_days INTEGER DEFAULT 14, ramp_start_volume INTEGER DEFAULT 5,
    ramp_started TEXT, min_gap_seconds INTEGER DEFAULT 240,
    warmup_enabled INTEGER DEFAULT 1, warmup_daily INTEGER DEFAULT 10,
    signature TEXT DEFAULT '', status TEXT DEFAULT 'active', last_error TEXT DEFAULT '',
    last_sent_at TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, list_id INTEGER REFERENCES lists(id),
    status TEXT DEFAULT 'draft',             -- draft | active | paused | completed
    timezone TEXT DEFAULT 'Europe/London', window_start TEXT DEFAULT '08:30', window_end TEXT DEFAULT '17:30',
    send_days TEXT DEFAULT 'mon,tue,wed,thu,fri', daily_cap INTEGER DEFAULT 200,
    stop_on_reply INTEGER DEFAULT 1, include_risky INTEGER DEFAULT 0,
    unsubscribe_footer INTEGER DEFAULT 1, plain_text INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_inboxes (
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    inbox_id INTEGER REFERENCES inboxes(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, inbox_id)
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, delay_days INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY, step_id INTEGER REFERENCES steps(id) ON DELETE CASCADE,
    label TEXT DEFAULT 'A', subject TEXT DEFAULT '', body TEXT DEFAULT '', active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE, inbox_id INTEGER REFERENCES inboxes(id),
    next_step INTEGER DEFAULT 0, next_send_at TEXT,
    status TEXT DEFAULT 'active',            -- active | replied | bounced | unsubscribed | completed | failed
    thread_subject TEXT DEFAULT '', last_message_id TEXT DEFAULT '', references_ids TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, lead_id)
);
CREATE INDEX IF NOT EXISTS idx_enroll_due ON enrollments(status, next_send_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL,      -- out | in | warmup_out | warmup_in | bounce
    enrollment_id INTEGER, campaign_id INTEGER, lead_id INTEGER, inbox_id INTEGER,
    step_position INTEGER, variant_id INTEGER,
    message_id TEXT, in_reply_to TEXT, from_addr TEXT, to_addr TEXT,
    subject TEXT, body TEXT, classification TEXT DEFAULT '', is_read INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_mid ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_msg_kind ON messages(kind, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, level TEXT, source TEXT, message TEXT, created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with tx() as c:
        c.executescript(SCHEMA)


def q(sql, params=()):
    with tx() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql, params=()):
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def log_event(source, message, level="info"):
    try:
        ex("INSERT INTO events(level, source, message, created_at) VALUES (?,?,?,?)",
           (level, source, str(message)[:2000], now_iso()))
    except Exception:
        pass


# ---- settings -------------------------------------------------------------

def get_setting(key: str) -> str:
    env = os.environ.get(key.upper())
    if env:
        return env
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if row and row["value"] is not None:
        return row["value"]
    return SETTING_DEFAULTS.get(key, "")


def set_setting(key: str, value: str):
    ex("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))


def all_settings() -> dict:
    return {k: get_setting(k) for k in SETTING_DEFAULTS}


def loads(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}
