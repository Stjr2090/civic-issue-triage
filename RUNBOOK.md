# Runbook: Deploy on Lightsail

Deployment target is an AWS Lightsail instance. The image is always **built on
Lightsail itself** — never build locally and push, to avoid cross-architecture
image problems (e.g. an arm64 laptop image failing on the x86 instance).

## Shared box (Lightsail already runs another app)

Discover before binding:

Step 0 — docker permissions: run `docker info` first. If it fails on permissions, either prefix every docker/compose command with `sudo`, or add your user to the group once (`sudo usermod -aG docker $USER`, then log out and back in).

```sh
ss -tlnp          # taken ports - avoid them
docker ps         # the other app's containers
ls /etc/nginx /etc/caddy 2>/dev/null   # reverse proxy present?
```
- Free port found -> set it: `PORT=8100 docker compose up -d --build`.
- Reverse proxy present -> do NOT take 80/443. Join as a second upstream: one new proxy route pointing at this container's port. Never edit the other app's proxy config; add a new file and reload.

## Deploy

1. SSH into the Lightsail instance.
2. Clone or pull the repo:
   ```sh
   git clone <repo-url> civic-issue-triage    # first time
   cd civic-issue-triage
   git pull                                   # subsequent deploys
   ```
3. Build and start (rebuilds the image on the instance):
   ```sh
   docker compose up -d --build
   ```
4. Verify the app is serving:
   ```sh
   curl localhost:8000/api/stats
   ```

## Localhost verification (no Docker needed)

For a quick local check on a laptop (Steph's visual pass) or a scratch run
on a shared box, plain uvicorn is enough — and a scratch run on a box that
already serves an app must use a scratch port and scratch DB so it cannot
collide with the live server:

```sh
git clone https://github.com/Stjr2090/civic-issue-triage && cd civic-issue-triage
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py                                  # 8 authorities
port=8123; db=$(mktemp -u /tmp/reporta-XXXX.db)         # scratch port + DB
DATABASE_PATH="$db" PORT=$port .venv/bin/uvicorn app.main:app --port $port
curl "localhost:$port/api/stats"                        # 200 when serving
```

**Known limit, by design:** `scripts/seed.py` seeds authorities only — a
fresh clone boots with **zero reports** and an empty dashboard until real
submissions arrive (see `deploy/lightsail-reporta` for the demo-report
seeder that fills this on the deploy box).

## Configuration environment variables

- The app reads its Groq configuration from the environment at call time:
  `GROQ_API_KEY` (required for AI classification; without it the classifier
  fails closed into keyword mode) and `GROQ_MODEL` (optional override of the
  classifier's chat model).
- Audio transcription (Phase 3) adds `WHISPER_MODEL` (transcriber model,
  separate from the classifier's `GROQ_MODEL`) and three quality-threshold
  knobs: `TRANSCRIPT_LOGPROB_FLOOR`, `TRANSCRIPT_NO_SPEECH_CEIL`,
  `TRANSCRIPT_COMPRESSION_RANGE`. Unset thresholds fall back to the module
  defaults documented in `docs/phase3-audio-intake-spec.md`.
- `docker-compose.yml` passes these through from the host environment with
  empty defaults: an unset variable arrives as an empty string, which keeps
  the app fail-closed (no key -> keyword mode / transcriber unavailable)
  instead of crashing compose interpolation.
- The live dev path today is bare uvicorn with the environment from the
  shell; compose is the container path. Compose parity exists so a future
  compose run cannot silently lose the Groq/audio configuration.

## Networking

- The app listens on port 8000 (override with the `PORT` env var).
- Opening the chosen port to the internet is an **AWS console step on Steph's
  side** (Lightsail instance firewall / networking rules). This runbook does not
  change firewall rules.

## Domain + auth before exposure

- The dashboard mutates data (status/route buttons). Basic auth goes on BEFORE the .xyz domain points here. Public citizen form: yes. Public mutation buttons: no.
- DNS (A record -> Lightsail IP) and TLS are console-side; if the existing proxy terminates TLS, reuse it rather than adding certs.

## Data and schema

- The SQLite database lives on the named volume `civic_db` mounted at `/data`
  (`DATABASE_PATH=/data/civic.db`).
- **Migrations:** additive, idempotent migrations live in `db/migrations/*.sql`
  and are applied by the loader in `app/db.py` at startup. Additive table
  migrations are safe on the live volume — no `docker compose down -v` needed.

## Operations

- Stop the stack with:
  ```sh
  docker compose down
  ```
- **Never** kill processes with `pkill` by pattern; it can match unrelated
  processes. Always manage the stack through `docker compose`.
- Compose hygiene on a shared box: stack commands only ever run inside `civic-issue-triage/`. Never `pkill` by pattern or stop containers project-blind - the other app shares this daemon.
