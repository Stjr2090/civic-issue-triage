"""T4 tests: AI classification + safety floor + fail-closed keyword mode.

No live network: httpx.post is monkeypatched and GROQ_API_KEY is unset unless a
test injects it. Every test must pass with GROQ_API_KEY unset.
"""
import json
import os
import sqlite3
import tempfile

import pytest

tmp = tempfile.mkdtemp()
os.environ["TMPDIR"] = tmp

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db  # noqa: E402
import app.classifier as classifier  # noqa: E402

db.DB_PATH = db.Path(tmp) / "test_classification.db"
conn = sqlite3.connect(db.DB_PATH)
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app.routers.reports as reports_mod  # noqa: E402

reports_mod.DB_PATH = db.DB_PATH

from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _schema():
    """Make sure the active reports DB has the schema, whatever module won."""
    conn = sqlite3.connect(reports_mod.DB_PATH)
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Default: no Groq key, so nothing can reach the network by accident."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class _FakeResp:
    def __init__(self, content, exc=None):
        self._content = content
        self._exc = exc

    def raise_for_status(self):
        if self._exc is not None:
            raise self._exc

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _fake_post(monkeypatch, items):
    """Install a fake httpx.post. Items are responses/exceptions in order."""
    calls = {"n": 0}

    def post(url, headers=None, json=None, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        item = items[min(idx, len(items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(classifier.httpx, "post", post)
    return calls


def _ai(content: dict) -> _FakeResp:
    return _FakeResp(json.dumps(content))


def _post_report(title, description, category):
    return client.post("/api/reports", json={
        "title": title,
        "description": description,
        "category": category,
    })


def test_happy_path_ai_stores_fields_and_preserves_category(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ai({
        "category": "roads", "urgency": "high", "confidence": 0.82,
    })])

    r = _post_report(
        "Pothole on Main Rd",
        "Deep pothole near the market, two vehicles already damaged.",
        "roads",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["category"] == "roads"
    assert body["urgency"] == 4
    assert abs(body["confidence"] - 0.82) < 1e-9
    assert body["needs_review"] == 0
    assert "mode=ai" in body["reason"]
    assert "suggested=roads" in body["reason"]
    assert "conf=0.82" in body["reason"]
    assert "urgency=high(4)" in body["reason"]


def test_low_confidence_below_floor_is_flagged(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ai({
        "category": "waste", "urgency": "medium", "confidence": 0.40,
    })])

    r = _post_report(
        "Bin maybe?",
        "There might be an issue with collection on this street sometime.",
        "waste",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_review"] == 1
    assert "mode=ai" in body["reason"]
    assert "conf=0.40" in body["reason"]


def test_disagreement_keeps_citizen_category_and_flags(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ai({
        "category": "water", "urgency": "medium", "confidence": 0.90,
    })])

    r = _post_report(
        "Cracked surface",
        "The road surface looks badly worn along the whole stretch.",
        "roads",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["category"] == "roads"
    assert body["needs_review"] == 1
    assert "suggested=water" in body["reason"]


def test_keyword_fallback_without_key(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("classifier must not call the network without a key")

    monkeypatch.setattr(classifier.httpx, "post", _explode)

    r = _post_report(
        "Pothole on the road",
        "A large pothole has formed on the street outside the shops.",
        "roads",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_review"] == 1
    assert body["urgency"] == 3
    assert body["confidence"] == 0.0
    assert "mode=keyword" in body["reason"]
    assert "suggested=roads" in body["reason"]


def test_floor_raises_ai_low_to_five(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ai({
        "category": "water", "urgency": "low", "confidence": 0.95,
    })])

    r = _post_report(
        "Water issue",
        "There is a burst sewage pipe flooding the lane near the school.",
        "water",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["urgency"] == 5
    assert "floor_hit=1" in body["reason"]


def test_floor_applies_in_keyword_mode(monkeypatch):
    r = _post_report(
        "Smell near estate",
        "There is a strong gas leak reported close to the housing blocks.",
        "public_services",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["urgency"] == 5
    assert body["needs_review"] == 1
    assert "mode=keyword" in body["reason"]
    assert "floor_hit=1" in body["reason"]


def test_malformed_json_twice_falls_back_to_keyword(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    calls = _fake_post(monkeypatch, [_FakeResp("definitely not json")])

    r = _post_report(
        "Rubbish pile",
        "Garbage has piled up on the corner and is starting to smell.",
        "waste",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_review"] == 1
    assert "mode=keyword" in body["reason"]
    assert calls["n"] == 2  # exactly one retry


def test_timeout_falls_back_to_keyword(monkeypatch):
    import httpx

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [httpx.ReadTimeout("too slow")])

    r = _post_report(
        "Broken streetlight",
        "The streetlight at the junction has been out for over a month.",
        "public_services",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_review"] == 1
    assert "mode=keyword" in body["reason"]


def test_ai_other_never_overwrites_citizen_category(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ai({
        "category": "other", "urgency": "medium", "confidence": 0.88,
    })])

    r = _post_report(
        "Odd complaint",
        "Something unusual that the model may not know how to classify.",
        "roads",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["category"] == "roads"
    assert body["needs_review"] == 1
    assert body["status"] == "new"
    assert "suggested=other" in body["reason"]


def test_one_malformed_then_valid_recovers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    calls = _fake_post(monkeypatch, [
        _FakeResp("{ broken"),
        _ai({"category": "roads", "urgency": "medium", "confidence": 0.70}),
    ])

    r = _post_report(
        "Road edge crumbling",
        "The edge of the road is crumbling and needs scheduled repair.",
        "roads",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_review"] == 0
    assert "mode=ai" in body["reason"]
    assert calls["n"] == 2
