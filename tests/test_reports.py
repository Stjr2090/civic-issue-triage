"""T1 tests: schema + CRUD smoke."""
import os
import tempfile

import pytest

tmp = tempfile.mkdtemp()
os.environ["TMPDIR"] = tmp

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db  # noqa: E402

db.DB_PATH = db.Path(tmp) / "test.db"
conn = db.sqlite3.connect(db.DB_PATH)
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app.routers.reports as reports_mod  # noqa: E402

reports_mod.DB_PATH = db.DB_PATH

from app.main import app  # noqa: E402

client = TestClient(app)

# Auth era: PATCH status, GET route/{id} and GET detail require an admin
# session (fail-closed protection is covered in test_auth_gate.py). Legacy
# tests below assumed an open admin surface. The auth suite clears the
# shared in-memory session store between its tests, so the legacy client
# re-authenticates before each test instead of holding one token.
os.environ["ADMIN_USERNAME"] = "legacy-suite"
os.environ["ADMIN_PASSWORD"] = "legacy-only"


@pytest.fixture(autouse=True)
def _admin_session():
    r = client.post(
        "/api/admin/login",
        json={"username": "legacy-suite", "password": "legacy-only"},
    )
    assert r.status_code == 200, r.text


def test_create_and_get_report():
    r = client.post("/api/reports", json={
        "title": "Pothole on Main Rd",
        "description": "Deep pothole near the market, two wheels already lost.",
        "category": "roads",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "new"
    assert body["urgency"] == 3
    rid = body["id"]

    r2 = client.get(f"/api/reports/{rid}")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Pothole on Main Rd"


def test_category_validation():
    r = client.post("/api/reports", json={
        "title": "Bad category",
        "description": "This category does not exist at all.",
        "category": "aliens",
    })
    assert r.status_code == 422


def test_other_category_lands_in_review_queue():
    r = client.post("/api/reports", json={
        "title": "Unclassifiable complaint",
        "description": "Something odd that does not fit any known category.",
        "category": "other",
    })
    assert r.status_code == 201, r.text
    assert r.json()["needs_review"] == 1


def test_unknown_category_still_rejected():
    r = client.post("/api/reports", json={
        "title": "Alien sighting",
        "description": "This category does not exist anywhere at all.",
        "category": "aliens",
    })
    assert r.status_code == 422


def test_list_filter_by_category():
    client.post("/api/reports", json={
        "title": "No water for days",
        "description": "The whole street has had no piped water since Monday.",
        "category": "water",
    })
    r = client.get("/api/reports", params={"category": "water"})
    assert r.status_code == 200
    assert all(row["category"] == "water" for row in r.json())


def test_status_update_and_audit():
    r = client.post("/api/reports", json={
        "title": "Overflowing bin",
        "description": "Community bin has not been collected in two weeks.",
        "category": "waste",
    })
    rid = r.json()["id"]
    r2 = client.patch(f"/api/reports/{rid}/status", params={"status": "routed"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "routed"
    assert client.patch(
        f"/api/reports/{rid}/status", params={"status": "bogus"}
    ).status_code == 422


def test_routing():
    # Seed an authority for public_services.
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "INSERT INTO authorities (name, category, contact) VALUES (?,?,?)",
        ("Town Clerk Office", "public_services", "clerk@example.gov"),
    )
    conn.commit()
    conn.close()

    r = client.post("/api/reports", json={
        "title": "Broken streetlight",
        "description": "Streetlight out for a month, junction is dark at night.",
        "category": "public_services",
    })
    rid = r.json()["id"]
    r2 = client.get(f"/api/route/{rid}")
    assert r2.status_code == 200
    assert r2.json()["routed_to"]["name"] == "Town Clerk Office"


def test_list_authorities_and_category_filter():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.executemany(
        "INSERT OR IGNORE INTO authorities (name, category, contact, priority)"
        " VALUES (?,?,?,?)",
        [
            ("Roads Authority", "roads", "roads@example.gov", 1),
            ("Roads Fallback", "roads", "roads-fallback@example.gov", 0),
            ("Water Authority", "water", "water@example.gov", 1),
        ],
    )
    conn.commit()
    conn.close()

    r = client.get("/api/authorities")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["category"] == "roads" for row in rows)
    assert any(row["category"] == "water" for row in rows)

    r2 = client.get("/api/authorities", params={"category": "roads"})
    assert r2.status_code == 200
    roads = r2.json()
    assert roads and all(row["category"] == "roads" for row in roads)
    priorities = [row["priority"] for row in roads]
    assert priorities == sorted(priorities, reverse=True)


def test_route_prefers_higher_priority():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.executemany(
        "INSERT OR IGNORE INTO authorities (name, category, contact, priority)"
        " VALUES (?,?,?,?)",
        [
            ("Primary Waste Authority", "waste", "waste@example.gov", 1),
            ("Fallback Waste Authority", "waste", "waste-fallback@example.gov", 0),
        ],
    )
    conn.commit()
    conn.close()

    r = client.post("/api/reports", json={
        "title": "Missed waste collection",
        "description": "Bins on this street have not been collected for two weeks.",
        "category": "waste",
    })
    rid = r.json()["id"]
    r2 = client.get(f"/api/route/{rid}")
    assert r2.status_code == 200
    assert r2.json()["routed_to"]["name"] == "Primary Waste Authority"
    assert r2.json()["routed_to"]["contact"] == "waste@example.gov"


def test_dashboard_stats_shape():
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "by_status" in body and "by_category" in body


def test_telegram_source_roundtrip_and_queue_filter():
    r = client.post("/api/reports", json={
        "title": "Water pipe burst via telegram",
        "description": "Report submitted through the telegram intake bot path.",
        "category": "water",
        "source": "telegram",
    })
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "telegram"
    q = client.get("/api/reports", params={"source": "telegram"})
    assert q.status_code == 200
    rows = q.json()
    assert rows and all(row["source"] == "telegram" for row in rows)
    assert client.post("/api/reports", json={
        "title": "Bad source value",
        "description": "Source value that is not allowed at all.",
        "category": "water",
        "source": "smoke_signal",
    }).status_code == 422


def test_authorities_active_bool_filter():
    """T6 regression: ?active=false must parse as bool, not 422 int_parsing."""
    r = client.get("/api/authorities", params={"active": "false"})
    assert r.status_code == 200
    rows = r.json()
    assert all(row["active"] == 0 for row in rows)
    r2 = client.get("/api/authorities", params={"active": "true"})
    assert r2.status_code == 200
    assert all(row["active"] == 1 for row in r2.json())


def test_reports_needs_review_bool_filter():
    r = client.get("/api/reports", params={"needs_review": "true"})
    assert r.status_code == 200
    assert all(row["needs_review"] == 1 for row in r.json())
    r2 = client.get("/api/reports", params={"needs_review": "false"})
    assert r2.status_code == 200
    assert all(row["needs_review"] == 0 for row in r2.json())


# ---------------------------------------------------------------------------
# Phase 2: admin case-detail probes (GET /api/reports/{report_id}/detail)
# ---------------------------------------------------------------------------

# The module fixture above builds the test DB from db.SCHEMA only. Production
# startup also applies db/migrations, so apply them here too (Phase 1's
# reports_attachments is merged and required by the detail endpoint).
_mig_conn = db.sqlite3.connect(db.DB_PATH)
db.apply_migrations(_mig_conn)
_mig_conn.close()

DASHBOARD_HTML = (
    db.Path(__file__).resolve().parent.parent / "static" / "dashboard.html"
).read_text(encoding="utf-8")


def _detail(report_id):
    return client.get(f"/api/reports/{report_id}/detail")


def _insert_raw_report(title, description, category, confidence, reason):
    conn = db.sqlite3.connect(db.DB_PATH)
    cur = conn.execute(
        "INSERT INTO reports (title, description, category, confidence, reason)"
        " VALUES (?,?,?,?,?)",
        (title, description, category, confidence, reason),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def test_detail_classified_row():
    r = client.post("/api/reports", json={
        "title": "Classified detail probe",
        "description": "A report the classifier handles end to end for detail.",
        "category": "roads",
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    d = _detail(rid)
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["analysis"]["state"] == "classified"
    history = body["routing_history"]
    assert any(ev["action"] == "classified" for ev in history)
    # `classified` detail renders parsed JSON, not the raw blob.
    classified = [ev for ev in history if ev["action"] == "classified"][0]
    assert isinstance(classified["detail"], dict)
    assert set(body["report"]) == set(client.get(f"/api/reports/{rid}").json())


def test_detail_pre_t4_row_renders_state_not_error():
    rid = _insert_raw_report(
        "Legacy pre-T4 row",
        "Written before the classification module existed.",
        "water", None, None,
    )
    d = _detail(rid)
    assert 200 <= d.status_code < 300, d.text
    assert d.status_code == 200
    assert d.json()["analysis"]["state"] == "not classified (pre-T4 report)"
    # No sibling read path turns this legacy row into a 4xx/5xx.
    assert client.get(f"/api/reports/{rid}").status_code == 200
    assert "not classified (pre-T4 report)" in DASHBOARD_HTML


def test_detail_no_attachment_state():
    r = client.post("/api/reports", json={
        "title": "No attachment probe",
        "description": "This report intentionally carries no media at all.",
        "category": "roads",
    })
    rid = r.json()["id"]
    d = _detail(rid)
    assert d.status_code == 200, d.text
    assert d.json()["attachments"] == []
    # Frontend maps the empty list to the defined state string.
    assert "no attachment" in DASHBOARD_HTML


def test_detail_with_attachment_row():
    r = client.post("/api/reports", json={
        "title": "Attachment probe",
        "description": "This report should carry exactly one photo file.",
        "category": "waste",
    })
    rid = r.json()["id"]
    conn = db.sqlite3.connect(db.DB_PATH)
    conn.execute(
        "INSERT INTO reports_attachments (report_id, media_type, file_ref)"
        " VALUES (?,?,?)",
        (rid, "photo", "uploads/probe.jpg"),
    )
    conn.commit()
    conn.close()

    d = _detail(rid)
    assert d.status_code == 200, d.text
    atts = d.json()["attachments"]
    assert len(atts) == 1
    assert atts[0]["media_type"] == "photo"
    assert atts[0]["file_ref"] == "uploads/probe.jpg"
    assert atts[0]["created_at"]


def test_detail_empty_routing_history():
    rid = _insert_raw_report(
        "No history yet",
        "Reported but never classified or routed by any path.",
        "roads", 0.9, "manual",
    )
    d = _detail(rid)
    assert d.status_code == 200, d.text
    assert d.json()["routing_history"] == []
    assert "no routing history yet" in DASHBOARD_HTML


def test_detail_other_category_has_no_options():
    r = client.post("/api/reports", json={
        "title": "Other category probe",
        "description": "Falls into the catch-all bucket with no authorities.",
        "category": "other",
    })
    rid = r.json()["id"]
    d = _detail(rid)
    assert d.status_code == 200, d.text
    assert d.json()["next_options"] == []
    # CATS map collateral fix: "other" renders as a label, not the raw string.
    assert 'other: "Other"' in DASHBOARD_HTML
    assert "no active authority for category" in DASHBOARD_HTML


def test_detail_unknown_id_is_404():
    assert _detail(999999999).status_code == 404


def test_detail_history_tracks_status_change():
    r = client.post("/api/reports", json={
        "title": "Status change probe",
        "description": "Detail history should track a status transition.",
        "category": "roads",
    })
    rid = r.json()["id"]
    patched = client.patch(
        f"/api/reports/{rid}/status", params={"status": "in_progress"}
    )
    assert patched.status_code == 200, patched.text

    d = _detail(rid)
    assert d.status_code == 200, d.text
    changes = [
        ev for ev in d.json()["routing_history"] if ev["action"] == "status_change"
    ]
    assert changes
    assert changes[-1]["detail"] == "in_progress"
