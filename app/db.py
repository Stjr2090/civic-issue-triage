"""Database schema for civic-issue-triage.

Three tables (obbo's design, unchanged):

- reports: one row per citizen-submitted civic issue.
- authorities: local authority routing table (data, not code).
- audit_log: append-only trail of routing/assignment decisions.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "civic.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL,
    category    TEXT    NOT NULL
                CHECK (category IN ('roads','water','waste','public_services')),
    urgency     INTEGER NOT NULL DEFAULT 3
                CHECK (urgency BETWEEN 1 AND 5),
    confidence  REAL,
    reason      TEXT,
    reporter    TEXT,
    location    TEXT,
    status      TEXT    NOT NULL DEFAULT 'new'
                CHECK (status IN ('new','routed','in_progress','resolved','closed')),
    dup_of      INTEGER REFERENCES reports(id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authorities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL
                CHECK (category IN ('roads','water','waste','public_services')),
    contact     TEXT,
    priority    INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL REFERENCES reports(id),
    action     TEXT    NOT NULL,
    detail     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reports_status   ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
CREATE INDEX IF NOT EXISTS idx_reports_created  ON reports(created_at);
CREATE INDEX IF NOT EXISTS idx_authorities_cat  ON authorities(category, active);
"""

conn = sqlite3.connect(DB_PATH)
conn.executescript(SCHEMA)
conn.commit()
conn.close()
