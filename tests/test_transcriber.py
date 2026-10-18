"""Phase 3 tests: transcriber fail-closed contract, quality gate, intake texts.

No live network: ``transcriber.httpx.post`` is monkeypatched and
``GROQ_API_KEY`` is unset unless a test injects it. Every test passes with
``GROQ_API_KEY`` unset. Pattern mirrors tests/test_classification.py.
"""
import json
import sqlite3

import httpx
import pytest

import app.db as db
import app.transcriber as transcriber
from app.routers.reports import ReportIn

MARKER = "[transcript] "


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Default: no Groq key, so nothing can reach the network by accident."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class _FakeResp:
    def __init__(self, body=None, raw=None, exc=None, status=200):
        self._body = body
        self._raw = raw
        self._exc = exc
        self.status_code = status

    def raise_for_status(self):
        if self._exc is not None:
            raise self._exc
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request(
                    "POST", transcriber.GROQ_TRANSCRIPTION_ENDPOINT
                ),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


def _fake_post(monkeypatch, items):
    """Install a fake httpx.post. Items are responses/exceptions in order."""
    state = {"n": 0, "calls": []}

    def post(url, headers=None, data=None, files=None, timeout=None, **kwargs):
        state["calls"].append(
            {
                "url": url,
                "headers": headers,
                "data": data,
                "files": files,
                "timeout": timeout,
            }
        )
        idx = state["n"]
        state["n"] += 1
        item = items[min(idx, len(items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(transcriber.httpx, "post", post)
    return state


def _body(text="hello world", **seg):
    return {"text": text, "segments": [seg] if seg else []}


def _ok(text="hello world", **seg):
    return _FakeResp(body=_body(text, **seg))


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_groq_text_and_quality(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(
        monkeypatch,
        [_ok("Hello from the audio note", avg_logprob=-0.2,
             no_speech_prob=0.05, compression_ratio=1.2)],
    )

    result = transcriber.transcribe(b"audio-bytes")

    assert result["mode"] == "groq"
    assert result["text"] == "Hello from the audio note"
    quality = result["quality"]
    assert quality["mode"] == "groq"
    assert quality["flag"] is False
    assert quality["flags"] == []
    assert quality["reason"] == ""
    assert quality["segments"] == 1
    assert state["n"] == 1


def test_posts_to_endpoint_with_bearer_and_verbose_json(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_ok("x")])

    transcriber.transcribe(b"x")

    call = state["calls"][0]
    assert call["url"] == transcriber.GROQ_TRANSCRIPTION_ENDPOINT
    assert call["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["data"]["response_format"] == "verbose_json"


def test_key_never_leaks_into_result(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-value")
    _fake_post(monkeypatch, [_ok("x")])

    result = transcriber.transcribe(b"x")

    assert "super-secret-value" not in json.dumps(result)


# ---------------------------------------------------------------------------
# 2. Timeout / invalid JSON / key-unset -> unavailable
# ---------------------------------------------------------------------------

def test_timeout_retries_once_then_unavailable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [httpx.ReadTimeout("too slow")])

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["text"] == ""
    assert result["quality"]["reason"] == "timeout"
    assert state["n"] == 2  # exactly one retry


def test_non_200_retries_once_then_unavailable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_FakeResp(status=500)])

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["quality"]["reason"] == "http_error"
    assert state["n"] == 2


def test_invalid_json_after_retry_unavailable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_FakeResp(raw="{not valid json")])

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["quality"]["reason"] == "invalid_json"
    assert state["n"] == 2


def test_body_missing_text_is_unavailable_after_retry(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_FakeResp(body={"segments": []})])

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["quality"]["reason"] == "invalid_json"
    assert state["n"] == 2


def test_one_bad_then_valid_recovers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_FakeResp(raw="oops"), _ok("recovered")])

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "groq"
    assert result["text"] == "recovered"
    assert state["n"] == 2


def test_key_unset_zero_http_calls(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("no HTTP call is allowed without a key")

    monkeypatch.setattr(transcriber.httpx, "post", _explode)

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["text"] == ""
    assert result["quality"]["reason"] == "no_key"


def test_empty_key_zero_http_calls(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")

    def _explode(*a, **k):
        raise AssertionError("no HTTP call is allowed for an empty key")

    monkeypatch.setattr(transcriber.httpx, "post", _explode)

    result = transcriber.transcribe(b"x")

    assert result["mode"] == "unavailable"
    assert result["quality"]["reason"] == "no_key"


# ---------------------------------------------------------------------------
# 3. Quality flags + call-time thresholds
# ---------------------------------------------------------------------------

def test_low_logprob_flags_and_reason(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ok("mumbled", avg_logprob=-0.9)])

    quality = transcriber.transcribe(b"x")["quality"]

    assert quality["flag"] is True
    assert "low_logprob" in quality["flags"]
    assert "low_logprob" in quality["reason"]


def test_high_no_speech_flags_and_reason(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ok("silence", no_speech_prob=0.97)])

    quality = transcriber.transcribe(b"x")["quality"]

    assert quality["flag"] is True
    assert "high_no_speech" in quality["flags"]
    assert "high_no_speech" in quality["reason"]


def test_compression_out_of_range_flags_and_reason(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ok("weird", compression_ratio=3.4)])

    quality = transcriber.transcribe(b"x")["quality"]

    assert quality["flag"] is True
    assert "compression_out_of_range" in quality["flags"]
    assert "compression_out_of_range" in quality["reason"]


