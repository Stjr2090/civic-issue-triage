# Phase 2: admin case-detail view (spec draft)

Status: signed off by build lead 2026-10-18 09:47 UTC, as written; PM (approval party) retains overrule authority. Prepared by the review/design role from main `575dae9`; re-grounded the same morning against `df36f2f` (MIT license commit, LICENSE only — zero file overlap, spec unchanged).
Gate: the Phase 1 attachments migration must merge before this branch's PR opens.
Nothing in this phase starts until that gate clears.

## Rulings binding this spec (settled 2026-10-18)

- **Auth.** The case-detail endpoint ships explicitly flagged unauthenticated-for-now.
  Enforcement lands with the future admin-auth ticket. No new auth surface before the
  18:00 UTC freeze on 2026-10-18.
- **Schema.** This phase is read-only on `app/db.py`. The module is single-writer
  `executescript`; one schema-writing branch at a time, and that branch is Phase 1.

## Purpose

Click-to-open case detail for admins from the dashboard: full report record, AI
analysis, routing history, and next routing options. The dashboard today
(`static/dashboard.html`) is list-only: filterable table, route buttons, stat cards,
30s auto-refresh. There is no detail view.

## API: `GET /api/reports/{report_id}/detail`

New route in `app/routers/reports.py` (same module, same `get_conn` pattern).
On success, 200 with four sections:

- **`report`**: the full row, identical field set to `GET /api/reports/{report_id}`.
  No new columns; no schema reads beyond existing tables (attachment reads are added
  on the gated branch, from Phase 1's merged schema).
- **`analysis`**: `{state, confidence, reason, urgency, needs_review}`. The state
  rule is exact and testable:
  - `confidence IS NULL OR reason IS NULL` → state is the defined string
    `"not classified (pre-T4 report)"`. This is a rendered state, never an error,
    never a 404, never a 5xx. All pre-T4 rows (legacy data written before the
    classification module existed) hit this branch.
  - otherwise → state `"classified"`. Degraded keyword-mode rows count as
    classified (the module wrote them); `needs_review` remains its own field.
- **`routing_history`**: rows from `audit_log WHERE report_id = ?` ordered by
  `created_at, id` (id breaks same-second ties). Known actions render with their
  `detail`: `classified` (parsed JSON detail), `routed` (authority name),
  `status_change` (status string). An empty list is a valid, defined render —
  nothing writes `routed` actions until the route endpoint is used for that report.
  It must not look like an error.
- **`next_options`**: authorities matching `category = report.category AND active = 1`,
  ordered `priority DESC`. An empty list is a defined render: "no active authority
  for category". This is mandatory for `category = 'other'`: the `authorities`
  CHECK excludes `other` by design (T9), so those reports always show zero options —
  correct behavior, mirrored from the 409 in `GET /route/{report_id}`. The detail
  view must present it as a state, not an error.

Error contract: unknown id → 404 (matches existing `get_report` convention).
Every other existing report id returns 200.

## Frontend: `static/dashboard.html`

- Clicking a row (excluding the existing Route button — stop propagation) opens a
  client-side detail panel/modal fed by the new endpoint. No new JS dependencies;
  the dashboard is dependency-free vanilla JS today and stays that way.
- Render rules, each a defined state:
  - pre-T4 row → exact string `not classified (pre-T4 report)`;
  - report with no attachment → `no attachment` (never an error; wire-in happens on
    the gated branch against Phase 1's merged schema, default until then);
  - empty history → `no routing history yet`;
  - empty options → `no active authority for category` (route action disabled in
    this state, so the UI never fires a guaranteed 409).
- Route action in the detail panel reuses the existing `GET /api/route/{report_id}`;
  no new mutating call is introduced. The existing table rule (button on
  `status = 'new'` only) carries over to the panel.
- Allowed collateral fix inside this file only: the `CATS` label map gains `other`
  (currently falls through to the raw string).

## Tests

Follow the existing pattern in `tests/test_reports.py` (tempdir DB swap + TestClient):

1. Happy path on a classified row: 200, `analysis.state == "classified"`, history
   contains the `classified` entry written at intake.
2. Pre-T4 probe: raw-INSERT a row with `confidence = NULL, reason = NULL`; detail is
   200 and `analysis.state == "not classified (pre-T4 report)"` (exact string
   equality). Assert never 4xx/5xx beyond the 200.
3. No-attachment probe: 200 with the defined `no attachment` state (write on the
   gated branch once Phase 1's schema exists; until then every row returns the
   default state).
4. Empty history: a fresh report.id with only the intake row → `routing_history == []`.
5. `other` row: `next_options == []`.
6. Unknown id → 404.
7. After `PATCH /reports/{id}/status`: history shows the `status_change` entry.

## Out of scope

- Any schema or migration change (Phase 1 owns these).
- Admin auth enforcement (future ticket; this endpoint is flagged for it).
- Changes to route/status mutation logic (note: `GET /route/{report_id}` currently
  re-routes regardless of status — documented here, not fixed here).
- Server-side rendering or new frontend/JS dependencies.

## Acceptance criteria

- `GET /api/reports/{report_id}/detail` returns 200 with all four sections for every
  existing report id; pre-T4 and no-attachment rows render their defined states and
  never error (probes 2 and 3 pass).
- Dashboard rows open the detail panel on click; all four defined states render as
  specified; route action reuses the existing endpoint and disables on empty options.
- Full pytest suite green: existing tests unchanged and passing, new probes added.
- **Done = merged to main and testable on the admin dashboard at `/dashboard`.**
