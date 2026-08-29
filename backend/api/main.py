"""
backend/api/main.py

FIX #13: Replaced InMemoryRateLimiter with SupabaseRateLimiter.
SEC-006: Tightened in-memory fallback limit from 10 to 3 rpm (per-instance on serverless).
SEC-009: Removed 'unsafe-inline' from script-src CSP; added frame-ancestors 'none'.
SEC-010: Normalise rate limit path key to prevent bypass via trailing slash/case.
"""
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import os
import time
import uuid
import logging
import base64
import json as _json
from datetime import datetime, timezone, timedelta


from .config import settings


# ── Sentry — exception telemetry ──────────────────────────────────────────
# Init must run BEFORE FastAPI app is created so the integration patches
# request handlers. Keeps quiet if SENTRY_DSN isn't set so local dev /
# CI / preview without a DSN doesn't error out.
def _init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=dsn,
            # Vercel sets VERCEL_GIT_COMMIT_SHA; we use it as the release tag
            # so a stack trace in Sentry maps to a specific commit.
            release=os.getenv("VERCEL_GIT_COMMIT_SHA", "dev"),
            environment=os.getenv("VERCEL_ENV") or os.getenv("ENVIRONMENT", "development"),
            integrations=[StarletteIntegration(), FastApiIntegration()],
            # Sample 10% of transactions in prod to keep quota under control.
            # We can crank up if we need more visibility.
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            # Strip PII — user IDs are sufficient, we don't need emails or IPs.
            send_default_pii=False,
        )
    except Exception:
        # Never let a Sentry init failure crash startup.
        logging.getLogger(__name__).warning("Sentry init skipped — SDK import failed")

_init_sentry()
from .logging_config import setup_logging
from .auth import get_current_user_id
from .exceptions import (
    AppException,
    app_exception_handler,
    validation_exception_handler,
    general_exception_handler,  
)

from .routes import (
    chat, courses, users, favorites, completed, notifications,
    current, suggestions, cards, transcript, degree_requirements,
    electives, clubs, syllabus, professors, admin, newsletters, forum,
    verification, webhooks, feedback, course_allocations, jobs,
    admin_approval, moderation,
)

logger = setup_logging()


def _validate_startup():
    """Fail fast if critical configuration is missing or invalid."""
    errors = []

    if not settings.ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is not set")
    elif not settings.ANTHROPIC_API_KEY.startswith("sk-ant-"):
        errors.append("ANTHROPIC_API_KEY has invalid format")

    if not settings.SUPABASE_URL:
        errors.append("SUPABASE_URL is not set")
    elif not settings.SUPABASE_URL.startswith("https://"):
        errors.append("SUPABASE_URL must start with https://")

    if not settings.SUPABASE_SERVICE_KEY:
        errors.append("SUPABASE_SERVICE_KEY is not set")

    if not settings.SUPABASE_ANON_KEY:
        logger.warning(
            "SUPABASE_ANON_KEY is not set — user-scoped queries will fall back to service role key. "
            "Set SUPABASE_ANON_KEY to enable Row Level Security enforcement."
        )

    if errors:
        for err in errors:
            logger.critical(f"STARTUP VALIDATION FAILED: {err}")
        raise RuntimeError(
            "Startup validation failed — fix the errors above before running."
        )

    logger.info("✅ Startup validation passed")


# ── FIX #13: Supabase-backed rate limiter ────────────────────────────────────

