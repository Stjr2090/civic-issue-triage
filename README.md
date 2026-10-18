# civic-issue-triage

Civic issue reporting for the NYLP AI Hackathon 2026. Citizens report problems through a **web form** or a **Telegram bot**; every report is classified for **urgency** (AI when available, keyword rules otherwise), **routed** to the responsible local authority, and tracked on a **priority dashboard**.

FastAPI + SQLite. Python 3.14. No database server required.

## Problem

Physical service-delivery defects (potholes, burst pipes, overflowing waste, broken streetlights, damaged public facilities) persist in cities long after citizens first report them. Feasibility research in Uganda and an independent market study of Karachi, Pakistan, bracket the same failure from opposite ends.

In Uganda, the reporting pipeline barely exists: citizens lack a low-friction, persistent channel to reach the body responsible for fixing a defect; the channels that do exist are one-off and unaccountable (a call to a councillor, a radio phone-in, a WhatsApp group that dies within a week); and no verified public baseline exists for how long civic incidents take to resolve. In Karachi, complaint infrastructure exists at scale: the Pakistan Citizen's Portal has processed over six million complaints, nearly one million of them in municipal services, yet reported satisfaction in that category is 47%. But responsibility for one visible problem is split across multiple authorities depending on service and exact location, duplicate reports of the same defect arrive as unrelated tickets, and a complaint marked resolved is not independently verified as fixed.

The two pictures converge on one gap: the problem is not only whether a citizen can report, but whether each report is classified consistently, routed to the authority that actually owns the defect, linked to duplicates so recurring problems become visible, and closed only when the outcome is confirmed. Reports that fail any of these steps die quietly. The cost, in both markets, is avoidable accidents and health hazards, wasted public spending, and citizens who stop reporting because nothing shows that reporting changes anything.

This prototype demonstrates the front of that pipeline: classification with a keyword fallback and a safety floor, routing suggestions with human review on uncertain cases, and status tracking on an auditable dashboard. Duplicate detection and citizen-confirmed resolution remain on the roadmap.

## How it works

1. **Intake** — web form at `/`, or the Telegram bot with a category keyboard.
2. **Classification** — with `GROQ_API_KEY` set, a hosted LLM classifies urgency and suggests a category (strict JSON, one retry, timeout-guarded). Without a key — or on any provider failure — intake degrades to keyword rules and **never blocks**. A safety floor (fire, flood, sewage, …) **raises** urgency and never lowers it, in every mode.
3. **Fail-closed defaults** — `other` categories, keyword-mode results, low-confidence answers, and model disagreements are flagged for human review instead of auto-routing.
4. **Routing + audit** — reports map to seeded local authorities; every classification and routing decision is appended to an audit log.

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

All environment variables are **optional** — the app runs with sensible defaults and degrades gracefully.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PATH` | `./data/civic.db` | SQLite database location |
| `PORT` | `8000` | Server port (Docker image) |
| `GROQ_API_KEY` | — | Enables AI classification; without it, keyword fallback is used |
| `GROQ_MODEL` | provider default | Chat model used for classification; set a chat-capable model available on your account |
| `CLASSIFIER_TIMEOUT` | `5` | Seconds before the AI call gives up and falls back |
| `CLASSIFIER_SAFETY_FLOOR` | `5` | Minimum urgency for safety-term matches (raise-only) |
| `CLASSIFICATION_CONFIDENCE_FLOOR` | `0.60` | Confidence below this sends a report to human review |
| `TELEGRAM_BOT_TOKEN` | — | Enables the Telegram bot |
| `API_BASE` | `http://localhost:8000` | API URL the Telegram bot talks to |

Secrets belong in `.env` (already excluded via `.gitignore` and `.dockerignore`) — never in the repo.

## Telegram bot

```bash
export TELEGRAM_BOT_TOKEN=your-token
python -m app.telegram_bot
```

The bot uses outbound long-polling only — no inbound ports. A guided conversation (category → description) files one report with the reporter's handle as source attribution.

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

27 tests; they require no API keys and no network — CI-safe by design.

## Known limitations

This is a hackathon prototype; we would rather state its edges than hide them:

- **Demo-grade auth.** Intake is public by design; dashboard mutation endpoints are unauthenticated. Add authentication before any real deployment.
- **Single-node SQLite.** One file; schema changes now run through a small idempotent SQL migration loader (`db/migrations`) rather than a full migration framework, and heavier schema work still favors a fresh volume and re-seed.
- **Confidence is model-reported, not calibrated.** The review threshold is a sensible heuristic, not a measured operating point.
- **Keyword fallback is blunt.** Substring matching can over-flag (the safe direction), and the safety terms are not an exhaustive multilingual list.
- **Intake is unthrottled.** No rate limiting on either intake path.
- **Routing records intent, it does not dispatch.** Authorities and contacts are seed data; no SMS/email is sent.
- **Reporter fields are stored in plaintext.** Real deployments need encryption and a retention policy.

## Roadmap

- Authentication for dashboard operators
- PostgreSQL + schema migrations
- Notification dispatch (SMS/email) on routing
- Calibrated confidence thresholds and rate limiting
- Deeper multilingual intake support

## License

[MIT](LICENSE)
