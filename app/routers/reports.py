"""Reports API: CRUD + routing."""
import json
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import classifier
from app.db import DB_PATH

router = APIRouter()

VALID_CATEGORIES = ("roads", "water", "waste", "public_services", "other")
VALID_SOURCES = ("web", "telegram", "api")
VALID_STATUSES = ("new", "routed", "in_progress", "resolved", "closed")


class ReportIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=4000)
    category: str
    reporter: str | None = None
    location: str | None = None
    source: str = "web"  # web | telegram | api (telegram path per design)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@router.post("/reports", status_code=201)
def create_report(payload: ReportIn):
    if payload.category not in VALID_CATEGORIES:
        raise HTTPException(422, f"category must be one of {VALID_CATEGORIES}")
    if payload.source not in VALID_SOURCES:
        raise HTTPException(422, f"source must be one of {VALID_SOURCES}")
    # T4: classify after basic validation. Defensive: classification failure
    # must never block intake.
    try:
        result = classifier.classify(payload.title, payload.description)
    except Exception:
        result = classifier.keyword_classify(payload.title, payload.description)

    # T9 rule stays binding: citizen category "other" always goes to review and
    # never auto-routes. The citizen-submitted category is never overwritten
    # (reconciliation #4); the AI suggestion is kept only in the reason/audit.
    needs_review = 1 if payload.category == "other" else 0
    if result["mode"] == "keyword":
        needs_review = 1
    if result["confidence"] < classifier.confidence_floor():
        needs_review = 1
    if result["suggested_category"] != payload.category:
        needs_review = 1

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO reports (title, description, category, reporter, location,"
            " source, urgency, confidence, reason, needs_review)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (payload.title, payload.description, payload.category,
             payload.reporter, payload.location, payload.source,
             result["urgency"], result["confidence"], result["reason"],
             needs_review),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO audit_log (report_id, action, detail) VALUES (?,?,?)",
            (rid, "classified", json.dumps({
                "mode": result["mode"],
                "suggested_category": result["suggested_category"],
                "confidence": result["confidence"],
                "urgency": result["urgency"],
                "needs_review": bool(needs_review),
            })),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.get("/reports")
def list_reports(
    status: str | None = None,
    category: str | None = None,
    source: str | None = None,
    needs_review: bool | None = None,
    limit: int = Query(50, le=200),
):
    q = "SELECT * FROM reports WHERE 1=1"
    params: list = []
    if status:
        q += " AND status=?"
        params.append(status)
    if category:
        q += " AND category=?"
        params.append(category)
    if source:
        q += " AND source=?"
        params.append(source)
    if needs_review is not None:
        q += " AND needs_review=?"
        params.append(1 if needs_review else 0)
    q += " ORDER BY urgency DESC, created_at DESC LIMIT ?"
    params.append(limit)
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


@router.get("/reports/{report_id}")
def get_report(report_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(404, "report not found")
        return dict(row)
    finally:
        conn.close()


@router.patch("/reports/{report_id}/status")
def update_status(report_id: int, status: str = Query(...)):
    if status not in VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {VALID_STATUSES}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE reports SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, report_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "report not found")
        conn.execute(
            "INSERT INTO audit_log (report_id, action, detail) VALUES (?,?,?)",
            (report_id, "status_change", status),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.get("/stats")
def stats():
    """Aggregates for the dashboard: counts by status and category."""
    conn = get_conn()
    try:
        by_status = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM reports GROUP BY status"
            ).fetchall()
        )
        by_category = dict(
            conn.execute(
                "SELECT category, COUNT(*) FROM reports GROUP BY category"
            ).fetchall()
        )
        total = sum(by_status.values())
        urgent_open = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE urgency >= 4"
            " AND status NOT IN ('resolved','closed')"
        ).fetchone()[0]
        return {
            "total": total,
            "by_status": by_status,
            "by_category": by_category,
            "urgent_open": urgent_open,
        }
    finally:
        conn.close()


@router.get("/authorities")
def list_authorities(
    active: bool | None = None,
    category: str | None = None,
):
    """List the routing table, ordered by category then priority DESC."""
    q = "SELECT * FROM authorities WHERE 1=1"
    params: list = []
    if active is not None:
        q += " AND active=?"
        params.append(1 if active else 0)
    if category:
        q += " AND category=?"
        params.append(category)
    q += " ORDER BY category, priority DESC"
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


@router.get("/route/{report_id}")
def route_report(report_id: int):
    """Route a report to the active authority for its category."""
    conn = get_conn()
    try:
        report = conn.execute(
            "SELECT * FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if not report:
            raise HTTPException(404, "report not found")
        authority = conn.execute(
            "SELECT * FROM authorities WHERE category=? AND active=1"
            " ORDER BY priority DESC LIMIT 1",
            (report["category"],),
        ).fetchone()
        if not authority:
            raise HTTPException(409, "no active authority for category")
        conn.execute(
            "UPDATE reports SET status='routed', updated_at=datetime('now')"
            " WHERE id=?",
            (report_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (report_id, action, detail) VALUES (?,?,?)",
            (report_id, "routed", authority["name"]),
        )
        conn.commit()
        return {"report_id": report_id, "routed_to": dict(authority)}
    finally:
        conn.close()
