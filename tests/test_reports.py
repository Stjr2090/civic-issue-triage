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