class SupabaseRateLimiter:
    """
    Sliding-window rate limiter backed by Supabase (Postgres).

    State is stored in the `rate_limits` table, so it survives serverless
    cold starts and is shared across all function instances.

    Schema (see sql/fix13_rate_limits_table.sql):
        rate_limits(key TEXT, window_start TIMESTAMPTZ, count INT)
        PRIMARY KEY (key, window_start)

    Strategy: 1-minute tumbling window.
      • window_start = current UTC minute truncated to the minute boundary
      • On each request: upsert (key, window_start) with count+1
      • If count after upsert ≥ limit → reject
      • Rows older than _PRUNE_AFTER are deleted lazily (every _PRUNE_EVERY calls)
    """

    _WINDOW_SECONDS = 60
    _PRUNE_AFTER    = timedelta(minutes=10)
    _PRUNE_EVERY    = 500  # prune stale rows once every N is_allowed() calls

    def __init__(self, default_rpm: int = 100):
        self.default_rpm = default_rpm
        self._call_count = 0
        self._fallback_counts: dict = {}  # F-04: in-memory fallback for DB outages

    @staticmethod
    def _window_start() -> str:
        """Current UTC minute boundary as an ISO string Postgres understands."""
        now = datetime.now(timezone.utc)
        truncated = now.replace(second=0, microsecond=0)
        return truncated.isoformat()

    def _get_supabase(self):
        # Import lazily to avoid circular imports and to keep startup fast.
        from .utils.supabase_client import get_supabase
        return get_supabase()

    # ── In-memory fallback (used when Supabase is unavailable) ───────────────
    # SEC-006: Reduced from 10 to 3 rpm. Each Vercel instance gets its own
    # in-memory counter during DB outages. With 10 rpm × 20 instances = 200
    # effective rpm. At 3 rpm × 20 instances = 60 rpm — still usable but much
    # safer for Claude API cost protection.
    _FALLBACK_RPM = 3

    def _fallback_is_allowed(self, key: str) -> bool:
        """Sliding-window in-memory check used when Supabase is unavailable."""
        now = time.time()
        window_start = now - self._WINDOW_SECONDS
        timestamps = self._fallback_counts.get(key, [])
        # Prune old entries
        timestamps = [t for t in timestamps if t > window_start]
        if len(timestamps) >= self._FALLBACK_RPM:
            return False
        timestamps.append(now)
        self._fallback_counts[key] = timestamps
        return True

    def is_allowed(self, key: str, rpm: int | None = None) -> bool:
        """
        Returns True if the request is within the rate limit.
        On Supabase error, applies a conservative in-memory fallback limit
        so a DB outage does not silently disable all rate limiting.
        """
        limit = rpm or self.default_rpm
        window = self._window_start()

        def _run():
            # Fetched INSIDE the retried closure, not captured before
            # with_retry runs it: a retry resets the global client singleton
            # (_reset_client), so grabbing it here is what makes a retry
            # actually use the fresh connection rather than the one that just
            # failed.
            supabase = self._get_supabase()

            # For true atomicity we call a small Postgres function.
            # If that function doesn't exist yet (not yet deployed), fall back
            # to the two-step approach.
            try:
                result = supabase.rpc(
                    'increment_rate_limit',
                    {'p_key': key, 'p_window': window}
                ).execute()
                new_count = result.data  # function returns the new count integer
            except Exception as rpc_exc:
                # A connection/timeout failure means this attempt never really
                # talked to Postgres, so falling back to two MORE calls on the
                # same broken connection just wastes time before with_retry
                # gets to retry anyway (and during a real outage, multiplies
                # every retry attempt into up to 3 Supabase round-trips
                # instead of 1). Only fall back to the manual read+write path
                # for a genuinely different failure — e.g. the RPC function
                # not existing yet — where a fresh connection wouldn't help
                # and retrying the SAME rpc() call would just fail again.
                from .utils.supabase_client import _is_connection_error, _is_timeout
                if _is_connection_error(rpc_exc) or _is_timeout(rpc_exc):
                    raise
                # Fallback: read + write (slightly less atomic but fine for our
                # traffic levels — duplicate requests within microseconds would
                # both pass, which is acceptable).
                read = (
                    supabase.table('rate_limits')
                    .select('count')
                    .eq('key', key)
                    .eq('window_start', window)
                    .execute()
                )
                if read.data:
                    new_count = read.data[0]['count'] + 1
                    supabase.table('rate_limits').update({
                        'count': new_count,
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    }).eq('key', key).eq('window_start', window).execute()
                else:
                    new_count = 1
                    supabase.table('rate_limits').insert({
                        'key': key,
                        'window_start': window,
                        'count': 1,
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    }).execute()

            # Lazy prune
            self._call_count += 1
            if self._call_count % self._PRUNE_EVERY == 0:
                self._prune(supabase)

            return new_count < limit

        try:
            # SYMBOLOS-BACKEND-19: this used to call the DB exactly once and
            # fall back to the degraded in-memory limiter on ANY exception,
            # including a single transient connection blip — the same class
            # of error the rest of the codebase already retries via
            # with_retry()/_is_connection_error(). Every other Supabase call
            # site got that resilience; this one, added earlier, never did.
            #
            # retry_on_timeout=True: the usual reason to leave this False is
            # that retrying a non-idempotent write after a read-timeout could
            # duplicate a row. Here the "duplicate" is a rate-limit counter
            # being one higher than reality, which makes the limiter slightly
            # MORE restrictive, never less — the safe direction for a rate
            # limiter to err in, unlike a generic write.
            # Lazy import, matching every other supabase_client use in this
            # module — avoids a circular import and keeps cold-start fast.
            #
            # is_allowed() is called synchronously from inside `async def`
            # middleware, so with_retry's time.sleep backoff can block the
            # event loop for up to ~0.9s (MAX_RETRIES=3) on a genuine
            # multi-attempt retry. That matches every other synchronous
            # Supabase call already wrapped in with_retry throughout this
            # codebase (get_user_by_id, create_user, …) called the same way
            # from async routes — not a new risk this introduces, just the
            # same accepted tradeoff applied consistently.
            from .utils.supabase_client import with_retry
            return with_retry("rate_limiter", _run, retry_on_timeout=True)

        except Exception as e:
            logger.error(
                f"[SECURITY] Rate limiter DB unavailable — falling back to in-memory "
                f"(limit={self._FALLBACK_RPM} rpm). Multi-instance protection is "
                f"degraded until Supabase recovers. Error: {type(e).__name__}"
            )
            return self._fallback_is_allowed(key)

    def _prune(self, supabase) -> None:
        """Delete rate_limit rows older than _PRUNE_AFTER. Best-effort."""
        try:
            cutoff = (datetime.now(timezone.utc) - self._PRUNE_AFTER).isoformat()
            supabase.table('rate_limits').delete().lt('window_start', cutoff).execute()
        except Exception as e:
            logger.debug(f"Rate limit prune failed (non-critical): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_startup()
    logger.info(f"Starting {settings.API_TITLE} v{settings.API_VERSION}")
    yield
    logger.info("Shutting down…")
    from .utils.posthog_client import shutdown as _posthog_shutdown
    _posthog_shutdown()


app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    lifespan=lifespan,
    # SEC FIX (audit #11 / Bonus): in production we hide docs AND the raw
    # OpenAPI schema. The auditor pulled the full 104-route list from
    # /openapi.json — that's an attacker's shopping list. Local dev still
    # gets full docs because DEBUG is set.
    docs_url=f"{settings.API_PREFIX}/docs" if settings.DEBUG else None,
    redoc_url=f"{settings.API_PREFIX}/redoc" if settings.DEBUG else None,
    openapi_url=f"{settings.API_PREFIX}/openapi.json" if settings.DEBUG else None,
)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Cron-Secret"],
    expose_headers=["X-Process-Time", "X-Request-ID"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Inject standard security headers on every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # SEC-009: Removed 'unsafe-inline' from script-src. The React/Vite frontend
    # doesn't need inline scripts. Added frame-ancestors 'none' for clickjacking
    # protection (stronger than X-Frame-Options in modern browsers).
    # NOTE: If the Vite build injects inline <script> tags and this breaks,
    # revert to "'self' 'unsafe-inline'" and implement nonce-based CSP next sprint.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://us-assets.i.posthog.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' https://*.supabase.co https://*.supabase.in https://ai-advisor-backend-seven.vercel.app "
        "https://*.ingest.us.sentry.io https://us.i.posthog.com https://us-assets.i.posthog.com; "
        "img-src 'self' data: blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# FIX #13: Use Supabase-backed limiter instead of in-memory
_limiter = SupabaseRateLimiter(default_rpm=settings.RATE_LIMIT_PER_MINUTE)


def _get_client_ip(request: Request) -> str:
    """
    Extract the real client IP from the request.

    Vercel (and most reverse proxies) append the connecting IP to the
    X-Forwarded-For header, making it the LAST entry.  The earlier entries
    are supplied by the client and therefore untrusted — using the first
    entry allows an attacker to bypass IP-based rate limits by injecting a
    fake IP at the front of the header (e.g. X-Forwarded-For: 1.2.3.4,
    real-ip).

    Strategy:
      1. Use the LAST value in X-Forwarded-For (appended by the edge).
      2. Fall back to the WSGI/ASGI remote address if the header is absent.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # The rightmost IP is the one appended by the trusted infrastructure
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        return parts[-1] if parts else "unknown"
    return request.client.host if request.client else "unknown"


def _get_user_id_from_token(request: Request) -> str | None:
    """
    Extract the user ID (sub claim) from the Bearer JWT without full verification.
    Auth middleware handles cryptographic verification — this is used ONLY to key
    per-user rate limit buckets so a shared corporate IP doesn't exhaust one limit
    for all users, and so a single user can't bypass limits by rotating IPs.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        # JWT payload is base64url-encoded — add padding if needed
        payload_b64 = parts[1]
        rem = len(payload_b64) % 4
        if rem:
            payload_b64 += "=" * (4 - rem)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = _get_client_ip(request)

    # SEC-010: Normalise path to prevent bypass via trailing slash or case variation.
    # Without this, /api/chat/send and /api/chat/send/ and /api/Chat/send
    # would each get separate rate limit buckets.
    raw_path = request.url.path
    normalised_path = raw_path.rstrip("/").lower()

    # Tier the rate limit by endpoint cost:
    #   - /api/chat/*                       : CHAT_RATE_LIMIT_PER_MINUTE (50/IP)
    #   - cards generate/stream/ask/translate : tightest — Claude + DB heavy
    #   - electives/recommend               : tight — single Claude call
    #   - transcript/parse|import           : tight — Claude with PDF (slow + costly)
    #   - everything else                   : RATE_LIMIT_PER_MINUTE (100/IP)
    if "/chat" in normalised_path:
        rpm = settings.CHAT_RATE_LIMIT_PER_MINUTE
    elif (
        "/cards/generate" in normalised_path
        or "/cards/stream"   in normalised_path
        or "/cards/ask"      in normalised_path
        or "/cards/retranslate" in normalised_path
        or "/electives/recommend" in normalised_path
        or "/transcript/parse"    in normalised_path
        or "/transcript/import"   in normalised_path
        or "/syllabus/parse"      in normalised_path
    ):
        rpm = settings.CLAUDE_RATE_LIMIT_PER_MINUTE
    elif "/inngest" in normalised_path:
        # Inngest callback — verified by HMAC signature, not rate-limited
        return await call_next(request)
    elif "/auth/check-verified" in normalised_path or "/auth/verify-email" in normalised_path:
        # Unauthenticated verification endpoints: tighter limit to block UUID
        # enumeration. Legitimate polling is every 3 s (~20/min), so 20 rpm
        # per IP covers normal use while blocking brute-force UUID scans.
        rpm = 20
    else:
        rpm = settings.RATE_LIMIT_PER_MINUTE

    # ── IP-based check (covers unauthenticated requests + shared-IP DoS) ──────
    if not _limiter.is_allowed(f"ip:{client_ip}:{normalised_path}", rpm):
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too many requests. Please try again later.",
                "details": {"retry_after": 60},
            },
        )

    # ── Per-user check (prevents single user from exhausting the IP bucket) ───
    # Authenticated routes only — unauthenticated requests have no user ID.
    user_id = _get_user_id_from_token(request)
    if user_id:
        user_rpm = max(rpm // 2, 10)  # per-user limit is half the IP limit, min 10
        if not _limiter.is_allowed(f"user:{user_id}:{normalised_path}", user_rpm):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please try again later.",
                    "details": {"retry_after": 60},
                },
            )

    return await call_next(request)


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    request_id = str(uuid.uuid4())
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    response.headers["X-Request-ID"] = request_id
    return response

