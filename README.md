# Reporta

**Civic issue reporting, classification and routing.** Citizens report problems through a web form or a Telegram bot (WhatsApp intake is in progress); every report is classified for urgency, routed to the responsible local authority, and tracked on an auditable priority dashboard.

Built for the NYLP AI Hackathon 2026. Backend: FastAPI + SQLite, Python 3.14. No database server required.

Live deployment: <https://reporta.hingaai.tech>

## Why Reporta

Potholes, burst pipes, overflowing waste and broken streetlights persist in cities long after citizens first report them. Uganda's reporting pipeline barely exists: the channels citizens do use are one-off and unaccountable, and no verified public baseline shows how long civic incidents take to resolve. Where complaint systems exist at scale the pattern repeats; Karachi's Citizen's Portal has processed over six million complaints, yet reported satisfaction in municipal services is 47%, because reports are not consistently classified, not routed to the authority that actually owns the defect, and not verified as fixed.

Reporta demonstrates the front of that pipeline: consistent classification with a safety floor, routing with human review on uncertain cases, and status tracking on an auditable dashboard.

## Features

- **Multi-channel intake** - web form at `/`, Telegram bot with a guided category conversation; WhatsApp Cloud API intake is in progress.
- **Urgency classification** - a hosted LLM when `GROQ_API_KEY` is set (strict JSON, one retry, timeout-guarded); keyword-rule fallback otherwise. Intake **never blocks** on the model.
- **Safety floor** - fire, flood, sewage and similar terms *raise* urgency and never lower it, in every mode.
- **Fail-closed defaults** - `other` categories, keyword-mode results, low-confidence answers and model disagreements are flagged for human review instead of auto-routed.
- **Routing + audit** - reports map to seeded local authorities; every classification and routing decision is appended to an audit log.
- **Voice-note transcription** - Groq Whisper with no-speech and compression quality gates; degrades keylessly.
- **Priority dashboard** - filters by category, status, review flag and source, plus aggregate stats.

## Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | Python 3.14 |
| Web framework | FastAPI |
| Database | SQLite (file-based, no server), idempotent SQL migrations in `db/migrations` |
| AI classification | Groq-hosted chat model (optional; keyword fallback built in) |
| Speech-to-text | Groq Whisper (`whisper-large-v3-turbo` by default) |
| Chat intake | Telegram Bot API, outbound long-polling only |
| Tests | pytest, 64 tests, keyless and CI-safe |

## Quickstart

```bash
git clone https://github.com/Stjr2090/civic-issue-triage
cd civic-issue-triage
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py          # seeds 8 sample authorities
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` for the intake form and `http://localhost:8000/dashboard` for the dashboard.

With Docker:

```bash
docker compose up --build       # PORT env var overrides the default 8000
```

## Configuration

All environment variables are **optional** - the app runs with sensible defaults and degrades gracefully.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PATH` | `./data/civic.db` | SQLite database location |
| `PORT` | `8000` | Server port (Docker image) |
| `GROQ_API_KEY` | — | Enables AI classification and transcription; without it, keyword fallback is used |
| `GROQ_MODEL` | provider default | Chat model used for classification; set a chat-capable model available on your account |
| `WHISPER_MODEL` | `whisper-large-v3-turbo` | Whisper model used for voice-note transcription |
| `CLASSIFIER_TIMEOUT` | `5` | Seconds before the AI call gives up and falls back |
| `CLASSIFIER_SAFETY_FLOOR` | `5` | Minimum urgency for safety-term matches (raise-only) |
| `CLASSIFICATION_CONFIDENCE_FLOOR` | `0.60` | Confidence below this sends a report to human review |
| `TELEGRAM_BOT_TOKEN` | — | Enables the Telegram bot |
| `API_BASE` | `http://localhost:8000` | API URL the Telegram bot talks to |

Secrets belong in `.env` (already excluded via `.gitignore` and `.dockerignore`) - never in the repo.

## Telegram bot

```bash
export TELEGRAM_BOT_TOKEN=your-token
python -m app.telegram_bot
```

The bot uses outbound long-polling only - no inbound ports. A guided conversation (category → description) files one report with the reporter's handle as source attribution.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/reports` | File a report |
| `GET` | `/api/reports` | List reports (filters: `?category=`, `?status=`, `?needs_review=`, `?source=`) |
| `GET` | `/api/reports/{id}` | Report detail |
| `PATCH` | `/api/reports/{id}/status` | Update report status |
| `GET` | `/api/stats` | Dashboard aggregates |
| `GET` | `/api/authorities` | Seeded local authorities |
| `GET` | `/api/route/{id}` | Route a report to its matching authority (409 when no match) |
| `GET` | `/` | Intake form |
| `GET` | `/dashboard` | Priority dashboard |

## Testing

```bash
pytest
```

64 tests; they require no API keys and no network - CI-safe by design.

## Known limitations

This is a hackathon prototype; we would rather state its edges than hide them:

- **Demo-grade auth.** Intake is public by design; dashboard mutation endpoints are unauthenticated. Session authentication for mutating routes is in progress on a feature branch.
- **Single-node SQLite.** One file; schema changes run through a small idempotent SQL migration loader (`db/migrations`), and heavier schema work still favors a fresh volume and re-seed.
- **Confidence is model-reported, not calibrated.** The review threshold is a sensible heuristic, not a measured operating point.
- **Keyword fallback is blunt.** Substring matching can over-flag (the safe direction), and the safety terms are not an exhaustive multilingual list.
- **Voice intake is API plumbing.** Transcription runs on audio passed to the API; there is no upload surface in the UI yet.
- **Intake is unthrottled.** No rate limiting on either intake path.
- **Routing records intent, it does not dispatch.** Authorities and contacts are seed data; no SMS/email is sent.
- **Reporter fields are stored in plaintext.** Real deployments need encryption and a retention policy.

## Roadmap

- Admin authentication (session login on mutating routes) - in progress
- WhatsApp Cloud API intake - in progress
- SMS intake via an Android-phone gateway
- Duplicate detection and citizen-confirmed resolution
- PostgreSQL migration
- Calibrated confidence thresholds and rate limiting
- Multilingual intake and replies (Luganda)

## License

[MIT](LICENSE)
