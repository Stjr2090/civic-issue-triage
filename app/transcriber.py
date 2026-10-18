"""Phase 3 audio intake: Groq Whisper transcription with fail-closed quality.

Design (see docs/phase3-audio-intake-spec.md):

- ``transcribe`` posts audio bytes to Groq's OpenAI-compatible
  ``/audio/transcriptions`` endpoint as a multipart upload, asking for
  ``verbose_json`` so per-segment quality signals come back.
- The module mirrors ``app/classifier.py``'s contract: it ALWAYS returns a
  result and NEVER raises. Intake must not block or drop a report because the
  provider is unavailable, so any failure (no key, timeout, non-200 after one
  retry, unparseable JSON) degrades to ``mode="unavailable"``.
- ``GROQ_API_KEY`` and ``WHISPER_MODEL`` are read from the env at CALL time.
  ``WHISPER_MODEL`` (not ``GROQ_MODEL``, which belongs to the chat classifier).
- Quality thresholds are read at CALL time too (a deliberate, declared
  deviation from classifier.py's import-time floors) so in-process
  ``monkeypatch.setenv`` can shift them.
- No secrets are ever logged.

The transcriber accepts bytes only; it never sees an opaque ``file_ref``.
"""

import json
import os

import httpx

GROQ_TRANSCRIPTION_ENDPOINT = (
    "https://api.groq.com/openai/v1/audio/transcriptions"
)
DEFAULT_WHISPER_MODEL = "whisper-large-v3-turbo"

# Default quality thresholds. These are string constants; the numeric values are
# parsed at CALL time from the env so callers can override per-process.
DEFAULT_LOGPROB_FLOOR = "-0.5"
DEFAULT_NO_SPEECH_CEIL = "0.9"
DEFAULT_COMPRESSION_RANGE = "0.5,2.5"

INTAKE_MARKER = "[transcript] "
TITLE_CHARS = 80
# 13 (marker) + 3987 = 4000 exactly, the ReportIn description hard cap.
DESCRIPTION_CHARS = 3987

_OGG_EXTENSIONS = {"oga", "ogg", "opus"}


class _TransientFailure(Exception):
    """Carries the last provider failure out of the retry loop."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _timeout() -> float:
    try:
        return float(os.environ.get("TRANSCRIBER_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30.0


def _model() -> str:
    """Whisper model at call time; unset/empty -> pinned default."""
    return os.environ.get("WHISPER_MODEL") or DEFAULT_WHISPER_MODEL


def _float_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raw = default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def logprob_floor() -> float:
    return _float_env("TRANSCRIPT_LOGPROB_FLOOR", DEFAULT_LOGPROB_FLOOR)


def no_speech_ceil() -> float:
    return _float_env("TRANSCRIPT_NO_SPEECH_CEIL", DEFAULT_NO_SPEECH_CEIL)


def compression_range():
    raw = os.environ.get("TRANSCRIPT_COMPRESSION_RANGE")
    if raw is None or raw == "":
        raw = DEFAULT_COMPRESSION_RANGE
    try:
        lo_s, hi_s = str(raw).split(",")
        lo, hi = float(lo_s), float(hi_s)
    except (TypeError, ValueError):
        lo, hi = (float(x) for x in DEFAULT_COMPRESSION_RANGE.split(","))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _normalize_upload(filename, content_type):
    """Return (name, content_type) with Telegram-shaped ``.oga`` mapped to
    ``.ogg`` (Groq's supported types lack ``.oga``). Bytes are untouched."""
    name = filename or "audio.ogg"
    ctype = (content_type or "").lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _OGG_EXTENSIONS or "ogg" in ctype or "opus" in ctype:
        base = name.rsplit(".", 1)[0] if "." in name else name
        return f"{base}.ogg", "audio/ogg"
    return name, content_type or "application/octet-stream"


def _reason_for(exc) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error"
    if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError, TypeError)):
        return "invalid_json"
    return "request_error"


def _quality(flag, flags, reasons, metrics, mode):
    return {
        "flag": bool(flag),
        "flags": list(flags),
        "reason": "; ".join(reasons),
        "reasons": list(reasons),
        "avg_logprob": metrics.get("avg_logprob"),
        "no_speech_prob": metrics.get("no_speech_prob"),
        "compression_ratio": metrics.get("compression_ratio"),
        "segments": metrics.get("segments", 0),
        "mode": mode,
    }


