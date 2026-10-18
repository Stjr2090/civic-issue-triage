# Reporta Phase 3 — Audio Intake Build Spec (v1.2, consolidated)

Status: lead-blessed (v1 10:24, v1.1 10:30, v1.2 deltas 10:35–10:38, gate-read folds A/B/D 10:45). Lift condition met on the draft side — verbatim copy only.
Build gates (binding order): PR #13 merge → Steph's on-record key confirm (+ spend) → phase3 branch → standing-rule opencode build → Agent-X QA matrix (9 items + 1 bead, v1.2.1) → obbo gate (spot-checks lift diff).
Doc target: `docs/phase3-audio-intake-spec.md` on the phase3 branch — the lift is a copy of this file.

## Module — `app/transcriber.py`
Mirrors `app/classifier.py` contract: always-returns-never-raises, ONE retry, no secrets ever logged.
- `GROQ_API_KEY` read at CALL time.
- Model: `DEFAULT_WHISPER_MODEL = "whisper-large-v3-turbo"` (lead-pinned 10:38 ruling). Override env `WHISPER_MODEL`, CALL-time read. Deliberately NOT `GROQ_MODEL` — that var belongs to T4's chat classifier.
- `whisper-large-v3` documented as fallback switch; the post-key Luganda WER check decides whether the demo needs v3.
- Returns `{"text", "quality", "mode"}`, `mode ∈ {"groq","unavailable"}`.

## Model & cost (gate-verified, obbo live fetch 10:36:38Z @ console.groq.com/docs/speech-to-text)
- turbo $0.04/hr (WER 12% en, no translation), v3 $0.111/hr (WER 10.3%, translates). Min billed 10s. 25MB free cap (100MB dev).
- Canonical pilot math: 1,000 × 60s notes = 16.7 audio-hours ≈ **$0.67 turbo** / $1.85 v3.
- Translation endpoint: non-goal v1.

## Opaque storage compliance
`file_ref` is OPAQUE (0001 header: readers must not parse or reinterpret). Transcriber accepts **bytes only**, never sees a `file_ref`.
- web: FastAPI `UploadFile` bytes.
- telegram: `getFile` → bytes; resolver uploads to Groq under `.ogg` name/content-type (Telegram arrives `.oga`; Groq supported types lack `.oga` — docs byte-check 10:18, independently verified 10:37). Bytes untouched — filename hint only, storage semantics unchanged. Raw `.oga` passthrough = post-key-confirm nice-to-have, NOT a build dependency.
- >25MB or unsupported container: actionable rejection; no chunking v1.

## Wiring — zero new decision surface
Transcript feeds the EXISTING T4 text pipeline (`classifier.classify(title_text, transcript_text)`). Category / urgency / confidence / safety floor (EN + Luganda + Swahili seeds) ride unchanged — spoken reports get the safety second pass for free.

