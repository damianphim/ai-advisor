"""
backend/api/routes/admin.py

Backend admin authentication endpoint.
- Uses constant-time comparison (hmac.compare_digest) to prevent timing attacks.
- Separate ADMIN_SECRET from CRON_SECRET — login no longer leaks CRON_SECRET (F-03).
  Instead, a short-lived signed admin session token is issued (15-minute expiry).
- Brute-force protection is now Supabase-backed (F-05) so the 5-attempt limit
  is shared across all serverless instances. Falls back to in-memory on DB error.
"""
import hashlib
import hmac
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ..config import settings
from ..utils.supabase_client import get_supabase
from ..utils.audit import log_access

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Admin session token ───────────────────────────────────────────────────────
# Format: "admin:<exp_unix_timestamp>:<hmac_sha256_hex>"
# Signed with ADMIN_SECRET so the server can verify without storing sessions.

_ADMIN_TOKEN_TTL = 900  # 15 minutes (reduced from 1 hour for security)

# ── Token revocation list ─────────────────────────────────────────────────────
# In-memory set of revoked token signatures. Cleared on cold start, which is
# acceptable because tokens are short-lived (15 min) and worst case a revoked
# token survives until its natural expiry.
_revoked_tokens: set[str] = set()


def _issue_admin_token() -> str:
    """Issue a short-lived signed admin session token."""
    exp = int(time.time()) + _ADMIN_TOKEN_TTL
    payload = f"admin:{exp}"
    sig = hmac.new(
        settings.ADMIN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{sig}"


def revoke_admin_token(token: str) -> None:
    """Add a token's signature to the revocation set."""
    parts = token.split(":")
    if len(parts) == 3:
        _revoked_tokens.add(parts[2])


def verify_admin_token(token: str) -> bool:
    """
    Verify a previously issued admin session token.
    Returns False if the token is malformed, expired, revoked, or has an invalid signature.
    """
    try:
        parts = token.split(":")
        if len(parts) != 3 or parts[0] != "admin":
            return False
        exp = int(parts[1])
        if time.time() > exp:
            # Lazily clean up expired entries from the revocation set
            _revoked_tokens.discard(parts[2])
            return False
        # Check revocation list before expensive HMAC
        if parts[2] in _revoked_tokens:
            return False
        payload = f"admin:{exp}"
        expected_sig = hmac.new(
            settings.ADMIN_SECRET.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(parts[2], expected_sig)
    except Exception:
        return False


# ── Brute-force protection (Supabase-backed, F-05) ────────────────────────────
# Attempts are stored in the same `rate_limits` table used by the main rate
# limiter so the counter is shared across all serverless instances.
# Falls back to an in-memory dict if Supabase is unavailable.

_ADMIN_RATE_LIMIT = 5    # max attempts per IP per window
_ADMIN_RATE_WINDOW = 60  # seconds
# Global cap shared by ALL callers regardless of claimed IP — closes the
# X-Forwarded-For header-rotation bypass of the per-IP limiter above (an
# attacker who supplies a fresh fake IP on every request would otherwise
# never hit the per-IP cap).
_ADMIN_GLOBAL_RATE_LIMIT = 20   # max attempts across all IPs per window
_ADMIN_GLOBAL_KEY = "admin_login:__global__"

# In-memory fallback (used only when Supabase is down)
_fallback_attempts: dict[str, list[float]] = defaultdict(list)


def _check_admin_rate_limit(ip: str) -> None:
    """
    Block the request if this IP has exceeded the admin login attempt limit.
    Uses Supabase for shared state across instances; falls back to in-memory.
    Raises HTTP 429 if the limit is exceeded.
    """
    # SEC-FIX: `ip` is derived from a client-controllable header
    # (X-Forwarded-For) with no verification that it was actually appended
    # by a trusted single proxy hop. Keying the ONLY limiter by this value
    # lets an attacker mint a fresh, never-before-seen key on every request
    # (a new fake IP each time) and get an unlimited number of attempts.
    # Fix: bound the credential check with a global, non-spoofable counter
    # in addition to the existing per-IP one, so header rotation can no
    # longer produce unlimited attempts against ADMIN_SECRET.
    _check_admin_rate_limit_key(f"admin_login:{ip}", _ADMIN_RATE_LIMIT, log_label=f"IP: {ip}")
    _check_admin_rate_limit_key(_ADMIN_GLOBAL_KEY, _ADMIN_GLOBAL_RATE_LIMIT, log_label="global")


def _check_admin_rate_limit_key(key: str, limit: int, *, log_label: str) -> None:
    """
    Block the request if `key` has exceeded `limit` attempts in the current
    window. Uses Supabase for shared state across instances; falls back to
    in-memory. Raises HTTP 429 if the limit is exceeded.
    """
    now = time.time()
    window_start_ts = now - _ADMIN_RATE_WINDOW

    # ── Try Supabase-backed check ─────────────────────────────────────────────
    try:
        from ..utils.supabase_client import get_supabase
        supabase = get_supabase()
        window_iso = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()

        try:
            result = supabase.rpc(
                "increment_rate_limit",
                {"p_key": key, "p_window": window_iso},
            ).execute()
            attempt_count = result.data
        except Exception:
            # RPC not available — two-step fallback (same as main rate limiter)
            read = (
                supabase.table("rate_limits")
                .select("count")
                .eq("key", key)
                .eq("window_start", window_iso)
                .execute()
            )
            if read.data:
                attempt_count = read.data[0]["count"] + 1
                supabase.table("rate_limits").update({
                    "count": attempt_count,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("key", key).eq("window_start", window_iso).execute()
            else:
                attempt_count = 1
                supabase.table("rate_limits").insert({
                    "key": key,
                    "window_start": window_iso,
                    "count": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).execute()

        if attempt_count > limit:
            logger.warning(f"Admin brute-force lockout (Supabase) for {log_label}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please wait 60 seconds.",
            )
        return  # Supabase check passed

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Admin rate-limit DB error, falling back to in-memory: {e}")

    # ── In-memory fallback ────────────────────────────────────────────────────
    attempts = _fallback_attempts[key]
    _fallback_attempts[key] = [t for t in attempts if t > window_start_ts]
    if len(_fallback_attempts[key]) >= limit:
        logger.warning(f"Admin brute-force lockout (in-memory fallback) for {log_label}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait 60 seconds.",
        )
    _fallback_attempts[key].append(now)


# ── Request schema ────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    secret: str


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/verify", status_code=status.HTTP_200_OK)
async def verify_admin(request: AdminLoginRequest, req: Request):
    """
    Verify the admin secret and issue a short-lived signed session token.

    - Authenticates with ADMIN_SECRET only (CRON_SECRET is never returned).
    - Issues a 15-minute signed token the frontend uses for subsequent admin calls.
    - Constant-time comparison prevents timing attacks.
    - Rate-limited to 5 attempts/minute per IP, Supabase-backed and shared
      across all serverless instances (falls back to in-memory if DB is down).
    """
    forwarded_for = req.headers.get("x-forwarded-for")
    if forwarded_for:
        # Use the rightmost entry — appended by Vercel's infrastructure in
        # the common case. This value is still attacker-influenced input
        # (there is no verification the request actually traversed a
        # trusted single-hop proxy that appends rather than passes the
        # header through untouched), so `_check_admin_rate_limit` also
        # enforces a global, non-IP-keyed cap — see its definition.
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        client_ip = parts[-1] if parts else "unknown"
    else:
        client_ip = req.client.host if req.client else "unknown"
    _check_admin_rate_limit(client_ip)

    admin_secret = settings.ADMIN_SECRET
    if not admin_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin access not configured",
        )

    if not hmac.compare_digest(request.secret, admin_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    logger.info(f"Admin access granted from {client_ip}")
    # Persist to the append-only audit trail — server logs alone (above) roll
    # off with the hosting provider's retention window; this is the durable
    # record of when the admin credential was used.
    log_access(user_id=None, action="admin_login", req=req)
    # Return a scoped, time-limited admin session token — NOT the CRON_SECRET.
    return {
        "token": _issue_admin_token(),
        "expires_in": _ADMIN_TOKEN_TTL,
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(req: Request):
    """Aggregate usage stats for the admin dashboard."""
    token = req.headers.get("X-Cron-Secret", "")
    if not verify_admin_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    sb = get_supabase()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()

    def _count(table, *, gte=None, eq=None):
        q = sb.table(table).select("id", count="exact")
        if gte:
            q = q.gte("created_at", gte)
        if eq:
            for col, val in eq.items():
                q = q.eq(col, val)
        try:
            return q.execute().count or 0
        except Exception:
            return 0

    total_users   = _count("users")
    new_7d_users  = _count("users", gte=week_ago)
    new_30d_users = _count("users", gte=month_ago)

    # Active = distinct users who sent at least one message in the past 7 days
    try:
        active_rows = (
            sb.table("chat_messages")
            .select("user_id")
            .eq("role", "user")
            .gte("created_at", week_ago)
            .execute()
        )
        active_7d = len({r["user_id"] for r in (active_rows.data or [])})
    except Exception:
        active_7d = 0

    total_msgs    = _count("chat_messages", eq={"role": "user"})
    msgs_7d       = _count("chat_messages", gte=week_ago, eq={"role": "user"})
    msgs_30d      = _count("chat_messages", gte=month_ago, eq={"role": "user"})

    feedback_total = _count("feedback")
    feedback_7d    = _count("feedback", gte=week_ago)
    jobs_total     = _count("jobs")
    forum_posts    = _count("forum_posts")
    club_joins     = _count("user_clubs")
    advisor_cards  = _count("advisor_cards")

    return {
        "users": {
            "total":     total_users,
            "new_7d":    new_7d_users,
            "new_30d":   new_30d_users,
            "active_7d": active_7d,
        },
        "messages": {
            "total":               total_msgs,
            "last_7d":             msgs_7d,
            "last_30d":            msgs_30d,
            "avg_per_active_user": round(msgs_7d / active_7d, 1) if active_7d else 0,
        },
        "features": {
            "transcript_uploads":   jobs_total,
            "feedback_total":       feedback_total,
            "feedback_7d":          feedback_7d,
            "forum_posts":          forum_posts,
            "club_joins":           club_joins,
            "advisor_cards":        advisor_cards,
        },
    }
