"""
backend/api/routes/forum.py

Community forum endpoints — posts, replies, and per-user likes.

All endpoints are auth-protected via get_current_user_id().
Likes use a separate junction table so a user cannot like the same
post/reply twice (enforced at the DB level by a PRIMARY KEY constraint).

Endpoints:
  GET    /api/forum/posts                   – list posts (filter, sort, paginate)
  POST   /api/forum/posts                   – create a post
  DELETE /api/forum/posts/{post_id}         – delete own post
  GET    /api/forum/posts/{post_id}/replies – list replies for a post
  POST   /api/forum/posts/{post_id}/replies – add a reply
  DELETE /api/forum/replies/{reply_id}      – delete own reply
  POST   /api/forum/posts/{post_id}/like    – toggle like on a post
  POST   /api/forum/replies/{reply_id}/like – toggle like on a reply
"""

from fastapi import APIRouter, HTTPException, status, Depends, Request, Query
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
import logging
from html import escape

from ..utils.supabase_client import get_supabase, with_retry
from ..auth import get_current_user_id, get_user_db, require_mcgill_email
from ..exceptions import DatabaseException
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

# New top-level sections after the 2026-04 forum redesign.
# Legacy categories kept for backwards compatibility with existing posts.
VALID_CATEGORIES = {
    # Reviews — "review" is the unified course+optional-professor review type
    # (2026-07). course_review/professor_review are kept read-only for
    # existing posts created before the merge.
    "review", "course_review", "professor_review",
    # Other sections
    "clubs", "general", "app_feedback",
    # Legacy
    "courses", "study", "advice", "planning",
}
REVIEW_CATEGORIES = {"review", "course_review", "professor_review"}
REVIEW_TARGET_TYPES = {"course", "professor"}
VALID_SORTS = {"hot", "new", "top"}

# ── Semester-aware ranking (replaces the old like_count*2 + 48h-decay "hot"
# score) ─────────────────────────────────────────────────────────────────
# McGill term windows, padded to hold year-over-year without an annual data
# update — mirrors frontend/src/lib/termDates.js so both agree on where a
# semester starts/ends:
#   Fall:   Aug 25 – Dec 31      Winter: Jan 1 – Apr 30      Summer: May 1 – Aug 24
_TERM_END_MONTH_DAY = {"Winter": (4, 30), "Summer": (8, 24), "Fall": (12, 31)}
# A post whose semester has ended keeps its full lifetime like_count mixed
# in with live posts as long as it's still gaining at least this many likes
# per day, averaged over the last 14 days — i.e. still getting a like every
# couple of days months later. Otherwise it ranks by upvotes-since-semester-end
# only. Conservative starting point; recalibrate once there's real like volume.
_VELOCITY_KEEP_THRESHOLD = 0.5


# ── Pydantic models ────────────────────────────────────────────────────────────

