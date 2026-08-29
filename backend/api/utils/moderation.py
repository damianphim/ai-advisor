"""
Trust & safety: shared report-creation logic used by every content surface.

damianphim/symbolos#161, Phase 1. Centralizing this (rather than each of
forum.py/clubs/*.py rolling its own) is what makes "prevent spam, duplicate
abuse, self-reporting" apply consistently everywhere instead of needing to
be re-implemented, and re-audited, per content type.

reports/moderation_actions have no client-facing RLS policies at all (see
the migration) — every read/write here uses the service-role client.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status

from .supabase_client import get_supabase

logger = logging.getLogger(__name__)

REASON_CATEGORIES = frozenset({
    "spam", "harassment", "hate_speech", "misinformation",
    "inappropriate_content", "impersonation", "other",
})

# Free-text context from the reporter — capped so a report can't be used to
# smuggle an arbitrarily large payload into the DB.
MAX_CONTEXT_LENGTH = 1000

_REPORT_RATE_LIMIT_RPM = 10


@dataclass(frozen=True)
class ContentTypeSpec:
    table: str
    owner_column: Optional[str]   # None for content_type == "user" (self-owned)
    snapshot_columns: tuple[str, ...]


# One place that knows how to look up, own-check, and snapshot each
# reportable content type. Adding a new reportable surface later means
# adding one entry here, not touching create_report() itself.
CONTENT_TYPES: dict[str, ContentTypeSpec] = {
    "forum_post": ContentTypeSpec(
        table="forum_posts", owner_column="user_id",
        snapshot_columns=("id", "user_id", "author", "title", "body", "category"),
    ),
    "forum_reply": ContentTypeSpec(
        table="forum_replies", owner_column="user_id",
        snapshot_columns=("id", "user_id", "author", "body"),
    ),
    "club": ContentTypeSpec(
        table="clubs", owner_column="created_by",
        snapshot_columns=("id", "created_by", "name", "description"),
    ),
    "club_event": ContentTypeSpec(
        table="club_events", owner_column="created_by",
        snapshot_columns=("id", "created_by", "title", "description"),
    ),
    "club_announcement": ContentTypeSpec(
        table="club_announcements", owner_column="created_by",
        snapshot_columns=("id", "created_by", "title", "body"),
    ),
    "user": ContentTypeSpec(
        table="users", owner_column=None,
        snapshot_columns=("id", "email", "username"),
    ),
}


def _check_rate_limit(reporter_id: str) -> None:
    """At most 10 reports/minute per reporter, across all content types.
    Fails open if the limiter is unavailable — a report queue being briefly
    unthrottled is a far smaller risk than losing legitimate reports during
    a rate-limiter outage."""
    try:
        from ..main import _limiter
        if not _limiter.is_allowed(f"report:{reporter_id}", rpm=_REPORT_RATE_LIMIT_RPM):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many reports submitted. Please wait a moment before reporting again.",
            )
    except HTTPException:
        raise
    except Exception:
        pass


def create_report(
    *,
    reporter_id: str,
    content_type: str,
    content_id: str,
    reason_category: str,
    context: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist a report. Raises HTTPException for every rejection case so
    route handlers can just call this and let FastAPI convert it.

    Returns the inserted report row (id, status, created_at — no reporter
    identity beyond what the caller already has).
    """
    if content_type not in CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown content_type: {content_type}")
    if reason_category not in REASON_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown reason_category: {reason_category}")
    if context and len(context) > MAX_CONTEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"context must be at most {MAX_CONTEXT_LENGTH} characters")

    _check_rate_limit(reporter_id)

    spec = CONTENT_TYPES[content_type]
    supabase = get_supabase()

    # For content_type == "user" the "owner" of the content IS the
    # content_id itself, checked up front before even looking the row up.
    if spec.owner_column is None and content_id == reporter_id:
        raise HTTPException(status_code=400, detail="You cannot report yourself")

    owner_row = supabase.table(spec.table).select(",".join(spec.snapshot_columns)) \
        .eq("id", content_id).execute()
    if not owner_row.data:
        raise HTTPException(status_code=404, detail="Reported content not found")
    content_row = owner_row.data[0]

    if spec.owner_column is not None and content_row.get(spec.owner_column) == reporter_id:
        raise HTTPException(status_code=400, detail="You cannot report your own content")

    # Duplicate-report check — the DB UNIQUE constraint is the real
    # enforcement (races are possible between this check and the insert),
    # this is just for a clean 409 instead of a raw constraint-violation
    # 500 in the common case.
    existing = (
        supabase.table("reports")
        .select("id")
        .eq("reporter_id", reporter_id)
        .eq("content_type", content_type)
        .eq("content_id", content_id)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="You have already reported this")

    now = datetime.now(timezone.utc).isoformat()
    try:
        result = supabase.table("reports").insert({
            "reporter_id": reporter_id,
            "content_type": content_type,
            "content_id": content_id,
            "reason_category": reason_category,
            "context": (context or "").strip() or None,
            "content_snapshot": content_row,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }).execute()
    except Exception as e:
        # The UNIQUE constraint is the race-safe backstop for the existence
        # check above — a concurrent duplicate lands here, not as a 500.
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="You have already reported this")
        logger.exception("Failed to persist report: %s", e)
        raise HTTPException(status_code=500, detail="Failed to submit report")

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to submit report")

    row = result.data[0]
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}


def remove_content(content_type: str, content_id: str) -> None:
    """
    Delete the reported row. content_type == "user" is deliberately
    rejected — removing a user account is not a "content removal" action,
    it needs its own proportionate-restriction mechanism (suspension),
    which is separate follow-up work, not this function's job.
    """
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"Unknown content_type: {content_type}")
    if spec.owner_column is None:
        raise HTTPException(status_code=400, detail="content_type 'user' cannot be removed as content")

    get_supabase().table(spec.table).delete().eq("id", content_id).execute()


def record_action(*, report_id: str, moderator_id: str, action: str, details: Optional[dict] = None) -> None:
    """Append one row to moderation_actions. Best-effort is NOT acceptable
    here (unlike audit.log_access) — moderator actions must not silently
    vanish, so a failure here propagates rather than being swallowed."""
    get_supabase().table("moderation_actions").insert({
        "report_id": report_id,
        "moderator_id": moderator_id,
        "action": action,
        "details": details or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
