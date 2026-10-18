"""T-WA tests: WhatsApp webhook + migration 0003 (source=whatsapp)."""
import hashlib
import hmac
import os
import tempfile

import pytest

tmp = tempfile.mkdtemp()
os.environ["TMPDIR"] = tmp
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db  # noqa: E402

db.DB_PATH = db.Path(tmp) / "test.db"
conn = db.sqlite3.connect(db.DB_PATH)
conn.executescript(db.SCHEMA)
db.apply_migrations(conn)
conn.commit()
conn.close()

import app.routers.reports as reports_mod  # noqa: E402

reports_mod.DB_PATH = db.DB_PATH

from app.main import app  # noqa: E402
from app.routers import whatsapp  # noqa: E402

# Earlier test modules import app.main first, so the webhook module has
# already captured (empty) env at import — pin the test credentials here.
whatsapp.VERIFY_TOKEN = "test-token"
whatsapp.APP_SECRET = "test-secret"

client = TestClient(app)

META_SECRET = "test-secret"


def _meta_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(
        META_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


def _message_payload(text: str, sender: str = "256700000001") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "ENTRY1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "PNID1"},
                    "contacts": [{"profile": {"name": "Test"}, "wa_id": sender}],
                    "messages": [{"from": sender, "id": "wamid.X", "type": "text",
                                  "text": {"body": text}}],
                },
            }],
        }],
    }


# --- GET /webhook: Meta verification handshake -----------------------------

def test_verify_handshake_ok():
    r = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "test-token",
        "hub.challenge": "CHALLENGE_123",
    })
    assert r.status_code == 200
    assert r.text == "CHALLENGE_123"


def test_verify_handshake_wrong_token():
    r = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "WRONG",
        "hub.challenge": "CHALLENGE_123",
    })
    assert r.status_code == 403


# --- POST /webhook: ack fast, process in background ------------------------

def test_post_message_creates_report_via_intake():
    payload = _message_payload(
        "Burst pipe flooding the lane behind the market, water everywhere."
    )
    import json
    raw = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=raw,
        headers={"x-hub-signature-256": _meta_sig(raw),
                 "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"received": True}

    # BackgroundTasks run inline under TestClient — the report exists now.
    reports = client.get("/api/reports", params={"source": "whatsapp"}).json()
    assert len(reports) == 1
    row = reports[0]
    assert row["source"] == "whatsapp"          # migration 0003 CHECK holds
    assert row["category"] == "other"           # no category over WhatsApp
    assert row["needs_review"] == 1             # T9: other → review, never auto-route
    assert row["reporter"] == "256700000001"
    assert row["status"] == "new"
    assert "Burst pipe" in row["description"]


def test_post_status_receipts_logged_and_dropped():
    _whatsapp_count_before_receipts = len(
        client.get("/api/reports", params={"source": "whatsapp"}).json()
    )
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "ENTRY1",
            "changes": [{
                "field": "messages",
                "value": {"statuses": [{"id": "wamid.S", "status": "delivered"}]},
            }],
        }],
    }
    import json
    raw = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=raw,
        headers={"x-hub-signature-256": _meta_sig(raw),
                 "content-type": "application/json"},
    )
    assert r.status_code == 200
    reports = client.get("/api/reports", params={"source": "whatsapp"}).json()
    assert len(reports) == _whatsapp_count_before_receipts  # receipt dropped, nothing entered intake


def test_post_bad_signature_rejected():
    r = client.post(
        "/webhook",
        content=b"{\"entry\": []}",
        headers={"x-hub-signature-256": "sha256=deadbeef",
                 "content-type": "application/json"},
    )
    assert r.status_code == 403


def test_post_non_text_message_dropped():
    _count_before = len(
        client.get("/api/reports", params={"source": "whatsapp"}).json()
    )
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "ENTRY1",
            "changes": [{
                "field": "messages",
                "value": {"messages": [{"from": "256700000001",
                                        "id": "wamid.M", "type": "image"}]},
            }],
        }],
    }
    import json
    raw = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=raw,
        headers={"x-hub-signature-256": _meta_sig(raw),
                 "content-type": "application/json"},
    )
    assert r.status_code == 200
    reports = client.get("/api/reports", params={"source": "whatsapp"}).json()
    assert len(reports) == _count_before  # non-text dropped, nothing entered intake


# --- migration 0003: rebuild keeps rows, extends the CHECK -----------------

def test_migration_0003_allows_whatsapp_source_rejects_unknown():
    conn = db.sqlite3.connect(db.DB_PATH)
    # A whatsapp row already round-tripped through full intake above; here we
    # assert the CHECK directly.
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO reports (title, description, category, source)"
            " VALUES ('t', 'some description here', 'roads', 'carrier_pigeon')"
        )
    conn.close()
