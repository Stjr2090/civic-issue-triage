"""Database schema for civic-issue-triage.

Three tables (obbo's design, unchanged):

- reports: one row per citizen-submitted civic issue.
- authorities: local authority routing table (data, not code).
- audit_log: append-only trail of routing/assignment decisions.
"""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "civic.db"
# Deployment-overridable so the Lightsail volume persists across redeploys.
DB_PATH = Path(os.environ.get("DATABASE_PATH") or DEFAULT_DB_PATH)
# Fresh clones have no data/ dir; create it before the import-time connect.
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL,
    category    TEXT    NOT NULL
                CHECK (category IN ('roads','water','waste','public_services','other')),
    urgency     INTEGER NOT NULL DEFAULT 3
                CHECK (urgency BETWEEN 1 AND 5),
    confidence  REAL,
    reason      TEXT,
    reporter    TEXT,
    location    TEXT,
    source      TEXT    NOT NULL DEFAULT 'web'
                CHECK (source IN ('web','telegram','api')),
    needs_review INTEGER NOT NULL DEFAULT 0,
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_authorities_name_cat
    ON authorities(name, category);
"""

def apply_migrations(conn):
    """Apply every *.sql file in db/migrations/ in lexical order.

    Additive and idempotent by convention (IF NOT EXISTS); no ledger/version
    tracking. Safe to run on every startup and on a live volume.
    """
    migrations_dir = Path(__file__).resolve().parent.parent / "db" / "migrations"
    if migrations_dir.exists():
        for migration in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(migration.read_text())
        conn.commit()


conn = sqlite3.connect(DB_PATH)
conn.executescript(SCHEMA)
apply_migrations(conn)
conn.commit()
conn.close()
