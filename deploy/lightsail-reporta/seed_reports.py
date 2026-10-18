#!/usr/bin/env python3
"""Seed synthetic sample reports through the live HTTP write path.

Standalone by design: this module imports ``httpx`` only. It never imports
``app.*``, never calls the classifier, never reads ``GROQ_API_KEY``, never
touches SQLite, and never fabricates ``mode``/``confidence``/``urgency``
fields. Every value the dashboard later shows -- classification, urgency,
``needs_review``, routing history and ``next_options`` -- is produced by the
server in response to the HTTP calls made here.

Usage::

    REPORTA_BASE_URL=http://127.0.0.1:8000 \\
        python deploy/lightsail-reporta/seed_reports.py

``--base-url`` overrides the ``REPORTA_BASE_URL`` environment variable, which
in turn overrides the default ``http://127.0.0.1:8000``.

The run is idempotent: reports whose ``[sample]``-prefixed title already exists
are skipped, so a second run creates nothing.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SAMPLE_PREFIX = "[sample]"
# Server list endpoint caps ``limit`` at 200; more than enough for demo data.
LIST_LIMIT = 200
DEFAULT_TIMEOUT = 10.0

# The seven demo reports. ``status`` is the final lifecycle state to set via the
# status endpoint; ``None`` means the report is routed instead (the route call
# itself sets status="routed"). The single "other" report is never routed: per
# the T9 rule it lands in the review queue.
REPORTS = [
    {
        # Routed: one of the authority-backed reports, audited via GET /api/route.
        "title": "[sample] Deep pothole at Kampala Road junction",
        "description": (
            "A deep pothole has opened up at the Kampala Road junction near the "
            "old taxi park. Two boda-boda riders have already been thrown off and "
            "the hole fills with rainwater, so its real depth is hidden. It keeps "
            "getting wider every week."
        ),
        "category": "roads",
        "reporter": "Aisha Nakato",
        "location": "Kampala Road junction, Central Division",
        "status": None,
        "route": True,
    },
    {
        "title": "[sample] Collapsed culvert floods Ntinda road",
        "description": (
            "The culvert crossing the drainage channel on Ntinda Road has "
            "collapsed at one end. Whenever it rains the whole lane floods and "
            "matatus cannot pass. Pedestrians are forced to wade through the "
            "water to reach the stage."
        ),
        "category": "roads",
        "reporter": "Brian Okello",
        "location": "Ntinda Road, Nakawa Division",
        "status": "new",
        "route": False,
    },
    {
        "title": "[sample] Burst main flooding Kira Road",
        "description": (
            "A water main burst on Kira Road two days ago and clean water is "
            "pouring down the street. The pressure has dropped to nothing in the "
            "nearby cells and the taps have been dry since the weekend."
        ),
        "category": "water",
        "reporter": "Grace Atim",
        "location": "Kira Road, Kamwokya",
        "status": "in_progress",
        "route": False,
    },
    {
        "title": "[sample] No piped water in Bwaise for a week",
        "description": (
            "The taps in Bwaise Zone 3 have had no piped water for over a week. "
            "Residents are buying jerricans from vendors at a high price. The "
            "local tank was said to be under repair but nobody has fixed it."
        ),
        "category": "water",
        "reporter": "Moses Kigozi",
        "location": "Bwaise Zone 3, Kawempe Division",
        "status": "resolved",
        "route": False,
    },
    {
        "title": "[sample] Overflowing skip not collected in Kireka",
        "description": (
            "The communal waste skip on Kireka main street has not been collected "
            "for three weeks. Rubbish is spilling onto the road and the smell is "
            "terrible. Stray dogs are scattering the garbage across the lane."
        ),
        "category": "waste",
        "reporter": "Sarah Nabirye",
        "location": "Kireka main street, Wakiso",
        "status": "new",
        "route": False,
    },
    {
        "title": "[sample] Street lights out on Entebbe Road stretch",
        "description": (
            "All the street lights along the Entebbe Road stretch near the market "
            "have been out for a month. The area is pitch dark at night and "
            "traders close early because it no longer feels safe to stay."
        ),
        "category": "public_services",
        "reporter": "Peter Ssemakula",
        "location": "Entebbe Road, near Nakasero Market",
        "status": "new",
        "route": False,
    },
    {
        # Catch-all "other": never routed, lands in the review queue (T9 rule).
        "title": "[sample] Stray goats damaging market stalls",
        "description": (
            "A herd of stray goats keeps wandering through the market and "
            "knocking over the produce stalls. Nobody seems to know which office "
            "is responsible for rounding them up."
        ),
        "category": "other",
        "reporter": "Halima Nansubuga",
        "location": "Nakasero Market, Central Division",
        "status": "new",
        "route": False,
    },
]


def _existing_titles(client, base_url: str) -> set:
    """Titles already present server-side (used for idempotent skips)."""
    response = client.get(f"{base_url}/api/reports", params={"limit": LIST_LIMIT})
    response.raise_for_status()
    return {row.get("title") for row in response.json()}


def _seed(base_url: str, client) -> dict:
    base = base_url.rstrip("/")
    existing = _existing_titles(client, base)

    created = 0
    skipped = 0
    routed = 0
    created_ids = []

    for spec in REPORTS:
        if spec["title"] in existing:
            skipped += 1
            continue

        response = client.post(
            f"{base}/api/reports",
            json={
                "title": spec["title"],
                "description": spec["description"],
                "category": spec["category"],
                "reporter": spec["reporter"],
                "location": spec["location"],
                "source": "web",
            },
        )
        response.raise_for_status()
        report = response.json()
        report_id = report["id"]
        created += 1
        created_ids.append(report_id)

        if spec.get("route"):
            route_response = client.get(f"{base}/api/route/{report_id}")
            route_response.raise_for_status()
            routed += 1
        elif spec.get("status"):
            status_response = client.patch(
                f"{base}/api/reports/{report_id}/status",
                params={"status": spec["status"]},
            )
            status_response.raise_for_status()

    return {
        "created": created,
        "skipped": skipped,
        "routed": routed,
        "created_ids": created_ids,
    }


def seed(base_url: str = DEFAULT_BASE_URL, client=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Create the demo reports through the live HTTP API.

    ``client`` may be any httpx-compatible client (e.g. a ``TestClient``) so the
    module can be exercised without a live server. When omitted, a real
    ``httpx.Client`` is created and closed here.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        return _seed(base_url, client)
    finally:
        if owns_client:
            client.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("REPORTA_BASE_URL", DEFAULT_BASE_URL),
        help="Base URL of the running Reporta API (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    summary = seed(args.base_url)
    print(
        f"seeded: created={summary['created']} skipped={summary['skipped']} "
        f"routed={summary['routed']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
