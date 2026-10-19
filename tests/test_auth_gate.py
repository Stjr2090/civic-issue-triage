"""Auth gate: stateless signed-JWT (HS256) session; fail-closed.

Protected set (obbo's recommendation, PM-approved 15:31Z):
- PATCH /api/reports/{id}/status  (mutating)
- GET  /api/route/{id}            (mutating via GET: status->routed + audit)
- GET  /api/reports/{id}/detail   (admin visibility affordance)
- GET  /api/reports              (case rows; Steph's privacy order)
- GET  /api/reports/{id}         (single case row; auth precedes existence)
- GET  /api/stats                (aggregate counts; Steph's privacy order)

Public set (unchanged): POST /api/reports (intake), GET /api/authorities
(routing metadata, zero case data), GET / (index), GET /dashboard (bare shell).

Login lives at POST /api/admin/login and issues a compact HS256 JWS in an
httponly session cookie. With JWT_SECRET, ADMIN_USERNAME or ADMIN_PASSWORD
unset, login returns 401 and issues nothing; protected routes return 401.
Verification is fully stateless, so an unexpired token survives a fresh
process (asserted here with a second TestClient).

The signing key is read at call time; tests flip os.environ directly against
the shared app. A module-level default key keeps sibling suites
(test_reports) able to authenticate under the stateless scheme.
"""
import base64
import json
import os
import tempfile
import types

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

# Sibling suite test_reports logs in with only ADMIN_USERNAME/ADMIN_PASSWORD
# set; a module-level signing key (saved/restored per test below) lets it
# authenticate under the stateless scheme. Collection imports every module
# before any test runs, so this key is present for the whole session.
TEST_JWT_SECRET = "gate-test-secret"
os.environ["JWT_SECRET"] = TEST_JWT_SECRET

client = TestClient(app)


def _seed_report(category: str = "roads") -> int:
    r = client.post("/api/reports", json={
        "title": "Gate probe pothole",
        "description": "Seeded by the auth gate suite for route probing.",
        "category": category,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _set_admin(secret: str = TEST_JWT_SECRET) -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    os.environ["JWT_SECRET"] = secret


def _login(c=client):
    return c.post(
        "/api/admin/login",
        json={"username": "admin", "password": "s3cret-demo-pw"},
    )


def _segment(encoded: str) -> dict:
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


@pytest.fixture(autouse=True)
def _clean_auth_env():
    """Each test starts fail-closed: creds/key unset, no cookie."""
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "ADMIN_USERNAME",
            "ADMIN_PASSWORD",
            "JWT_SECRET",
            "JWT_TTL_SECONDS",
            "COOKIE_SECURE",
        )
    }
    client.cookies.clear()
    yield
    client.cookies.clear()
    # Pop-then-restore: a test body may have set vars that were absent at
    # setup (e.g. COOKIE_SECURE=1); leaving them would leak Secure cookies
    # into later suites over http (unsent cookie -> 401). We saw exactly that.
    for k in saved:
        os.environ.pop(k, None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


# --- fail-closed: config unset -----------------------------------------------


def test_gate_unset_creds_all_protected_routes_401():
    rid = _seed_report()
    for method, url in (
        ("patch", f"/api/reports/{rid}/status?status=routed"),
        ("get", f"/api/route/{rid}"),
        ("get", f"/api/reports/{rid}/detail"),
    ):
        r = getattr(client, method)(url)
        assert r.status_code == 401, (method, url, r.status_code, r.text)


def test_gate_unset_creds_login_401_issues_nothing():
    r = _login()
    assert r.status_code == 401
    assert "reporta_session" not in r.cookies


def test_gate_jwt_secret_unset_login_and_protected_401_intake_open():
    """Creds present but no signing key: still fail closed, intake public."""
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "s3cret-demo-pw"
    os.environ.pop("JWT_SECRET", None)
    r = _login()
    assert r.status_code == 401
    assert "reporta_session" not in r.cookies
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}/detail").status_code == 401
    # Case-data reads are gated too; only intake + routing metadata stay open.
    assert client.get("/api/reports").status_code == 401
    assert client.get(f"/api/reports/{rid}").status_code == 401
    assert client.get("/api/stats").status_code == 401


def test_gate_intake_and_reads_stay_public():
    # Reads moved behind the gate; the public surface is intake + routing
    # metadata + the two static shells.
    _seed_report()
    assert client.get("/api/authorities").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/dashboard").status_code == 200


# --- case-data reads gated (guards commit, item 1 of 2) -----------------------


def test_gate_signed_out_reports_list_401_no_rows():
    _seed_report()
    r = client.get("/api/reports")
    assert r.status_code == 401
    # No case rows may leak through the fail-closed 401 body.
    assert "title" not in r.text
    assert "Gate probe pothole" not in r.text


def test_gate_signed_out_stats_401():
    assert client.get("/api/stats").status_code == 401


def test_gate_signed_out_single_report_401():
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}").status_code == 401


def test_gate_signed_out_unknown_report_401_not_404():
    # Auth precedes existence: a nonexistent id must never be disclosed.
    assert client.get("/api/reports/999999").status_code == 401


def test_gate_public_set_intact():
    r = client.post("/api/reports", json={
        "title": "Public intake probe",
        "description": "Intake must stay open to citizens without a session.",
        "category": "roads",
    })
    assert r.status_code == 201, r.text
    assert client.get("/api/authorities").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/dashboard").status_code == 200