class PostCreate(BaseModel):
    author:       str  = Field(..., min_length=1, max_length=80)
    avatar_color: str  = Field("#ed1b2f", max_length=20)
    category:     str  = Field("general", max_length=30)
    title:        str  = Field(..., min_length=1, max_length=120)
    body:         str  = Field(..., min_length=1, max_length=5000)
    tags:         List[str] = Field(default_factory=list)
    program_info: Optional[str] = Field(None, max_length=200)

    # Review-only fields. Required when category ∈ REVIEW_CATEGORIES.
    rating:              Optional[int] = Field(None, ge=1, le=5)
    difficulty_rating:   Optional[int] = Field(None, ge=1, le=5)
    review_target_type:  Optional[str] = Field(None, max_length=20)
    review_target_value: Optional[str] = Field(None, max_length=120)
    # Unified "review" category only: the course is review_target_value,
    # the professor (if the reviewer named one) is separate and optional.
    professor_name:      Optional[str] = Field(None, max_length=120)
    # Derived, not client-supplied: subject prefix (e.g. "COMP") extracted
    # from review_target_value for course reviews, powers the subject filter.
    subject:             Optional[str] = Field(None, max_length=10)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {VALID_CATEGORIES}")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: List[str]) -> List[str]:
        cleaned = [t.strip()[:50] for t in v if t.strip()]
        return cleaned[:8]   # max 8 tags

    @field_validator("review_target_type")
    @classmethod
    def validate_target_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if v not in REVIEW_TARGET_TYPES:
            raise ValueError(f"review_target_type must be one of {REVIEW_TARGET_TYPES}")
        return v

    def enforce_review_consistency(self) -> None:
        """Ensure review posts have required review fields, and non-reviews don't."""
        if self.category == "review":
            # Unified review type: always a course review, professor optional.
            if self.rating is None:
                raise ValueError("rating (1–5) is required for review posts")
            if self.difficulty_rating is None:
                raise ValueError("difficulty_rating (1–5) is required for review posts")
            if not self.review_target_value:
                raise ValueError("review_target_value (course code) is required for review posts")
            self.review_target_type = "course"
            self.professor_name = (self.professor_name or "").strip()[:120] or None
            import re
            m = re.match(r"^([A-Za-z]+)", self.review_target_value)
            self.subject = m.group(1).upper() if m else None
        elif self.category in REVIEW_CATEGORIES:
            # Legacy course_review/professor_review — kept read/write-compatible
            # for any client still on the old shape, but no longer created by
            # the current UI.
            if self.rating is None:
                raise ValueError("rating (1–5) is required for review posts")
            if not self.review_target_value:
                raise ValueError("review_target_value is required for review posts")
            if not self.review_target_type:
                self.review_target_type = (
                    "course" if self.category == "course_review" else "professor"
                )
            self.difficulty_rating = None
            self.professor_name = None
            if self.review_target_type == "course":
                import re
                m = re.match(r"^([A-Za-z]+)", self.review_target_value)
                self.subject = m.group(1).upper() if m else None
            else:
                self.subject = None
        else:
            # Clear review fields on non-review posts for data cleanliness
            self.rating = None
            self.difficulty_rating = None
            self.review_target_type = None
            self.review_target_value = None
            self.professor_name = None
            self.subject = None


class ReplyCreate(BaseModel):
    author:       str = Field(..., min_length=1, max_length=80)
    avatar_color: str = Field("#ed1b2f", max_length=20)
    body:         str = Field(..., min_length=1, max_length=2000)
    program_info: Optional[str] = Field(None, max_length=200)


# ── Helper: fetch liked IDs for a user ────────────────────────────────────────

def _get_liked_post_ids(supabase, user_id: str, post_ids: List[str]) -> set:
    if not post_ids:
        return set()
    try:
        res = (
            supabase.table("forum_post_likes")
            .select("post_id")
            .eq("user_id", user_id)
            .in_("post_id", post_ids)
            .execute()
        )
        return {r["post_id"] for r in (res.data or [])}
    except Exception:
        return set()


def _get_liked_reply_ids(supabase, user_id: str, reply_ids: List[str]) -> set:
    if not reply_ids:
        return set()
    try:
        res = (
            supabase.table("forum_reply_likes")
            .select("reply_id")
            .eq("user_id", user_id)
            .in_("reply_id", reply_ids)
            .execute()
        )
        return {r["reply_id"] for r in (res.data or [])}
    except Exception:
        return set()


# ── Semester-aware ranking helpers ─────────────────────────────────────────────

def _get_active_term(dt) -> tuple:
    """(term, year) for a given datetime — mirrors termDates.js getActiveTerm()."""
    m, d, y = dt.month, dt.day, dt.year
    if m <= 4:
        return ("Winter", y)
    if m < 8 or (m == 8 and d < 25):
        return ("Summer", y)
    return ("Fall", y)


def _term_end_date(term: str, year: int):
    from datetime import datetime, timezone
    month, day = _TERM_END_MONTH_DAY[term]
    return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)


def _get_post_like_timestamps(supabase, post_ids: List[str]) -> dict:
    """{post_id: [like created_at datetimes]} for the given posts."""
    if not post_ids:
        return {}
    from datetime import datetime
    try:
        rows = (
            supabase.table("forum_post_likes")
            .select("post_id, created_at")
            .in_("post_id", post_ids)
            .execute().data or []
        )
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        out.setdefault(r["post_id"], []).append(ts)
    return out