## Intake text rules (never silently empty, never 422)
- `title = "[transcript] " + transcript[:80]` → 13+80 = 93 « 200 max.
- `description = "[transcript] " + transcript[:3987]` → 13+3987 = **4000 exactly** (obbo's boundary fix; kills the 4013→422 hole at max-length).
- Empty/failed transcript: `"[transcript] "` alone (13 chars clears `ReportIn` minimums 3/10, reports.py:19-20) → files clean, `needs_review=1` forced, audio preserved, audit `mode=unavailable`. Same rule in 0002-mode and zero-schema fallback.
- **Confirmed transcript** (Finding B, 10:45): the text a human has ratified at the confirm step, per the split contract. With a confirmed transcript, needs_review follows T4/T9 rules normally. The empty case = no confirmed text ⇒ marker-only filing.
- Reviewer edits happen in Phase 2 admin case-detail — no scope growth.
- 80-char convention reuses `telegram_bot.py:95`'s existing truncation habit (one convention, not two).

## Intake surfaces (Finding A, ruled 10:45)
- **Telegram (demo path):** voice accepted at the description step — the step's guidance prompt points at the mic. Voice never blocks and never drops: same fallback contract as everywhere else in the intake path (transcription failure ⇒ marker-only filing, `needs_review=1`, audio preserved, audit entry).
- **Web:** voice intake deferred this phase — explicit "web voice intake deferred" line, no file-input surface. If the web form grows an audio line later, it rides a later branch (Steph's no-over-engineering ruling).

## Quality gate (fail-closed, CALL-TIME — declared deviation from classifier.py:33-37 import-time floors)
`verbose_json` segments expose `avg_logprob` / `no_speech_prob` / `compression_ratio`. CALL-time env reads (precedent: classifier's own call-time key/model reads; enables in-process monkeypatch QA):
- `TRANSCRIPT_LOGPROB_FLOOR = -0.5` (flag if below)
- `TRANSCRIPT_NO_SPEECH_CEIL = 0.9` (flag if above)
- `TRANSCRIPT_COMPRESSION_RANGE = "0.5,2.5"` (flag if outside)
Any flag ⇒ `needs_review=1`, reason string stored in `quality_json`. Starting defaults, tunable by evidence.

## Schema — 0002, blessed as written 10:24 (kept byte-identical here)
`db/migrations/0002-reports-transcripts.sql`, additive + idempotent, `reports` untouched:
```sql
CREATE TABLE IF NOT EXISTS reports_transcripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES reports_attachments(id),
    text          TEXT    NOT NULL,
    quality_json  TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reports_transcripts_attachment
    ON reports_transcripts(attachment_id);
```
`text` holds the raw transcript (may be empty string). Zero-schema fallback (documented): transcript into description via the marker rules above; audit via `audit_log`.

## Failure path (intake must never block, never drop)
No key / timeout / bad-JSON-after-retry / API error ⇒ `mode=unavailable`: report still files (marker rules), audio row remains the record, `audit_log` entry. No queueing v1.

## Tests (no live network — httpx monkeypatched, key unset; pattern: tests/test_classification.py)
1. Happy-path JSON → transcript row, mode=groq, quality_json populated.
2. Timeout → one retry → unavailable; bad-JSON after retry → unavailable; key-unset → unavailable.
3. Quality any-flag → needs_review=1 + reason in quality_json; env-override shift test via in-process `monkeypatch.setenv` (call-time contract).
4. Never-blocks property test (intake latency bounded, classifier failure or transcriber failure must not raise).
5. Empty-transcript filing: no 422, needs_review=1, audio preserved, audit mode=unavailable.
6. Max-length transcript (exactly 4000 chars) → description lands at exactly 4000 **including** prefix, files clean.
7. Title format assert: `[transcript] ` + first 80 chars.
8. Resolver `.ogg` normalization: telegram-shaped bytes upload under `.ogg` name/content-type, bytes untouched (keyless, CI-runnable).
9. Audit rows: success meta on "transcribed"; `mode=unavailable` + reason on failure.
10. Container env parity (compose QA): `GROQ_API_KEY`, `GROQ_MODEL`, `WHISPER_MODEL`, `TRANSCRIPT_*` all ride `environment:` (Phase 1 logged gap: only PORT rides today — obbo's split rules the fix rides a compose-parity change, not the audio PR).
11. Model-default bead (Agent-X pack v1.2.1, lead-accepted 10:39): with `WHISPER_MODEL` unset, the transcription request payload carries `model=whisper-large-v3-turbo`; with it set, the env value flows through byte-exact. Asserts the request payload (monkeypatched post path, T4 fake-post style) — never a live call.

## Non-goals v1
Chunking (>25MB), translation endpoint, async queue/workers, multi-track audio, speaker diarization.

## Post-key lane (after Steph's confirm; merge first per gate order)
- Killed-mid-flight drill (Agent-X).
- Luganda/English WER sanity — RECOMMENDED (lead-ruled 10:38): one short Luganda sample through turbo AND v3; flat delta ⇒ turbo ships confidently; blown delta ⇒ v3 for the demo.
- Raw `.oga` passthrough acceptance check.
- Bot echo-confirm flow (nice-to-have, per gate lean 10:44): optional confirmation step where the bot echoes the transcript for ratification before filing; the confirmed-transcript definition above is flow-agnostic.
- Pricing caveat ledger: EMPTY (gate-verified 10:36:38Z; spend numbers match builder byte-check 10:18 to the cent).

## Lane history (receipt anchors)
- 09:50 obbo live-fire: whisper-large-v3 + turbo HTTP 200 on the app key (capability banked).
- 10:24 spec v1 accepted + 0002 blessed; 10:30 v1.1 (title N=80, call-time thresholds, empty-transcript test adopted).
- 10:32 obbo boundary catch (3987) + attribution sweep; 10:35 lead rules both v1.2 deltas; 10:36 Agent-X pack re-lock; 10:37 obbo pricing gate-verify + turbo lean; 10:38 lead pins turbo + WER check recommended.
