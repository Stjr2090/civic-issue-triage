CREATE TABLE IF NOT EXISTS reports_transcripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES reports_attachments(id),
    text          TEXT    NOT NULL,
    quality_json  TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reports_transcripts_attachment
    ON reports_transcripts(attachment_id);