@app.get(f"{settings.API_PREFIX}/sentry-test")
async def sentry_test():
    raise ValueError("Sentry backend test — safe to ignore")

# Exception handlers
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# Routers
app.include_router(admin.router,               prefix=f"{settings.API_PREFIX}/admin",               tags=["Admin"])
app.include_router(chat.router,                prefix=f"{settings.API_PREFIX}/chat",                tags=["Chat"])
app.include_router(courses.router,             prefix=f"{settings.API_PREFIX}/courses",             tags=["Courses"])
app.include_router(users.router,               prefix=f"{settings.API_PREFIX}/users",               tags=["Users"])
app.include_router(favorites.router,           prefix=f"{settings.API_PREFIX}/favorites",           tags=["Favorites"])
app.include_router(completed.router,           prefix=f"{settings.API_PREFIX}/completed",           tags=["Completed"])
app.include_router(notifications.router,       prefix=f"{settings.API_PREFIX}/notifications",       tags=["Notifications"])
app.include_router(current.router,             prefix=f"{settings.API_PREFIX}/current",             tags=["Current Courses"])
app.include_router(suggestions.router,         prefix=f"{settings.API_PREFIX}/suggestions",         tags=["Suggestions"])
app.include_router(cards.router,               prefix=f"{settings.API_PREFIX}/cards",               tags=["Cards"])
app.include_router(transcript.router,          prefix=f"{settings.API_PREFIX}/transcript",          tags=["Transcript"])
app.include_router(degree_requirements.router, prefix=f"{settings.API_PREFIX}/degree-requirements", tags=["Degree Requirements"])
app.include_router(electives.router,           prefix=f"{settings.API_PREFIX}/electives",           tags=["Electives"])
app.include_router(clubs.router,               prefix=f"{settings.API_PREFIX}/clubs",               tags=["Clubs"])
app.include_router(syllabus.router,            prefix=f"{settings.API_PREFIX}/syllabus",            tags=["Syllabus"])
app.include_router(professors.router,          prefix=f"{settings.API_PREFIX}/professors",          tags=["Professors"])
app.include_router(newsletters.router,         prefix=f"{settings.API_PREFIX}/newsletters",         tags=["Newsletters"])
app.include_router(forum.router,               prefix=f"{settings.API_PREFIX}/forum",               tags=["Forum"])
app.include_router(verification.router,        prefix=f"{settings.API_PREFIX}/auth",                tags=["Auth"])
app.include_router(webhooks.router,             prefix=f"{settings.API_PREFIX}/webhooks",            tags=["Webhooks"])
app.include_router(feedback.router,             prefix=f"{settings.API_PREFIX}/feedback",            tags=["Feedback"])
app.include_router(course_allocations.router,   prefix=f"{settings.API_PREFIX}/users",               tags=["Course Allocations"])
app.include_router(jobs.router,                 prefix=f"{settings.API_PREFIX}/jobs",                tags=["Jobs"])
app.include_router(admin_approval.router,       prefix=f"{settings.API_PREFIX}/admin-approval",      tags=["Admin Approval"])
app.include_router(moderation.reports_router,   prefix=f"{settings.API_PREFIX}/reports",             tags=["Trust & Safety"])
app.include_router(moderation.moderation_router, prefix=f"{settings.API_PREFIX}/moderation",          tags=["Trust & Safety"])

