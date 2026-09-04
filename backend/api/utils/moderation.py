"""
Trust & safety: shared report-creation logic used by every content surface.

damianphim/symbolos#161, Phase 1. Centralizing this (rather than each of
forum.py/clubs/*.py rolling its own) is what makes "prevent spam, duplicate
abuse, self-reporting" apply consistently everywhere instead of needing to
be re-implemented, and re-audited, per content type.

reports/moderation_actions have no client-facing RLS policies at all (see
the migration) — every read/write here uses the service-role client.

Phase 2b adds suspend_user()/require_not_suspended() — a "proportionate
user restriction" is deliberately NOT enforced by adding a check to
get_current_user_id (api/auth.py), which runs on every single authenticated
request in the app and already makes one Supabase call for JWT
verification; a second DB read there would roughly double network calls
app-wide just to catch the rare suspended user. A suspension is meant to
block posting, not reading, so it's checked only at specific
content-creation routes (require_not_suspended's own docstring lists
them) — matching this codebase's existing pattern of inline per-route
checks (is_email_verified, check_and_record_llm_usage) over blanket
middleware.
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
    rejected — removing a user account is not a "content removal" action;
    see suspend_user() for the proportionate-restriction path used instead.
    """
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"Unknown content_type: {content_type}")
    if spec.owner_column is None:
        raise HTTPException(status_code=400, detail="content_type 'user' cannot be removed as content")

    get_supabase().table(spec.table).delete().eq("id", content_id).execute()


def suspend_user(user_id: str, reason: Optional[str] = None) -> None:
    """Proportionate restriction for a report about a user account
    (content_type == "user"). Blocks posting via require_not_suspended()
    at specific content-creation endpoints — deliberately NOT a full
    account lockout; see the module docstring for why."""
    get_supabase().table("users").update({
        "is_suspended": True,
        "suspended_at": datetime.now(timezone.utc).isoformat(),
        "suspended_reason": reason,
    }).eq("id", user_id).execute()


def is_user_suspended(user_id: str) -> bool:
    result = get_supabase().table("users").select("is_suspended").eq("id", user_id).execute()
    return bool(result.data and result.data[0].get("is_suspended"))


def require_not_suspended(user_id: str) -> None:
    """Call at the top of any content-creation route (forum post/reply,
    club submission, club event/announcement creation) — the specific
    action a suspension is meant to block, not every authenticated request.
    See the module docstring for why this isn't a check in
    get_current_user_id instead."""
    if is_user_suspended(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_suspended", "message": "Your account has been suspended."},
        )


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