def test_thresholds_are_read_at_call_time(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(
        monkeypatch,
        [_ok("x", avg_logprob=-0.9, no_speech_prob=0.95,
             compression_ratio=3.0)],
    )

    assert transcriber.transcribe(b"x")["quality"]["flag"] is True

    monkeypatch.setenv("TRANSCRIPT_LOGPROB_FLOOR", "-2.0")
    monkeypatch.setenv("TRANSCRIPT_NO_SPEECH_CEIL", "0.99")
    monkeypatch.setenv("TRANSCRIPT_COMPRESSION_RANGE", "0.5,4.0")

    quality = transcriber.transcribe(b"x")["quality"]
    assert quality["flag"] is False
    assert quality["reason"] == ""


def test_invalid_threshold_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [_ok("x", avg_logprob=-0.9)])

    monkeypatch.setenv("TRANSCRIPT_LOGPROB_FLOOR", "not-a-number")
    quality = transcriber.transcribe(b"x")["quality"]

    assert quality["flag"] is True
    assert "low_logprob" in quality["flags"]


# ---------------------------------------------------------------------------
# 4. Never-raises property
# ---------------------------------------------------------------------------

_FAILURES = [
    httpx.ReadTimeout("timeout"),
    httpx.ConnectError("no route"),
    _FakeResp(status=503),
    _FakeResp(raw="{"),
    _FakeResp(body={}),
    _FakeResp(body=None),
]


@pytest.mark.parametrize("failure", _FAILURES)
def test_never_raises_across_failure_modes(monkeypatch, failure):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _fake_post(monkeypatch, [failure])

    for args in [(b"",), (None,), (b"some bytes",)]:
        result = transcriber.transcribe(*args)
        assert set(result) >= {"text", "quality", "mode"}
        assert result["mode"] in {"groq", "unavailable"}


def test_never_raises_without_key(monkeypatch):
    result = transcriber.transcribe(b"anything")
    assert result["mode"] == "unavailable"


# ---------------------------------------------------------------------------
# 5. Intake text rules
# ---------------------------------------------------------------------------

def test_empty_transcript_yields_marker_only_valid_texts():
    title, description = transcriber.make_intake_text("")
    assert title == MARKER
    assert description == MARKER
    assert len(title) == 13
    assert len(description) == 13
    ReportIn(title=title, description=description, category="other")

    # None is treated as empty, never raises.
    assert transcriber.make_intake_text(None) == (MARKER, MARKER)


def test_max_length_boundary_is_exactly_4000():
    transcript = "a" * 5000
    title, description = transcriber.make_intake_text(transcript)

    assert description == MARKER + transcript[:3987]
    assert len(description) == 4000
    assert len(title) == 93
    assert title == MARKER + transcript[:80]
    ReportIn(title=title, description=description, category="other")

    # Exact boundary: a 3987-char transcript also lands at exactly 4000.
    _, at_boundary = transcriber.make_intake_text("b" * 3987)
    assert len(at_boundary) == 4000

    # One char shorter is under the cap.
    _, under = transcriber.make_intake_text("b" * 3986)
    assert len(under) == 3999


def test_title_is_marker_plus_first_80_chars():
    transcript = "".join(str(i % 10) for i in range(5000))
    title, description = transcriber.make_intake_text(transcript)

    assert title == MARKER + transcript[:80]
    assert len(title) == 13 + 80
    assert description == MARKER + transcript[:3987]
    assert len(description) == 4000


# ---------------------------------------------------------------------------
# 7. Migration 0002
# ---------------------------------------------------------------------------

def test_0002_migration_applies_cleanly_twice(tmp_path):
    conn = sqlite3.connect(tmp_path / "mig.db")
    conn.executescript(db.SCHEMA)

    db.apply_migrations(conn)
    db.apply_migrations(conn)  # idempotent

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "reports_transcripts" in tables
    assert "reports_attachments" in tables

    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reports_transcripts)")
    }
    assert cols == {
        "id", "attachment_id", "text", "quality_json", "created_at",
    }

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_reports_transcripts_attachment" in indexes

    # reports is untouched: original insert path still works.
    conn.execute(
        "INSERT INTO reports (title, description, category) VALUES (?,?,?)",
        ("Title", "A description long enough", "roads"),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# 11. Model default bead + Telegram .ogg normalization
# ---------------------------------------------------------------------------

def test_model_default_and_env_override_payload(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    state = _fake_post(monkeypatch, [_ok("x")])

    transcriber.transcribe(b"x")
    assert state["calls"][0]["data"]["model"] == "whisper-large-v3-turbo"

    monkeypatch.setenv("WHISPER_MODEL", "whisper-large-v3")
    transcriber.transcribe(b"x")
    assert state["calls"][1]["data"]["model"] == "whisper-large-v3"


def test_model_ignores_groq_model_and_empty_whisper_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("WHISPER_MODEL", "")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.1-8b-instant")
    state = _fake_post(monkeypatch, [_ok("x")])

    transcriber.transcribe(b"x")

    assert state["calls"][0]["data"]["model"] == "whisper-large-v3-turbo"


def test_telegram_oga_normalized_to_ogg_bytes_untouched(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    state = _fake_post(monkeypatch, [_ok("x")])
    payload = b"\x00\x01telegram-voice\xff"

    transcriber.transcribe(
        payload, filename="voice.oga", content_type="application/octet-stream"
    )

    name, content, content_type = state["calls"][0]["files"]["file"]
    assert name == "voice.ogg"
    assert content_type == "audio/ogg"
    assert content == payload