# ── Inngest serve endpoint ───────────────────────────────────────────────────
# Registers /api/inngest so Inngest can call back our background functions.
# Must come after all routers are included.
#
# Preview deployments don't get INNGEST_* env vars (they're set on Production
# only), so serving here logged "missing signing key" at error level on every
# preview boot — 199 events in Sentry, all environment=preview, none of them
# actionable. Production is unaffected and config.py already hard-fails there
# if the keys are absent, so skipping when unkeyed can't mask a real outage.
#
# Local dev is deliberately still served: the Inngest dev server doesn't check
# signatures, so background jobs keep working without any keys.
_vercel_env = os.getenv("VERCEL_ENV") or os.getenv("ENVIRONMENT", "development")
_is_vercel_preview = _vercel_env == "preview"

if _is_vercel_preview and not settings.INNGEST_SIGNING_KEY.strip():
    logger.info(
        "Inngest serve skipped — no signing key on this preview deployment. "
        "Background jobs are disabled here; production is unaffected."
    )
else:
    from .inngest_app import inngest_client, INNGEST_FUNCTIONS
    import inngest.fast_api
    inngest.fast_api.serve(app, inngest_client, INNGEST_FUNCTIONS)


@app.get("/")
async def root():
    # SEC-09: Mirror health endpoint — don't expose version in production.
    response = {
        "service": settings.API_TITLE,
        "status": "operational",
        "docs": f"{settings.API_PREFIX}/docs" if settings.DEBUG else None,
    }
    if settings.DEBUG:
        response["version"] = settings.API_VERSION
    return response