def test_gate_signed_in_case_data_reads_200():
    _set_admin()
    assert _login().status_code == 200
    rid = _seed_report()
    assert client.get("/api/reports").status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert client.get(f"/api/reports/{rid}").status_code == 200
    assert client.get(f"/api/reports/{rid}/detail").status_code == 200


# --- creds set: happy path + JWT semantics -----------------------------------


def test_login_success_sets_cookie_and_unlocks_routes():
    rid = _seed_report()
    # Route happy path needs an active authority for the report's category.
    c = db.sqlite3.connect(db.DB_PATH)
    c.execute(
        "INSERT INTO authorities (name, category, priority, active)"
        " VALUES ('KCCA Roads', 'roads', 5, 1)"
    )
    c.commit()
    c.close()
    _set_admin()
    r = _login()
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


def test_cookie_is_jws_with_default_ttl_claims():
    _set_admin()
    r = _login()
    assert r.status_code == 200, r.text
    token = client.cookies.get("reporta_session")
    parts = token.split(".")
    assert len(parts) == 3
    header = _segment(parts[0])
    payload = _segment(parts[1])
    assert header["alg"] == "HS256"
    assert payload["sub"] == "admin"
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["exp"], int)
    # Default lifetime is exactly 12h.
    assert payload["exp"] - payload["iat"] == 43200


def test_jwt_ttl_seconds_override_honored():
    _set_admin()
    os.environ["JWT_TTL_SECONDS"] = "60"
    r = _login()
    assert r.status_code == 200, r.text
    payload = _segment(client.cookies.get("reporta_session").split(".")[1])
    assert payload["exp"] - payload["iat"] == 60
    assert "max-age=60" in r.headers["set-cookie"].lower()


def test_login_wrong_password_401_no_session():
    _set_admin()
    r = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"
    assert not client.cookies.get("reporta_session")


def test_logout_clears_cookie_and_locks():
    _set_admin()
    login = _login()
    assert login.status_code == 200
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}/detail").status_code == 200
    client.post("/api/admin/logout")
    client.cookies.pop("reporta_session", None)
    assert client.get(f"/api/reports/{rid}/detail").status_code == 401


# --- token integrity ----------------------------------------------------------


def test_tampered_signature_401():
    _set_admin()
    assert _login().status_code == 200
    token = client.cookies.get("reporta_session")
    h, p, s = token.split(".")
    sig = bytearray(auth_mod._b64url_decode(s))
    sig[0] ^= 0x01
    tampered = f"{h}.{p}.{auth_mod._b64url_encode(bytes(sig))}"
    assert tampered != token
    client.cookies.set("reporta_session", tampered)
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}/detail").status_code == 401


def test_tampered_payload_401():
    _set_admin()
    assert _login().status_code == 200
    h, p, s = client.cookies.get("reporta_session").split(".")
    forged_payload = auth_mod._b64url_encode(
        json.dumps({"sub": "evil", "iat": 0, "exp": 9999999999}).encode()
    )
    client.cookies.set("reporta_session", f"{h}.{forged_payload}.{s}")
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}/detail").status_code == 401


def test_malformed_token_401():
    _set_admin()
    client.cookies.set("reporta_session", "forged-token")
    rid = _seed_report()
    assert client.get(f"/api/reports/{rid}/detail").status_code == 401


def test_expired_token_401(monkeypatch):
    _set_admin()
    os.environ["JWT_TTL_SECONDS"] = "1"
    # Patch only the auth module's clock reference: advancing the real time
    # module would make httpx's cookie jar drop the max-age=1 cookie first.
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        auth_mod, "time", types.SimpleNamespace(time=lambda: clock["now"])
    )
    assert _login().status_code == 200
    assert _segment(client.cookies.get("reporta_session").split(".")[1])[
        "exp"
    ] == 1001
    rid = _seed_report()
    # Advance past expiry: no server-side store needed, exp claim governs.
    clock["now"] = 1002.0
    r = client.get(f"/api/reports/{rid}/detail")
    assert r.status_code == 401
    assert r.json()["detail"] == "admin session expired"


def test_restart_independence_fresh_client_accepts_token():
    """A second client (fresh process analogue) accepts the first's token."""
    _set_admin()
    rid = _seed_report()
    assert _login().status_code == 200
    token = client.cookies.get("reporta_session")
    assert token

    fresh = TestClient(app)
    fresh.cookies.set("reporta_session", token)
    assert fresh.get(f"/api/reports/{rid}/detail").status_code == 200


# --- cookie flags (review ask 15:44Z: Secure + SameSite for HTTPS face) -------


def test_cookie_flags_default_no_secure_flag():
    """Default (local http testing): no Secure flag; SameSite/httponly set."""
    _set_admin()
    r = _login()
    assert r.status_code == 200
    set_cookie = r.headers["set-cookie"].lower()
    assert "samesite=lax" in set_cookie
    assert "httponly" in set_cookie
    assert "secure" not in set_cookie


def test_cookie_flags_cookie_secure_sets_secure():
    """Box/HTTPS deploy sets COOKIE_SECURE=1 -> Secure flag present."""
    _set_admin()
    os.environ["COOKIE_SECURE"] = "1"
    r = _login()
    assert r.status_code == 200
    set_cookie = r.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "httponly" in set_cookie
