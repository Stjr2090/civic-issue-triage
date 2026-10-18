"""Tests for the additive SQLite migration loader (db/migrations + app.db)."""
import sqlite3
from pathlib import Path

import pytest

import app.db as db

EXPECTED_COLUMNS = {
    "id",
    "report_id",
    "media_type",
    "file_ref",
    "created_at",
}


def _fresh_conn(tmp_path):
    conn = sqlite3.connect(Path(tmp_path) / "test.db")
    return conn


def test_fresh_db_loads_migrations(tmp_path):
    conn = _fresh_conn(tmp_path)
    conn.executescript(db.SCHEMA)
    db.apply_migrations(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "reports_attachments" in tables

    cols = {row[1] for row in conn.execute("PRAGMA table_info(reports_attachments)")}
    assert cols == EXPECTED_COLUMNS

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_reports_attachments_report" in indexes

    cur = conn.execute(
        "INSERT INTO reports (title, description, category) VALUES (?, ?, ?)",
        ("Pothole", "A deep pothole on Main Rd", "roads"),
    )
    report_id = cur.lastrowid

    conn.execute(
        "INSERT INTO reports_attachments (report_id, media_type, file_ref) "
        "VALUES (?, 'photo', ?)",
        (report_id, "uploads/abc.jpg"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reports_attachments (report_id, media_type, file_ref) "
            "VALUES (?, 'video', ?)",
            (report_id, "uploads/clip.mp4"),
        )

    conn.close()


def test_pre_migration_volume_intact(tmp_path):
    # Legacy volume: only the three original tables, no attachment table and no
    # migration loader applied.
    conn = _fresh_conn(tmp_path)
    conn.executescript(db.SCHEMA)

    conn.execute(
        "INSERT INTO reports (title, description, category, source) VALUES (?, ?, ?, ?)",
        ("Telegram report", "Reported via Telegram bot", "water", "telegram"),
    )
    conn.execute(
        "INSERT INTO reports (title, description, category, source) VALUES (?, ?, ?, ?)",
        ("Web report", "Reported from the public form", "waste", "web"),
    )
    conn.execute(
        "INSERT INTO authorities (name, category, contact) VALUES (?, ?, ?)",
        ("City Water Board", "water", "water@example.org"),
    )
    conn.execute(
        "INSERT INTO audit_log (report_id, action, detail) VALUES (1, 'routed', 'sent to authority')"
    )
    conn.commit()

    db.apply_migrations(conn)

    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM authorities").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1

    sources = {
        row[0] for row in conn.execute("SELECT source FROM reports ORDER BY id")
    }
    assert sources == {"telegram", "web"}
    assert conn.execute(
        "SELECT contact FROM authorities WHERE name='City Water Board'"
    ).fetchone()[0] == "water@example.org"
    assert conn.execute(
        "SELECT action FROM audit_log WHERE id=1"
    ).fetchone()[0] == "routed"

    # Empty-state: no backfill of existing reports.
    assert conn.execute(
        "SELECT COUNT(*) FROM reports_attachments"
    ).fetchone()[0] == 0

    conn.close()


def test_loader_is_idempotent(tmp_path):
    conn = _fresh_conn(tmp_path)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO reports (title, description, category, source) VALUES (?, ?, ?, ?)",
        ("Web report", "Reported from the public form", "waste", "web"),
    )
    conn.commit()

    db.apply_migrations(conn)
    db.apply_migrations(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "reports_attachments" in tables
    assert conn.execute(
        "SELECT COUNT(*) FROM reports_attachments"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1

    conn.close()


def test_source_check_untouched():
    assert "source IN ('web','telegram','api')" in db.SCHEMA
