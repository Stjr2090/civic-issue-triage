-- 0003-reports-source-whatsapp: extend the reports.source CHECK to allow
-- 'whatsapp' (T-WA lane ruling: WhatsApp events enter the existing intake,
-- origin traceable via source=whatsapp).
--
-- SQLite cannot ALTER a CHECK constraint, so this rebuilds the reports table
-- with the CHECK extended to web|telegram|api|whatsapp. Same columns, same
-- defaults, same indexes, rows copied verbatim. Idempotent by convention:
-- safe to re-run on a live volume (re-runs copy rows into a fresh table and
-- rename back). No `docker compose down -v` needed.
--
-- foreign_keys is toggled OFF around the swap because reports_attachments /
-- reports_transcripts reference reports(id) and SQLite re-checks FKs on
-- parent-table drops.

PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS reports_new (
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
                CHECK (source IN ('web','telegram','api','whatsapp')),
    needs_review INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'new'
                CHECK (status IN ('new','routed','in_progress','resolved','closed')),
    dup_of      INTEGER REFERENCES reports(id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO reports_new
    (id, title, description, category, urgency, confidence, reason,
     reporter, location, source, needs_review, status, dup_of,
     created_at, updated_at)
SELECT
    id, title, description, category, urgency, confidence, reason,
    reporter, location, source, needs_review, status, dup_of,
    created_at, updated_at
FROM reports;

DROP TABLE reports;
ALTER TABLE reports_new RENAME TO reports;

CREATE INDEX IF NOT EXISTS idx_reports_status   ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
CREATE INDEX IF NOT EXISTS idx_reports_created  ON reports(created_at);

PRAGMA foreign_keys=ON;
