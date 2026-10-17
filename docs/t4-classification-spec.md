# T4 Classification: decision scoring spec

Status: approved by PM (Stephen) via Fo on 2026-10-17. Implementation is slot-held on the app-facing `GROQ_API_KEY` in the runtime env.

## Purpose

T4 fills category, urgency and confidence for every incoming report. It runs at intake (`POST /reports`) and writes the same fields the database already validates. Nothing downstream changes: routing still reads the category and picks the authority.

## Provider

Groq, `llama-3.1-8b-instant` to start. `GROQ_API_KEY` comes from the runtime env (never in the repo). `GROQ_MODEL` env override with that default.

## Decision-question format for urgency

Urgency is NOT a free-form score. The model answers a choice-type question with criteria strings (same pattern as the UCC decision head):

**Q: How urgent is this civic report?**
- **critical (5)**: immediate danger to life, health, property or safety; spreading harm; service completely down with people at risk.
- **high (4)**: essential service fully down for many people; unsafe conditions developing; needs action within days.
- **medium (3)**: degraded service or a recurring nuisance; affects some people; can wait for scheduled work.
- **low (1-2)**: cosmetic, minor, or already being handled; no safety impact.

The model picks exactly one choice. The chosen choice maps to the integer urgency stored in the DB (`CHECK 1-5`).

## Category

Output set: `roads`, `water`, `waste`, `public_services`, `other`. The fifth `other` was approved by the PM on 2026-10-17. `reports.category` CHECK constraint and `VALID_CATEGORIES` must gain `other`; schema changes need a fresh volume until a migration ticket exists (RUNBOOK).

## Confidence and fail-closed

- `confidence` in [0,1] returned by the model.
- Below `CLASSIFICATION_CONFIDENCE_FLOOR` (default 0.60, env override): `needs_review=1`, no auto routing.
- Model category differs from the citizen-chosen category: `needs_review=1` (disagreement flag, both values kept).
- `other` never auto-routes; it sits in the review queue.

## Degradation path

No key or model failure: keyword rules (English seeds plus Luganda and Swahili keywords) set the category tentatively and always `needs_review=1`. The demo still functions. T4 failure must never block intake.

## Tests

The existing 11 tests keep passing (contract unchanged). New tests use mocked model responses: happy path, low confidence, disagreement, keyword fallback, and `other` never routed.

## Out of scope (post-hackathon)

Fine-tuned decision head (Sunflower-Gemma4-E2B) trained on review-queue corrections; embedding-based duplicate clustering.
