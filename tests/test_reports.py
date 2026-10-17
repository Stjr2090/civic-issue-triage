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