def _semester_aware_score(post: dict, like_timestamps: List, now) -> float:
    """
    Ranks posts made during the current semester by raw upvotes (like the
    old "Top"). Once a post's semester has ended, it ranks by upvotes
    earned SINCE the semester ended — UNLESS it's still gaining upvotes at
    a healthy clip (>= _VELOCITY_KEEP_THRESHOLD/day over the trailing 14
    days), in which case it keeps its full lifetime like_count so a review
    that's still actively useful doesn't get buried just because the
    semester it was written in is over.
    """
    from datetime import datetime, timedelta
    like_count = post.get("like_count") or 0
    try:
        created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    except Exception:
        return float(like_count)

    term, year = _get_active_term(created)
    semester_end = _term_end_date(term, year)
    if now <= semester_end:
        return float(like_count)

    likes_since_end = sum(1 for ts in like_timestamps if ts > semester_end)
    recent_cutoff = now - timedelta(days=14)
    recent_likes = sum(1 for ts in like_timestamps if ts > recent_cutoff)
    recent_velocity = recent_likes / 14

    score = float(likes_since_end)
    if recent_velocity >= _VELOCITY_KEEP_THRESHOLD:
        score += like_count
    return score


# ── Report rate-limit helper ───────────────────────────────────────────────────
def _check_report_rate_limit(user_id: str) -> None:
    """
    Allow at most 5 forum reports per user per minute.
    Prevents a malicious user from flooding the admin inbox.
    Fails open (allows through) if the rate limiter is unavailable.
    """
    try:
        from api.main import _limiter
        if not _limiter.is_allowed(f"forum_report:{user_id}", rpm=5):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many reports submitted. Please wait a moment before reporting again.",
            )
    except HTTPException:
        raise
    except Exception:
        # Rate limiter unavailable — fail open so reporting still works
        pass


# ── GET /posts ────────────────────────────────────────────────────────────────

@router.get("/posts", response_model=dict)
async def list_posts(
    req:      Request,
    category: Optional[str] = Query(None),
    subject:  Optional[str] = Query(None, max_length=10),
    sort:     str            = Query("hot"),
    search:   Optional[str] = Query(None),
    limit:    int            = Query(30, ge=1, le=100),
    offset:   int            = Query(0,  ge=0, le=9900),  # cap prevents full-table-scan DoS
    current_user_id: str     = Depends(get_current_user_id),
    user_sb                  = Depends(get_user_db),
):
    """
    List forum posts.

    - category: filter by category slug
    - subject: filter reviews by subject prefix (e.g. "COMP")
    - sort: hot (default; semester-aware upvote ranking, see _semester_aware_score) | new | top
    - search: matches title, body, and — for reviews — the course/professor
      name (review_target_value) and subject, so "COMP" or "Vybihal" finds
      relevant reviews even if those words never appear in the post body.
    - limit/offset: pagination
    """
    if sort not in VALID_SORTS:
        sort = "hot"

    def _run():
        q = user_sb.table("forum_posts").select(
            "id, user_id, author, avatar_color, category, title, body, tags, program_info, "
            "rating, difficulty_rating, review_target_type, review_target_value, professor_name, subject, "
            "like_count, created_at"
        )

        if category == "review":
            # The unified reviews feed also shows legacy course_review/
            # professor_review posts created before the 2026-07 merge —
            # they're the same kind of content, just under the old category
            # strings, and hiding them would make existing reviews vanish.
            q = q.in_("category", list(REVIEW_CATEGORIES))
        elif category and category != "all" and category in VALID_CATEGORIES:
            q = q.eq("category", category)

        if subject:
            q = q.eq("subject", subject.strip().upper())

        if search:
            safe = search.strip()[:100].replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            q = q.or_(
                f"title.ilike.%{safe}%,body.ilike.%{safe}%,"
                f"review_target_value.ilike.%{safe}%,subject.ilike.%{safe}%"
            )

        if sort == "new":
            q = q.order("created_at", desc=True)
        elif sort == "top":
            q = q.order("like_count", desc=True).order("created_at", desc=True)
        else:  # hot: like_count + recency hybrid handled in Python below
            q = q.order("created_at", desc=True)

        q = q.limit(limit + offset)  # fetch enough for offset
        return q.execute().data or []

    try:
        posts = with_retry("forum_list_posts", _run, retry_on_timeout=True)
    except Exception as e:
        logger.error(f"forum list_posts error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch posts")

    # Default ranking: semester-aware upvotes, replacing the old
    # like_count*2 + 48h-decay "hot" score. See _semester_aware_score.
    if sort == "hot":
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        like_timestamps = _get_post_like_timestamps(user_sb, [p["id"] for p in posts])
        posts.sort(key=lambda p: _semester_aware_score(p, like_timestamps.get(p["id"], []), now), reverse=True)

    posts = posts[offset:offset + limit]

    # Attach reply counts
    post_ids = [p["id"] for p in posts]
    reply_counts: dict = {}
    if post_ids:
        try:
            # Supabase doesn't do COUNT per group easily via REST — fetch ids only
            rc_res = (
                user_sb.table("forum_replies")
                .select("post_id")
                .in_("post_id", post_ids)
                .execute()
            )
            for r in (rc_res.data or []):
                pid = r["post_id"]
                reply_counts[pid] = reply_counts.get(pid, 0) + 1
        except Exception:
            pass

    # Attach per-user liked status
    liked_ids = _get_liked_post_ids(user_sb, current_user_id, post_ids)

    for p in posts:
        p["reply_count"] = reply_counts.get(p["id"], 0)
        p["liked"] = p["id"] in liked_ids

    return {"posts": posts, "total": len(posts)}


