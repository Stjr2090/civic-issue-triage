"""Reports API: CRUD + routing."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import DB_PATH
import sqlite3

router = APIRouter()

VALID_CATEGORIES = ("roads", "water", "waste", "public_services")
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
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO reports (title, description, category, reporter, location, source)"
            " VALUES (?,?,?,?,?,?)",
            (payload.title, payload.description, payload.category,
             payload.reporter, payload.location, payload.source),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.get("/reports")
def list_reports(
    status: str | None = None,
    category: str | None = None,
    source: str | None = None,
    needs_review: int | None = None,
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
