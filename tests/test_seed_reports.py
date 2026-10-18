"""Tests for the Lightsail sample-report seeder (deploy/lightsail-reporta).

Hermetic: mirrors tests/test_reports.py's temp-DB pattern (module-level
``db.DB_PATH`` override + ``TestClient``). No real network and no live server
are used -- the seeder is handed the ``TestClient`` so every HTTP call is
dispatched straight into the ASGI app. ``GROQ_API_KEY`` is unset, so the server
classifies in keyless keyword mode.
"""
import importlib.util
import os
import pathlib
import sqlite3
import tempfile

import pytest

tmp = tempfile.mkdtemp()
os.environ["TMPDIR"] = tmp

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db  # noqa: E402

db.DB_PATH = db.Path(tmp) / "test_seed_reports.db"
conn = sqlite3.connect(db.DB_PATH)
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app.routers.reports as reports_mod  # noqa: E402

reports_mod.DB_PATH = db.DB_PATH

from app.main import app  # noqa: E402

client = TestClient(app)

# Production startup also applies db/migrations; the detail endpoint needs the
# attachments table that Phase 1 added.
_mig_conn = sqlite3.connect(db.DB_PATH)
db.apply_migrations(_mig_conn)
_mig_conn.close()

# The seeder lives outside the package (deploy/), so load it by file path.
SEEDER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "deploy"
    / "lightsail-reporta"
    / "seed_reports.py"
)
_spec = importlib.util.spec_from_file_location("seed_reports", SEEDER_PATH)
seed_reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_reports)

# TestClient's default base URL; absolute URLs resolve through the ASGI app.
BASE_URL = "http://testserver"

# One active authority per authority-backed category. The seeder itself never
# seeds authorities, so the routing table is set up here (as the deployment's
# scripts/seed.py would).
AUTHORITIES = [
    ("Sample Roads Authority", "roads", "roads@example.gov", 1),
    ("Sample Water Authority", "water", "water@example.gov", 1),
    ("Sample Waste Authority", "waste", "waste@example.gov", 1),
    ("Sample Public Services Authority", "public_services", "clerk@example.gov", 1),
]


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Keyless mode: the server must classify via keyword seeds."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _schema():
    """Guarantee the active reports DB has the schema, whatever module won."""
    c = sqlite3.connect(reports_mod.DB_PATH)
    c.executescript(db.SCHEMA)
    c.commit()
    c.close()


@pytest.fixture(scope="module")
def seeded():
    """Run the seeder once against the TestClient and return its summary."""
    os.environ.pop("GROQ_API_KEY", None)
    c = sqlite3.connect(reports_mod.DB_PATH)
    try:
        c.executemany(
            "INSERT OR IGNORE INTO authorities (name, category, contact, priority)"
            " VALUES (?,?,?,?)",
            AUTHORITIES,
        )
        c.commit()
    finally:
        c.close()
    return seed_reports.seed(BASE_URL, client=client)


def _spec_id_pairs(summary):
    """Design entries aligned with the ids the seeder reported as created."""
    return list(zip(seed_reports.REPORTS, summary["created_ids"]))


def _routed_audit_ids():
    c = sqlite3.connect(reports_mod.DB_PATH)
    try:
        rows = c.execute(
            "SELECT report_id FROM audit_log WHERE action='routed'"
        ).fetchall()
    finally:
        c.close()
    return {row[0] for row in rows}


# 1. Fresh run creates exactly seven.
def test_fresh_run_creates_exactly_seven(seeded):
    assert seeded["created"] == 7
    assert seeded["skipped"] == 0
    assert len(seeded["created_ids"]) == 7


# 2. Created ids + statuses match the design.
def test_created_statuses_match_design(seeded):
    by_status = {}
    routed_ids = []
    for spec, rid in _spec_id_pairs(seeded):
        body = client.get(f"/api/reports/{rid}").json()
        by_status.setdefault(body["status"], []).append(rid)
        if spec.get("route"):
            routed_ids.append(rid)

    assert len(by_status.get("new", [])) == 4
    assert len(by_status.get("in_progress", [])) == 1
    assert len(by_status.get("resolved", [])) == 1
    assert len(by_status.get("routed", [])) == 1
    # The single routed report is the design's route entry.
    assert by_status["routed"] == routed_ids


# 3. audit_log holds real "routed" rows for exactly the routed seeded reports.
def test_audit_log_routed_rows_match_routed_reports(seeded):
    routed_in_log = _routed_audit_ids()
    design_routed = 0
    for spec, rid in _spec_id_pairs(seeded):
        expected = bool(spec.get("route"))
        design_routed += int(expected)
        assert (rid in routed_in_log) is expected
    assert design_routed == 1


# 4. Every authority-backed seeded report has populated next_options.
def test_next_options_populated_for_authority_backed(seeded):
    checked = 0
    for spec, rid in _spec_id_pairs(seeded):
        if spec["category"] == "other":
            continue
        detail = client.get(f"/api/reports/{rid}/detail")
        assert detail.status_code == 200, detail.text
        assert detail.json()["next_options"], spec["title"]
        checked += 1
    assert checked == 6


# 5. The "other" report is in review and has zero routed audit rows.
def test_other_report_needs_review_and_unrouted(seeded):
    others = [
        (spec, rid)
        for spec, rid in _spec_id_pairs(seeded)
        if spec["category"] == "other"
    ]
    assert len(others) == 1
    _, rid = others[0]

    body = client.get(f"/api/reports/{rid}").json()
    assert body["needs_review"] == 1

    c = sqlite3.connect(reports_mod.DB_PATH)
    try:
        routed = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE report_id=? AND action='routed'",
            (rid,),
        ).fetchone()[0]
    finally:
        c.close()
    assert routed == 0


# 6. A rerun is a no-op: created=0, skipped=7.
def test_rerun_is_a_noop(seeded):
    again = seed_reports.seed(BASE_URL, client=client)
    assert again["created"] == 0
    assert again["skipped"] == 7
    assert again["routed"] == 0
    assert again["created_ids"] == []


# 7. No fabricated analysis: keyless keyword mode, server-provided fields only.
def test_no_fabricated_analysis_keyword_mode(seeded):
    for spec, rid in _spec_id_pairs(seeded):
        detail = client.get(f"/api/reports/{rid}/detail").json()
        analysis = detail["analysis"]
        report = detail["report"]

        assert analysis["state"] == "classified"
        assert "keyword" in analysis["reason"]
        assert "mode=ai" not in analysis["reason"]
        # Detail analysis is the server response, not client-side invention.
        assert analysis["reason"] == report["reason"]
        assert analysis["confidence"] == report["confidence"]

        classified = [
            ev for ev in detail["routing_history"] if ev["action"] == "classified"
        ]
        assert classified
        assert classified[-1]["detail"]["mode"] == "keyword"


# Design sanity: the seven reports cover the required categories and routes.
def test_design_covers_required_categories():
    categories = [spec["category"] for spec in seed_reports.REPORTS]
    assert len(categories) == 7
    assert categories.count("roads") == 2
    assert categories.count("water") == 2
    assert categories.count("waste") == 1
    assert categories.count("public_services") == 1
    assert categories.count("other") == 1
    assert all(spec["title"].startswith(seed_reports.SAMPLE_PREFIX) for spec in seed_reports.REPORTS)