# ── POST /posts ───────────────────────────────────────────────────────────────

@router.post("/posts", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostCreate,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Create a new forum post. Auth + verified email required.

    SEC FIX: inserts via the service-role client, not the RLS-scoped
    user_sb — the direct-authenticated-insert RLS policy on forum_posts
    let anyone bypass every check in this function (suspension, email
    verification, McGill-domain gate, rate limiting, HTML escaping) by
    calling PostgREST directly with a valid Supabase JWT. See the paired
    migration dropping forum_posts_insert_own/forum_replies_insert_own.
    """
    # SEC FIX #5: a throwaway account with mailer_autoconfirm could otherwise
    # spam the forum from a disposable address. Verified email keeps the
    # community to people who control a real inbox.
    from ..utils.verified_user import is_email_verified
    from ..utils.anomaly import record_action
    from ..utils.moderation import require_not_suspended
    require_not_suspended(current_user_id)
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to post."})
    require_mcgill_email(current_user_id)
    record_action(current_user_id, "forum_post")

    # Validate review fields are present when category is a review
    try:
        payload.enforce_review_consistency()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # SEC FIX #9: HTML-escape user-controlled content at write time so even
    # if a future renderer interprets the body as HTML, no markup smuggling
    # is possible. We escape on write rather than render so existing
    # consumers (notifications, admin emails) are safe too.
    payload.title = escape(payload.title or "")
    payload.body  = escape(payload.body or "")
    if payload.author:
        payload.author = escape(payload.author)
    if payload.professor_name:
        payload.professor_name = escape(payload.professor_name)

    def _run():
        data = {
            "user_id":             current_user_id,
            "author":              payload.author,
            "avatar_color":        payload.avatar_color,
            "category":            payload.category,
            "title":               payload.title,
            "body":                payload.body,
            "tags":                payload.tags,
            "program_info":        payload.program_info,
            "rating":              payload.rating,
            "difficulty_rating":   payload.difficulty_rating,
            "review_target_type":  payload.review_target_type,
            "review_target_value": payload.review_target_value,
            "professor_name":      payload.professor_name,
            "subject":             payload.subject,
        }
        res = get_supabase().table("forum_posts").insert(data).execute()
        if not res.data:
            raise DatabaseException("create_post", "No data returned")
        return res.data[0]

    try:
        post = with_retry("forum_create_post", _run)
        post["reply_count"] = 0
        post["liked"] = False
        logger.info(f"Forum post created by {current_user_id}: {post['id']}")
        return {"post": post}
    except DatabaseException:
        raise HTTPException(status_code=500, detail="Failed to create post")
    except Exception as e:
        logger.exception(f"Unexpected error creating forum post: {e}")
        raise HTTPException(status_code=500, detail="Failed to create post")


# ── DELETE /posts/{post_id} ───────────────────────────────────────────────────

@router.delete("/posts/{post_id}", response_model=dict)
async def delete_post(
    post_id: str,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """Delete own forum post."""
    try:
        # Verify ownership
        existing = user_sb.table("forum_posts").select("user_id").eq("id", post_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Post not found")
        if existing.data[0]["user_id"] != current_user_id:
            raise HTTPException(status_code=403, detail="Not your post")
        user_sb.table("forum_posts").delete().eq("id", post_id).execute()
        return {"message": "Post deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error deleting forum post {post_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete post")


# ── GET /posts/{post_id}/replies ──────────────────────────────────────────────

@router.get("/posts/{post_id}/replies", response_model=dict)
async def list_replies(
    post_id: str,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """Fetch all replies for a post, oldest-first."""
    def _run():
        return (
            user_sb.table("forum_replies")
            .select("id, post_id, user_id, author, avatar_color, body, program_info, like_count, created_at")
            .eq("post_id", post_id)
            .order("created_at", desc=False)
            .execute()
            .data or []
        )

    try:
        replies = with_retry("forum_list_replies", _run, retry_on_timeout=True)
    except Exception as e:
        logger.error(f"forum list_replies error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch replies")

    # Attach per-user liked status
    reply_ids = [r["id"] for r in replies]
    liked_ids = _get_liked_reply_ids(user_sb, current_user_id, reply_ids)
    for r in replies:
        r["liked"] = r["id"] in liked_ids

    return {"replies": replies}


# ── POST /posts/{post_id}/replies ─────────────────────────────────────────────

@router.post("/posts/{post_id}/replies", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_reply(
    post_id: str,
    payload: ReplyCreate,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
):
    """Add a reply to a post. Auth + verified email required.

    SEC FIX: inserts via the service-role client — see create_post's
    docstring for why (the RLS-scoped client's insert policy let anyone
    bypass every Python-side check here via a direct PostgREST call)."""
    from ..utils.verified_user import is_email_verified
    from ..utils.anomaly import record_action
    from ..utils.moderation import require_not_suspended
    require_not_suspended(current_user_id)
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to reply."})
    require_mcgill_email(current_user_id)
    record_action(current_user_id, "forum_reply")
    payload.body = escape(payload.body or "")
    if payload.author:
        payload.author = escape(payload.author)

    def _run():
        supabase = get_supabase()
        # Verify post exists
        post_check = supabase.table("forum_posts").select("id").eq("id", post_id).execute()
        if not post_check.data:
            raise HTTPException(status_code=404, detail="Post not found")
        data = {
            "post_id":      post_id,
            "user_id":      current_user_id,
            "author":       payload.author,
            "avatar_color": payload.avatar_color,
            "body":         payload.body,
            "program_info": payload.program_info,
        }
        res = supabase.table("forum_replies").insert(data).execute()
        if not res.data:
            raise DatabaseException("create_reply", "No data returned")
        return res.data[0]

    try:
        reply = with_retry("forum_create_reply", _run)
        reply["liked"] = False
        logger.info(f"Forum reply created by {current_user_id} on post {post_id}")
        return {"reply": reply}
    except HTTPException:
        raise
    except DatabaseException:
        raise HTTPException(status_code=500, detail="Failed to create reply")
    except Exception as e:
        logger.exception(f"Unexpected error creating reply: {e}")
        raise HTTPException(status_code=500, detail="Failed to create reply")


# ── DELETE /replies/{reply_id} ────────────────────────────────────────────────

@router.delete("/replies/{reply_id}", response_model=dict)
async def delete_reply(
    reply_id: str,
    req:      Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """Delete own reply."""
    try:
        existing = user_sb.table("forum_replies").select("user_id").eq("id", reply_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Reply not found")
        if existing.data[0]["user_id"] != current_user_id:
            raise HTTPException(status_code=403, detail="Not your reply")
        user_sb.table("forum_replies").delete().eq("id", reply_id).execute()
        return {"message": "Reply deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error deleting reply {reply_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete reply")


# ── POST /posts/{post_id}/like ────────────────────────────────────────────────

@router.post("/posts/{post_id}/like", response_model=dict)
async def toggle_post_like(
    post_id: str,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """
    Toggle like on a post.
    Returns {liked: bool, like_count: int}.
    """
    require_mcgill_email(current_user_id)
    try:
        post_check = user_sb.table("forum_posts").select("user_id").eq("id", post_id).execute()
        if not post_check.data:
            raise HTTPException(status_code=404, detail="Post not found")
        if post_check.data[0]["user_id"] == current_user_id:
            raise HTTPException(status_code=400, detail="You cannot like your own post")

        # Check if already liked
        existing = (
            user_sb.table("forum_post_likes")
            .select("user_id")
            .eq("user_id", current_user_id)
            .eq("post_id", post_id)
            .execute()
        )
        already_liked = bool(existing.data)

        if already_liked:
            user_sb.table("forum_post_likes").delete() \
                .eq("user_id", current_user_id).eq("post_id", post_id).execute()
            # Decrement
            user_sb.rpc("decrement_post_like", {"p_post_id": post_id}).execute()
            delta = -1
        else:
            user_sb.table("forum_post_likes").insert(
                {"user_id": current_user_id, "post_id": post_id}
            ).execute()
            # Increment — use raw SQL via rpc
            user_sb.rpc("increment_post_like", {"p_post_id": post_id}).execute()
            delta = 1

        # Fetch updated count
        post_res = user_sb.table("forum_posts").select("like_count").eq("id", post_id).execute()
        like_count = post_res.data[0]["like_count"] if post_res.data else 0

        return {"liked": not already_liked, "like_count": like_count}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error toggling post like: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle like")


# ── POST /replies/{reply_id}/like ─────────────────────────────────────────────

@router.post("/replies/{reply_id}/like", response_model=dict)
async def toggle_reply_like(
    reply_id: str,
    req:      Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """Toggle like on a reply."""
    try:
        reply_check = user_sb.table("forum_replies").select("user_id").eq("id", reply_id).execute()
        if not reply_check.data:
            raise HTTPException(status_code=404, detail="Reply not found")
        if reply_check.data[0]["user_id"] == current_user_id:
            raise HTTPException(status_code=400, detail="You cannot like your own reply")

        existing = (
            user_sb.table("forum_reply_likes")
            .select("user_id")
            .eq("user_id", current_user_id)
            .eq("reply_id", reply_id)
            .execute()
        )
        already_liked = bool(existing.data)

        if already_liked:
            user_sb.table("forum_reply_likes").delete() \
                .eq("user_id", current_user_id).eq("reply_id", reply_id).execute()
            user_sb.rpc("decrement_reply_like", {"p_reply_id": reply_id}).execute()
        else:
            user_sb.table("forum_reply_likes").insert(
                {"user_id": current_user_id, "reply_id": reply_id}
            ).execute()
            user_sb.rpc("increment_reply_like", {"p_reply_id": reply_id}).execute()

        reply_res = user_sb.table("forum_replies").select("like_count").eq("id", reply_id).execute()
        like_count = reply_res.data[0]["like_count"] if reply_res.data else 0

        return {"liked": not already_liked, "like_count": like_count}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error toggling reply like: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle like")


# ── Helpers: report email ──────────────────────────────────────────────────────

def _send_report_email(content_type: str, content_id: str, reporter_id: str,
                       author: str, preview: str):
    """Send a report notification email to all admin addresses."""
    if not settings.RESEND_API_KEY:
        return
    admin_emails = [e.strip() for e in settings.ADMIN_EMAILS.split(",") if e.strip()]
    if not admin_emails:
        return

    safe_type    = escape(content_type)
    safe_id      = escape(content_id)
    safe_author  = escape(author or "Unknown")
    safe_preview = escape((preview or "")[:300])
    safe_reporter = escape(reporter_id)

    subject = f"🚩 Forum {safe_type} reported — Symbolos"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
        <tr><td style="background:#ED1B2F;border-radius:12px 12px 0 0;padding:20px 28px;">
          <span style="color:#fff;font-size:13px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;opacity:0.85;">Symbolos</span>
        </td></tr>
        <tr><td style="background:#ffffff;padding:28px;border-left:1px solid #e4e4e7;border-right:1px solid #e4e4e7;">
          <span style="display:inline-block;background:#fff7ed;color:#f97316;font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;padding:4px 10px;border-radius:20px;">Content Report</span>
          <h2 style="margin:16px 0 8px;font-size:20px;font-weight:700;color:#111827;">Forum {safe_type.capitalize()} Reported</h2>
          <p style="margin:0 0 20px;font-size:14px;color:#6b7280;">A forum {safe_type} has been flagged for review.</p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:20px;">
            <tr><td style="padding:4px 0;font-size:13px;color:#374151;"><strong>Type:</strong> {safe_type.capitalize()}</td></tr>
            <tr><td style="padding:4px 0;font-size:13px;color:#374151;"><strong>Content ID:</strong> {safe_id}</td></tr>
            <tr><td style="padding:4px 0;font-size:13px;color:#374151;"><strong>Author:</strong> {safe_author}</td></tr>
            <tr><td style="padding:4px 0;font-size:13px;color:#374151;"><strong>Reported by:</strong> {safe_reporter}</td></tr>
            <tr><td style="padding:12px 0 4px;font-size:13px;color:#374151;"><strong>Content preview:</strong></td></tr>
            <tr><td style="padding:8px 12px;background:#fff;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;color:#6b7280;font-style:italic;">{safe_preview}</td></tr>
          </table>
          <p style="margin:0;font-size:12px;color:#9ca3af;">Please review this content in the Symbolos admin panel.</p>
        </td></tr>
        <tr><td style="background:#f9fafb;border:1px solid #e4e4e7;border-top:none;border-radius:0 0 12px 12px;padding:16px 28px;">
          <p style="margin:0;font-size:11px;color:#9ca3af;">Symbolos Admin Notification</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    try:
        import httpx
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "Symbolos <notifications@symbolos.ca>", "to": admin_emails,
                  "subject": subject, "html": html},
            timeout=8,
        )
    except Exception as exc:
        logger.warning(f"Failed to send report email: {exc}")


# ── POST /posts/{post_id}/report ───────────────────────────────────────────────

@router.post("/posts/{post_id}/report", response_model=dict)
async def report_post(
    post_id: str,
    req:     Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """
    Flag a post for review. Persists to the reports table (survives an
    email/provider failure, appears in the moderator queue — #161) and
    still sends the existing admin email notification.

    No reason_category from the client yet — the frontend reason-category
    picker is Phase 2 (#161) UI work; this defaults to "other" so today's
    frontend (which sends no body at all) keeps working unchanged.
    """
    _check_report_rate_limit(current_user_id)
    try:
        from ..utils.moderation import create_report
        create_report(
            reporter_id=current_user_id, content_type="forum_post",
            content_id=post_id, reason_category="other",
        )

        post_res = user_sb.table("forum_posts").select("user_id, author, title, body").eq("id", post_id).execute()
        p = post_res.data[0] if post_res.data else {}
        preview = f"{p.get('title', '')} — {p.get('body', '')}"
        _send_report_email("post", post_id, current_user_id, p.get("author", ""), preview)
        logger.info(f"Post {post_id} reported by {current_user_id}")
        return {"message": "Report submitted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error reporting post {post_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit report")


# ── POST /replies/{reply_id}/report ───────────────────────────────────────────

@router.post("/replies/{reply_id}/report", response_model=dict)
async def report_reply(
    reply_id: str,
    req:      Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """Flag a reply for review. Persists to the reports table (#161) and
    still sends the existing admin email notification. See report_post's
    docstring for why reason_category defaults to "other" here."""
    _check_report_rate_limit(current_user_id)
    try:
        from ..utils.moderation import create_report
        create_report(
            reporter_id=current_user_id, content_type="forum_reply",
            content_id=reply_id, reason_category="other",
        )

        reply_res = user_sb.table("forum_replies").select("user_id, author, body").eq("id", reply_id).execute()
        r = reply_res.data[0] if reply_res.data else {}
        _send_report_email("reply", reply_id, current_user_id, r.get("author", ""), r.get("body", ""))
        logger.info(f"Reply {reply_id} reported by {current_user_id}")
        return {"message": "Report submitted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error reporting reply {reply_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit report")

# ════════════════════════════════════════════════════════════════════
#  Reviews — user's instructor/course list + rating aggregates
# ════════════════════════════════════════════════════════════════════

@router.get("/my-instructors", response_model=dict)
async def my_instructors(
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """
    Return the current user's distinct courses and professors aggregated across
    their completed_courses and current_courses rows (year-over-year history is
    preserved because each course has its own row per term).

    Returns:
      {
        "courses":    [{"course_code","course_title","occurrences":[{term,year,professor}, ...]}, ...]
        "professors": [{"name","courses":[{"course_code","term","year"}, ...]}, ...]
      }
    """
    try:
        completed = (user_sb.table("completed_courses")
                     .select("course_code, course_title, term, year, professor")
                     .eq("user_id", current_user_id).execute().data or [])
        current = (user_sb.table("current_courses")
                   .select("course_code, course_title, professor")
                   .eq("user_id", current_user_id).execute().data or [])

        # Build per-course aggregate
        course_map: dict = {}
        prof_map: dict = {}
        for row in completed + current:
            code = (row.get("course_code") or "").strip().upper()
            if not code:
                continue
            entry = course_map.setdefault(code, {
                "course_code": code,
                "course_title": row.get("course_title") or "",
                "occurrences": [],
            })
            occ = {
                "term":      row.get("term"),
                "year":      row.get("year"),
                "professor": row.get("professor") or None,
            }
            entry["occurrences"].append(occ)
            if not entry["course_title"] and row.get("course_title"):
                entry["course_title"] = row["course_title"]

            prof = (row.get("professor") or "").strip()
            if prof:
                p = prof_map.setdefault(prof, {"name": prof, "courses": []})
                p["courses"].append({
                    "course_code": code,
                    "term":        row.get("term"),
                    "year":        row.get("year"),
                })

        return {
            "courses":    sorted(course_map.values(), key=lambda c: c["course_code"]),
            "professors": sorted(prof_map.values(), key=lambda p: p["name"].lower()),
        }
    except Exception as e:
        logger.exception(f"my_instructors error for {current_user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch instructors")


@router.get("/reviews/summary", response_model=dict)
async def reviews_summary(
    target_type: str = Query(..., regex="^(course|professor)$"),
    target_value: str = Query(..., min_length=1, max_length=120),
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """
    Aggregate rating info for a single course or professor.
    Returns avg_rating, rating_count, and a per-star histogram.
    """
    try:
        # Case-insensitive match for professor names; exact match for course codes.
        # A course lookup matches every review naming that course, whether it's a
        # unified "review" post or a legacy course_review post (both set
        # review_target_type="course"). A professor lookup has to check two
        # shapes: legacy professor_review posts (the professor WAS the review
        # target) and unified review posts (the professor is the separate,
        # optional professor_name field on a course review).
        if target_type == "course":
            normalized = target_value.strip().upper()
            rows = user_sb.table("forum_posts") \
                .select("rating, difficulty_rating") \
                .eq("review_target_type", "course") \
                .eq("review_target_value", normalized) \
                .execute().data or []
        else:
            normalized = target_value.strip()
            legacy_rows = user_sb.table("forum_posts") \
                .select("rating, difficulty_rating") \
                .eq("review_target_type", "professor") \
                .ilike("review_target_value", normalized) \
                .execute().data or []
            named_rows = user_sb.table("forum_posts") \
                .select("rating, difficulty_rating") \
                .ilike("professor_name", normalized) \
                .execute().data or []
            rows = legacy_rows + named_rows

        ratings = [r["rating"] for r in rows if r.get("rating") is not None]
        difficulties = [r["difficulty_rating"] for r in rows if r.get("difficulty_rating") is not None]

        histogram = {str(i): 0 for i in range(1, 6)}
        for r in ratings:
            if 1 <= r <= 5:
                histogram[str(r)] += 1

        avg = round(sum(ratings) / len(ratings), 2) if ratings else None
        avg_difficulty = round(sum(difficulties) / len(difficulties), 2) if difficulties else None
        return {
            "target_type":     target_type,
            "target_value":    target_value,
            "avg_rating":      avg,
            "avg_difficulty":  avg_difficulty,
            "rating_count":    len(ratings),
            "histogram":       histogram,
        }
    except Exception as e:
        logger.exception(f"reviews_summary error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch review summary")


# ════════════════════════════════════════════════════════════════════
#  Set / update a course's professor (year-over-year persistence)
# ════════════════════════════════════════════════════════════════════

class CourseProfessorUpdate(BaseModel):
    professor: Optional[str] = Field(None, max_length=120)


@router.patch("/courses/{course_code}/professor", response_model=dict)
async def set_course_professor(
    course_code: str,
    payload: CourseProfessorUpdate,
    term: Optional[str] = Query(None, max_length=10),
    year: Optional[int] = Query(None, ge=1900, le=2100),
    current_user_id: str = Depends(get_current_user_id),
    user_sb = Depends(get_user_db),
):
    """
    Set the professor for a course in the user's history. Targets either
    completed_courses or current_courses by course_code (and optional term/year
    to disambiguate retakes).

    POST /api/forum/courses/COMP%20251/professor?term=Fall&year=2024
      body: {"professor": "Joseph Vybihal"}
    """
    code = course_code.strip().upper()
    if not code:
        raise HTTPException(status_code=422, detail="course_code required")

    prof_value = (payload.professor or "").strip()[:120] or None

    updated = 0
    try:
        for table in ("completed_courses", "current_courses"):
            q = user_sb.table(table).update({"professor": prof_value}) \
                .eq("user_id", current_user_id).eq("course_code", code)
            if term:
                q = q.eq("term", term.capitalize())
            if year:
                q = q.eq("year", year)
            res = q.execute()
            updated += len(res.data or [])
    except Exception as e:
        logger.exception(f"set_course_professor error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update professor")

    return {"updated": updated, "professor": prof_value}