def _unavailable(reason: str) -> dict:
    return {
        "text": "",
        "quality": _quality(True, [reason], [reason], {}, "unavailable"),
        "mode": "unavailable",
    }


def _parse_verbose(body: dict):
    """Parse ``verbose_json`` into (text, quality); raise on unusable bodies."""
    text = body["text"]
    text = "" if text is None else str(text)
    segments = body.get("segments") or []

    logprobs, no_speech, compression = [], [], []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if seg.get("avg_logprob") is not None:
            logprobs.append(float(seg["avg_logprob"]))
        if seg.get("no_speech_prob") is not None:
            no_speech.append(float(seg["no_speech_prob"]))
        if seg.get("compression_ratio") is not None:
            compression.append(float(seg["compression_ratio"]))

    floor = logprob_floor()
    ceil = no_speech_ceil()
    lo, hi = compression_range()

    flags, reasons = [], []
    worst_logprob = min(logprobs) if logprobs else None
    if worst_logprob is not None and worst_logprob < floor:
        flags.append("low_logprob")
        reasons.append(
            f"low_logprob(avg_logprob={worst_logprob}<floor={floor})"
        )
    worst_no_speech = max(no_speech) if no_speech else None
    if worst_no_speech is not None and worst_no_speech > ceil:
        flags.append("high_no_speech")
        reasons.append(
            f"high_no_speech(no_speech_prob={worst_no_speech}>ceil={ceil})"
        )
    worst_ratio = None
    for ratio in compression:
        if ratio < lo or ratio > hi:
            worst_ratio = ratio
            flags.append("compression_out_of_range")
            reasons.append(
                f"compression_out_of_range(ratio={ratio} not in [{lo},{hi}])"
            )
            break
    if worst_ratio is None and compression:
        worst_ratio = max(compression)

    metrics = {
        "avg_logprob": worst_logprob,
        "no_speech_prob": worst_no_speech,
        "compression_ratio": worst_ratio,
        "segments": len(segments),
    }
    return text, _quality(bool(flags), flags, reasons, metrics, "groq")


def _ai_transcribe(audio_bytes, filename, content_type, key):
    """POST the audio; ONE retry on any transient failure, then raise."""
    if audio_bytes is None:
        audio_bytes = b""
    name, ctype = _normalize_upload(filename, content_type)
    payload = {"model": _model(), "response_format": "verbose_json"}
    files = {"file": (name, audio_bytes, ctype)}
    headers = {"Authorization": f"Bearer {key}"}

    last_error = None
    for _ in range(2):  # one retry
        try:
            response = httpx.post(
                GROQ_TRANSCRIPTION_ENDPOINT,
                headers=headers,
                data=payload,
                files=files,
                timeout=_timeout(),
            )
            response.raise_for_status()
            body = response.json()
            return _parse_verbose(body)
        except Exception as exc:  # noqa: BLE001 - fail closed below
            last_error = exc
    raise _TransientFailure(last_error)


def transcribe(audio_bytes, filename="audio.ogg", content_type="audio/ogg") -> dict:
    """Transcribe audio bytes; NEVER raises.

    Returns ``{"text", "quality", "mode"}`` where ``mode`` is ``"groq"`` on
    success or ``"unavailable"`` on any fail-closed path. An empty/unset
    ``GROQ_API_KEY`` short-circuits with no HTTP call at all.
    """
    try:
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            return _unavailable("no_key")
        try:
            text, quality = _ai_transcribe(
                audio_bytes, filename, content_type, key
            )
            return {"text": text, "quality": quality, "mode": "groq"}
        except _TransientFailure as exc:
            return _unavailable(_reason_for(exc.cause))
        except Exception as exc:  # noqa: BLE001 - fail closed
            return _unavailable(_reason_for(exc))
    except Exception:  # noqa: BLE001 - absolute never-raises guard
        return _unavailable("unexpected_error")


def make_intake_text(transcript):
    """Return (title, description) filing texts for a transcript.

    Pure function; never raises. Empty/failed transcripts yield marker-only
    strings that still clear ``ReportIn`` minimums (title>=3, description>=10).
    The 3987 description slice keeps ``marker + transcript`` at exactly the
    4000-char hard cap.
    """
    text = "" if transcript is None else str(transcript)
    return (
        INTAKE_MARKER + text[:TITLE_CHARS],
        INTAKE_MARKER + text[:DESCRIPTION_CHARS],
    )
