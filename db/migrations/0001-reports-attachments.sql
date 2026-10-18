-- 0001-reports-attachments: add per-report media attachments.
--
-- `file_ref` is OPAQUE storage: it holds either a web upload path or a Telegram
-- `file_id`. Readers must NOT parse or reinterpret it; resolve it per source
-- channel (web vs telegram).
--
-- This migration is additive and idempotent (CREATE TABLE/INDEX IF NOT EXISTS),
-- so it is safe to apply on a live volume. No `docker compose down -v` needed.

CREATE TABLE IF NOT EXISTS reports_attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL REFERENCES reports(id),
    media_type TEXT    NOT NULL CHECK (media_type IN ('photo','voice')),
    file_ref   TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reports_attachments_report
    ON reports_attachments(report_id);
