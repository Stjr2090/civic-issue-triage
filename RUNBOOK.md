# Runbook: Deploy on Lightsail

Deployment target is an AWS Lightsail instance. The image is always **built on
Lightsail itself** — never build locally and push, to avoid cross-architecture
image problems (e.g. an arm64 laptop image failing on the x86 instance).

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

## Networking

- The app listens on port 8000 (override with the `PORT` env var).
- Opening the chosen port to the internet is an **AWS console step on Steph's
  side** (Lightsail instance firewall / networking rules). This runbook does not
  change firewall rules.

## Data and schema

- The SQLite database lives on the named volume `civic_db` mounted at `/data`
  (`DATABASE_PATH=/data/civic.db`).
- **Fresh-DB assumption:** the volume persists across redeploys, so data is
  kept — but there is no migration mechanism yet. A schema change therefore
  needs a fresh volume (`docker compose down -v` before `up`) until a migration
  ticket exists.

## Operations

- Stop the stack with:
  ```sh
  docker compose down
  ```
- **Never** kill processes with `pkill` by pattern; it can match unrelated
  processes. Always manage the stack through `docker compose`.
