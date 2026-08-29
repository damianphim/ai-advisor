"""
Trust & safety: report submission + moderator queue.

damianphim/symbolos#161. Phase 1 (engineering backbone) shipped
persistence + the queue with only "dismiss" as a resolution path. Phase 2a
adds content removal. Deliberately NOT in this file yet — tracked as
explicit follow-up work, not silently dropped: proportionate user
restrictions (needs a decision about where suspension gets enforced —
see #161's PR discussion), the public copyright/takedown page, and all
frontend UI.

Two routers because they have different prefixes (/api/reports vs.
/api/moderation) and different audiences (any authenticated user vs.
moderators only) — see api/main.py for how both get registered.

Moderator auth: reuses ADMIN_USER_IDS/is_admin_user (the platform-wide
Symbolos admin list), NOT the separate ADMIN_SECRET/X-Cron-Secret password
session used by admin.py. That's a deliberate choice for Phase 1 (see the
PR description) — it's a normal Bearer-JWT check like every other
authenticated route, not a second login flow.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth import get_current_user_id
from ..utils.supabase_client import get_supabase
from ..utils.moderation import create_report, record_action, remove_content
from .clubs.permissions import is_admin_user

reports_router = APIRouter()
moderation_router = APIRouter()


def _require_moderator(current_user_id: str) -> None:
    if not is_admin_user(current_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Moderator access required")


# ── Report submission (any authenticated user) ─────────────────────────────

class ReportCreate(BaseModel):
    content_type: str
    content_id: str = Field(..., min_length=1, max_length=200)
    reason_category: str
    context: Optional[str] = Field(None, max_length=1000)


@reports_router.post("", status_code=status.HTTP_201_CREATED)
async def submit_report(
    body: ReportCreate,
    current_user_id: str = Depends(get_current_user_id),
):
    """Report any supported content type. See utils/moderation.py's
    CONTENT_TYPES for what's covered and create_report() for anti-abuse
    (rate limit, duplicate, self-report) enforcement."""
    result = create_report(
        reporter_id=current_user_id,
        content_type=body.content_type,
        content_id=body.content_id,
        reason_category=body.reason_category,
        context=body.context,
    )
    return {"message": "Report submitted", **result}


# ── Moderator queue (ADMIN_USER_IDS only) ───────────────────────────────────

class AssignRequest(BaseModel):
    assignee_id: Optional[str] = None  # None = assign to self


class NoteRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=2000)


class DismissRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class ResolveRequest(BaseModel):
    action: str  # "remove_content" — the only resolution action implemented so far
    reason: Optional[str] = Field(None, max_length=2000)


@moderation_router.get("/reports")
async def list_reports(
    status_filter: Optional[str] = None,
    content_type: Optional[str] = None,
    assigned_to: Optional[str] = None,
    current_user_id: str = Depends(get_current_user_id),
):
    _require_moderator(current_user_id)
    supabase = get_supabase()
    query = supabase.table("reports").select("*").order("created_at", desc=True)
    if status_filter:
        query = query.eq("status", status_filter)
    if content_type:
        query = query.eq("content_type", content_type)
    if assigned_to:
        query = query.eq("assigned_to", assigned_to)
    result = query.execute()
    return {"reports": result.data or [], "count": len(result.data or [])}


@moderation_router.get("/reports/{report_id}")
async def get_report(report_id: str, current_user_id: str = Depends(get_current_user_id)):
    _require_moderator(current_user_id)
    supabase = get_supabase()
    report = supabase.table("reports").select("*").eq("id", report_id).execute()
    if not report.data:
        raise HTTPException(status_code=404, detail="Report not found")
    actions = (
        supabase.table("moderation_actions")
        .select("*")
        .eq("report_id", report_id)
        .order("created_at", desc=False)
        .execute()
    )
    return {"report": report.data[0], "actions": actions.data or []}


def _get_report_or_404(report_id: str) -> dict:
    result = get_supabase().table("reports").select("*").eq("id", report_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Report not found")
    return result.data[0]


@moderation_router.post("/reports/{report_id}/assign")
async def assign_report(
    report_id: str,
    body: AssignRequest,
    req: Request,
    current_user_id: str = Depends(get_current_user_id),
):
    _require_moderator(current_user_id)
    _get_report_or_404(report_id)
    assignee_id = body.assignee_id or current_user_id
    if not is_admin_user(assignee_id):
        raise HTTPException(status_code=400, detail="assignee_id must be a moderator")

    supabase = get_supabase()
    supabase.table("reports").update({
        "assigned_to": assignee_id,
        "status": "in_review",
    }).eq("id", report_id).execute()
    record_action(
        report_id=report_id, moderator_id=current_user_id, action="assigned",
        details={"assignee_id": assignee_id},
    )
    return {"message": "Report assigned"}


@moderation_router.post("/reports/{report_id}/notes")
async def add_note(
    report_id: str,
    body: NoteRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    _require_moderator(current_user_id)
    _get_report_or_404(report_id)
    record_action(
        report_id=report_id, moderator_id=current_user_id, action="note_added",
        details={"note": body.note},
    )
    return {"message": "Note added"}


@moderation_router.post("/reports/{report_id}/dismiss")
async def dismiss_report(
    report_id: str,
    body: DismissRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    """Mark a report as unfounded — no violation, nothing done to the
    content. See resolve_report for the "yes, take action" path."""
    _require_moderator(current_user_id)
    report = _get_report_or_404(report_id)
    if report["status"] in ("dismissed", "resolved"):
        raise HTTPException(status_code=409, detail=f"Report is already {report['status']}")

    supabase = get_supabase()
    supabase.table("reports").update({"status": "dismissed"}).eq("id", report_id).execute()
    record_action(
        report_id=report_id, moderator_id=current_user_id, action="status_changed",
        details={"from": report["status"], "to": "dismissed", "reason": body.reason},
    )
    return {"message": "Report dismissed"}


@moderation_router.post("/reports/{report_id}/resolve")
async def resolve_report(
    report_id: str,
    body: ResolveRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Take an actual remediation action on the reported content, then mark
    the report resolved. Only 'remove_content' exists so far —
    proportionate user restrictions are separate follow-up work (#161),
    not bundled here since enforcing a restriction needs its own decision
    about where to check it (see api/utils/moderation.py's module
    docstring... once that lands).
    """
    _require_moderator(current_user_id)
    report = _get_report_or_404(report_id)
    if report["status"] in ("dismissed", "resolved"):
        raise HTTPException(status_code=409, detail=f"Report is already {report['status']}")

    if body.action != "remove_content":
        raise HTTPException(status_code=400, detail=f"Unsupported resolution action: {body.action}")

    remove_content(report["content_type"], report["content_id"])

    supabase = get_supabase()
    supabase.table("reports").update({"status": "resolved"}).eq("id", report_id).execute()
    record_action(
        report_id=report_id, moderator_id=current_user_id, action="content_removed",
        details={
            "content_type": report["content_type"],
            "content_id": report["content_id"],
            "reason": body.reason,
        },
    )
    return {"message": "Content removed, report resolved"}
