# Runbook: Deploy on Lightsail

Deployment target is an AWS Lightsail instance. The image is always **built on
Lightsail itself** — never build locally and push, to avoid cross-architecture
image problems (e.g. an arm64 laptop image failing on the x86 instance).

## Shared box (Lightsail already runs another app)

Discover before binding:
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
- Compose hygiene on a shared box: stack commands only ever run inside `civic-issue-triage/`. Never `pkill` by pattern or stop containers project-blind - the other app shares this daemon.
