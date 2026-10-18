"""Auth gate: unset creds = every protected route locked (fail-closed).

Protected set (obbo's recommendation, PM-approved 15:31Z):
- PATCH /api/reports/{id}/status  (mutating)
- GET  /api/route/{id}            (mutating via GET: status->routed + audit)
- GET  /api/reports/{id}/detail   (admin visibility affordance)

Public set (unchanged): POST /api/reports (intake), all other reads.

Login lives at POST /api/admin/login and issues an httponly session cookie.
With ADMIN_USERNAME/ADMIN_PASSWORD unset, login returns 503 and issues
nothing; protected routes return 401. This module tests the fail-closed
axis primarily in a creds-unset process; the creds-set happy path is
exercised in-process by flipping the env vars the auth module reads
dynamically.
"""
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
db.apply_migrations(conn)  # reports_attachments lives in db/migrations
conn.commit()
conn.close()

import app.routers.reports as reports_mod  # noqa: E402

reports_mod.DB_PATH = db.DB_PATH

# Auth module reads env dynamically per request (no import-time freeze), so
# tests flip os.environ directly against the shared TestClient.
import app.routers.auth as auth_mod  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _seed_report(category: str = "roads") -> int:
    r = client.post("/api/reports", json={
        "title": "Gate probe pothole",
        "description": "Seeded by the auth gate suite for route probing.",
        "category": category,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture(autouse=True)
def _clean_auth_env():
    """Each test starts fail-closed: creds unset, no sessions."""
    saved = {
        k: os.environ.pop(k, None) for k in ("ADMIN_USERNAME", "ADMIN_PASSWORD")
    }
    auth_mod._sessions.clear()
    client.cookies.clear()
    yield
    auth_mod._sessions.clear()
    client.cookies.clear()
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


# --- fail-closed: unset creds ------------------------------------------------


def test_gate_unset_creds_all_protected_routes_401():
    rid = _seed_report()
    for method, url in (
        ("patch", f"/api/reports/{rid}/status?status=routed"),
        ("get", f"/api/route/{rid}"),
        ("get", f"/api/reports/{rid}/detail"),
    ):
        r = getattr(client, method)(url)
        assert r.status_code == 401, (method, url, r.status_code, r.text)


def test_gate_unset_creds_login_503_issues_nothing():
    r = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "whatever"},
    )
    assert r.status_code == 503
    assert "reporta_session" not in r.cookies


def test_gate_intake_and_reads_stay_public():
    rid = _seed_report()
    assert client.get("/api/reports").status_code == 200
    assert client.get(f"/api/reports/{rid}").status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/authorities").status_code == 200


# --- creds set: happy path + session semantics -------------------------------


def test_login_success_sets_cookie_and_unlocksRoutes():
    rid = _seed_report()
    # Route happy path needs an active authority for the report's category.
    c = db.sqlite3.connect(db.DB_PATH)
    c.execute(
        "INSERT INTO authorities (name, category, priority, active)"
        " VALUES ('KCCA Roads', 'roads', 5, 1)"
    )
    c.commit()
    c.close()
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    r = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "s3cret-demo-pw"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert client.cookies.get("reporta_session")

    # Protected routes now serve the admin.
    r2 = client.patch(f"/api/reports/{rid}/status?status=routed")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "routed"
    assert client.get(f"/api/reports/{rid}/detail").status_code == 200

    r3 = client.get(f"/api/route/{rid}")
    assert r3.status_code == 200, r3.text
    assert r3.json()["routed_to"]


def test_login_wrong_password_401_no_session():
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    r = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    assert not client.cookies.get("reporta_session")


def test_logout_kills_session_server_side():
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "s3cret-demo-pw"},
    )
    assert client.get("/api/stats").status_code == 200  # sanity
    rid = _seed_report()
    assert client.patch(f"/api/reports/{rid}/status?status=new").status_code == 200
    client.post("/api/admin/logout")
    client.cookies.pop("reporta_session", None)
    r = client.patch(f"/api/reports/{rid}/status?status=closed")
    assert r.status_code == 401


def test_expired_session_rejected():
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "s3cret-demo-pw"},
    )
    token = client.cookies.get("reporta_session")
    assert token
    auth_mod._sessions[token] = 0.0  # force-expire
    rid = _seed_report()
    r = client.patch(f"/api/reports/{rid}/status?status=closed")
    assert r.status_code == 401
    # Lazily evicted.
    assert token not in auth_mod._sessions


def test_garbage_cookie_401():
    client.cookies.set("reporta_session", "forged-token")
    rid = _seed_report()
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    r = client.patch(f"/api/reports/{rid}/status?status=closed")
    assert r.status_code == 401