@app.get(f"{settings.API_PREFIX}/health")
async def health_check(deep: bool = False):
    """Liveness + optional dependency readiness check.

    Default (?deep=false): cheap liveness — "the function booted". Used as
    the fast path so a flood of monitor pings doesn't cost a DB query each.

    ?deep=true: verifies the Supabase connection actually works. THIS is
    what the uptime monitor should poll, because a shallow check reports
    "healthy" even when the database is unreachable — which is exactly the
    outage you most want to be paged about. Returns 503 if the DB is down
    so Better Stack / UptimeRobot register it as an incident.
    """
    response = {"status": "healthy"}
    if settings.DEBUG:
        response["version"] = settings.API_VERSION

    if deep:
        checks = {}
        ok = True
        # Database round-trip — cheap single-row read.
        try:
            from .utils.supabase_client import get_supabase
            get_supabase().table("users").select("id").limit(1).execute()
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = "error"
            ok = False
            logger.error("Health check DB probe failed: %s", type(exc).__name__)

        response["checks"] = checks
        if not ok:
            response["status"] = "degraded"
            return JSONResponse(status_code=503, content=response)

    return response


@app.get(f"{settings.API_PREFIX}/auth/flags")
async def auth_flags(current_user_id: str = Depends(get_current_user_id)):
    """Return auth flags for the current user (admin status, McGill email).

    SEC FIX #2: the McGill flag MUST come from auth.users.email (the verified
    identity Supabase received during signup), not the users table column the
    profile owner can edit. The previous version read users.email, so anyone
    could PATCH their profile to set email="<anything>@mail.mcgill.ca" and
    flip is_mcgill_email to true — gaining access to private clubs and
    McGill-only features.
    """
    from .utils.supabase_client import get_supabase
    admin_emails = set(e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip())
    mcgill_domains = ("@mcgill.ca", "@mail.mcgill.ca")
    try:
        sb = get_supabase()
        # Verified auth email — the user CANNOT change this through any
        # public route. Supabase only updates it after the user proves
        # control of the new mailbox via Supabase Auth's own email-change
        # confirmation flow.
        auth_email = ""
        email_confirmed = False
        try:
            u = sb.auth.admin.get_user_by_id(current_user_id)
            auth_email = (getattr(u.user, "email", None) or "").lower()
            email_confirmed = bool(getattr(u.user, "email_confirmed_at", None))
        except Exception:
            pass

        is_admin = auth_email in admin_emails
        # Require BOTH correct domain AND confirmed email — prevents a fake
        # signup with an unverified @mcgill.ca address from gaining McGill access.
        is_mcgill = (any(auth_email.endswith(d) for d in mcgill_domains) and email_confirmed) or is_admin
        return {"is_admin": is_admin, "is_mcgill_email": is_mcgill}
    except Exception:
        return {"is_admin": False, "is_mcgill_email": False}
