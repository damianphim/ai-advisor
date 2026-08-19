"""
backend/api/routes/newsletters.py

Newsletter system — subscribe to external RSS/iCal newsletter sources,
sync events to user calendars, manage subscriptions.

Tables (create in Supabase):

  newsletter_sources:
    id          UUID PK default gen_random_uuid()
    name        TEXT NOT NULL
    url         TEXT NOT NULL          -- RSS or iCal feed URL
    feed_type   TEXT DEFAULT 'rss'     -- 'rss' | 'ical'
    category    TEXT DEFAULT 'general'
    logo_url    TEXT
    created_at  TIMESTAMPTZ DEFAULT now()

  newsletter_subscriptions:
    id          UUID PK default gen_random_uuid()
    user_id     UUID NOT NULL REFERENCES auth.users(id)
    source_id   UUID NOT NULL REFERENCES newsletter_sources(id) ON DELETE CASCADE
    calendar_sync BOOLEAN DEFAULT true
    created_at  TIMESTAMPTZ DEFAULT now()
    UNIQUE(user_id, source_id)

  newsletter_events:
    id          UUID PK default gen_random_uuid()
    source_id   UUID NOT NULL REFERENCES newsletter_sources(id) ON DELETE CASCADE
    title       TEXT NOT NULL
    description TEXT
    date        DATE NOT NULL
    time        TEXT           -- HH:MM 24h
    end_time    TEXT
    location    TEXT
    link        TEXT
    external_id TEXT           -- dedup key from feed
    created_at  TIMESTAMPTZ DEFAULT now()
    UNIQUE(source_id, external_id)
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel, Field
from typing import Optional, List
import logging
import re
import hmac
import hashlib
from datetime import datetime, timezone, date
from html import escape

from ..utils.supabase_client import get_supabase
from ..auth import get_current_user_id
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

MCGILL_EMAIL_DOMAINS = ("mcgill.ca", "mail.mcgill.ca")

def _get_admin_emails() -> set:
    return set(e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip())


async def _require_mcgill_email(user_id: str):
    """Verify the user has a McGill email address or is an admin. Raises 403 if not."""
    try:
        sb = get_supabase()
        resp = sb.table("users").select("email").eq("id", user_id).single().execute()
        email = (resp.data or {}).get("email", "")
        if not email or (
            not any(email.lower().endswith(f"@{d}") for d in MCGILL_EMAIL_DOMAINS)
            and email.lower() not in _get_admin_emails()
        ):
            raise HTTPException(
                status_code=403,
                detail="Newsletter subscriptions are only available for McGill email addresses",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"McGill email check failed: {e}")
        raise HTTPException(status_code=403, detail="Could not verify McGill email")


def _send_newsletter_email(to_email: str, source_name: str, source_url: str, action: str = "subscribed"):
    """Send a newsletter subscription confirmation email."""
    if not settings.RESEND_API_KEY:
        return
    safe_name = escape(source_name)
    safe_url = escape(source_url)
    subject = f"📰 Subscribed to {safe_name} — Symbolos"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">
        <tr><td style="background:#ED1B2F;border-radius:12px 12px 0 0;padding:20px 28px;">
          <span style="color:#fff;font-size:13px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;opacity:0.85;">Symbolos</span>
        </td></tr>
        <tr><td style="background:#ffffff;padding:28px;border-left:1px solid #e4e4e7;border-right:1px solid #e4e4e7;">
          <div style="margin-bottom:16px;">
            <span style="display:inline-block;background:#ecfeff;color:#0891b2;font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;padding:4px 10px;border-radius:20px;">Newsletter</span>
          </div>
          <h1 style="margin:0 0 8px;font-size:22px;font-weight:700;color:#111827;line-height:1.3;">Subscribed to {safe_name}</h1>
          <p style="margin:0 0 16px;font-size:14px;color:#374151;line-height:1.6;">
            You'll receive email updates when new events are posted from <strong>{safe_name}</strong>.
            Events will also appear on your Symbolos calendar.
          </p>
          <div style="text-align:center;margin-top:20px;">
            <a href="{safe_url}" style="display:inline-block;background:#0891b2;color:#fff;font-size:14px;font-weight:600;text-decoration:none;padding:12px 28px;border-radius:8px;margin-right:8px;">View Newsletter</a>
            <a href="https://symbolos.ca" style="display:inline-block;background:#ED1B2F;color:#fff;font-size:14px;font-weight:600;text-decoration:none;padding:12px 28px;border-radius:8px;">Open Symbolos</a>
          </div>
        </td></tr>
        <tr><td style="background:#f9fafb;border:1px solid #e4e4e7;border-top:none;border-radius:0 0 12px 12px;padding:16px 28px;text-align:center;">
          <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
            You're receiving this because you subscribed to {safe_name} on Symbolos.<br/>
            You can unsubscribe anytime from Settings.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        import httpx
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "Symbolos <notifications@symbolos.ca>", "to": [to_email], "subject": subject, "html": html},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(f"Resend error for newsletter sub: {resp.status_code} {resp.text}")
        else:
            logger.info(f"Newsletter subscription email sent to {to_email} for {source_name}")
    except Exception as e:
        logger.exception(f"Failed to send newsletter subscription email: {e}")


def _notify_newsletter_subscribers(source_id: str, source_name: str, events: list):
    """Email all subscribers of a newsletter source about new events."""
    if not settings.RESEND_API_KEY or not events:
        return
    try:
        sb = get_supabase()
        subs = sb.table("newsletter_subscriptions").select("user_id, email_muted").eq("source_id", source_id).execute()
        if not subs.data:
            return

        emails = []
        for s in subs.data:
            if s.get("email_muted"):
                continue
            try:
                u = sb.table("users").select("email").eq("id", s["user_id"]).single().execute()
                if u.data and u.data.get("email"):
                    emails.append(u.data["email"])
            except Exception:
                continue
        if not emails:
            return

        safe_name = escape(source_name)
        event_rows = ""
        for ev in events[:10]:
            ev_title = escape(ev.get("title", ""))
            ev_date = escape(ev.get("date", ""))
            ev_time = escape(ev.get("time") or "")
            event_rows += f"""<tr>
              <td style="padding:8px 12px;font-size:14px;color:#111827;font-weight:600;border-bottom:1px solid #f0f0f0;">{ev_title}</td>
              <td style="padding:8px 12px;font-size:13px;color:#6b7280;border-bottom:1px solid #f0f0f0;white-space:nowrap;">{ev_date} {ev_time}</td>
            </tr>"""

        subject = f"📰 {len(events)} new event{'s' if len(events) != 1 else ''} from {safe_name}"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">
        <tr><td style="background:#ED1B2F;border-radius:12px 12px 0 0;padding:20px 28px;">
          <span style="color:#fff;font-size:13px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;opacity:0.85;">Symbolos</span>
        </td></tr>
        <tr><td style="background:#ffffff;padding:28px;border-left:1px solid #e4e4e7;border-right:1px solid #e4e4e7;">
          <div style="margin-bottom:16px;">
            <span style="display:inline-block;background:#ecfeff;color:#0891b2;font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;padding:4px 10px;border-radius:20px;">Newsletter Events</span>
          </div>
          <h1 style="margin:0 0 8px;font-size:20px;font-weight:700;color:#111827;line-height:1.3;">{len(events)} new event{'s' if len(events) != 1 else ''} from {safe_name}</h1>
          <p style="margin:0 0 16px;font-size:14px;color:#6b7280;">These events have been added to your Symbolos calendar.</p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border-radius:8px;overflow:hidden;">
            <tr><th style="padding:8px 12px;font-size:12px;color:#9ca3af;text-align:left;text-transform:uppercase;letter-spacing:0.05em;">Event</th>
                <th style="padding:8px 12px;font-size:12px;color:#9ca3af;text-align:left;text-transform:uppercase;letter-spacing:0.05em;">Date</th></tr>
            {event_rows}
          </table>
          <div style="text-align:center;margin-top:20px;">
            <a href="https://symbolos.ca" style="display:inline-block;background:#ED1B2F;color:#fff;font-size:14px;font-weight:600;text-decoration:none;padding:12px 28px;border-radius:8px;">View Calendar</a>
          </div>
        </td></tr>
        <tr><td style="background:#f9fafb;border:1px solid #e4e4e7;border-top:none;border-radius:0 0 12px 12px;padding:16px 28px;text-align:center;">
          <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
            You're receiving this because you subscribed to {safe_name} on Symbolos.<br/>
            Unsubscribe anytime from Settings.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

        import httpx
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "Symbolos <notifications@symbolos.ca>", "to": emails, "subject": subject, "html": html},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(f"Resend error for newsletter event notification: {resp.status_code} {resp.text}")
        else:
            logger.info(f"Newsletter event email sent to {len(emails)} subscribers of {source_name}")
    except Exception as e:
        logger.exception(f"Failed to send newsletter event notification: {e}")


# ── Pydantic schemas ────────────────────────────────────────────────────────

class NewsletterSourceOut(BaseModel):
    id: str
    name: str
    url: str
    feed_type: str = "rss"
    category: str = "general"
    logo_url: Optional[str] = None
    subscribed: bool = False          # enriched per-user
    email_muted: bool = False         # enriched per-user


class SubscriptionCreate(BaseModel):
    source_id: str
    calendar_sync: bool = True
    email_muted: bool = False


class SubscriptionUpdate(BaseModel):
    calendar_sync: Optional[bool] = None
    email_muted: Optional[bool] = None


class NewsletterEventOut(BaseModel):
    id: str
    source_id: str
    source_name: Optional[str] = None
    title: str
    description: Optional[str] = None
    date: str
    time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    link: Optional[str] = None


# ── Helper ──────────────────────────────────────────────────────────────────

def _ensure_tables(sb):
    """
    Lightweight check — if the tables don't exist the queries will fail
    gracefully and we return empty results. This avoids hard crashes during
    initial deployment before the migration runs.
    """
    pass


# ── Routes: Sources ─────────────────────────────────────────────────────────

@router.get("/sources", response_model=List[NewsletterSourceOut])
async def list_sources(
    category: Optional[str] = None,
    search: Optional[str] = None,
    current_user_id: str = Depends(get_current_user_id),
):
    """List all available newsletter sources, enriched with user subscription status."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()

        # Fetch all sources
        q = sb.table("newsletter_sources").select("*").order("name")
        if category:
            q = q.eq("category", category)
        sources_resp = q.execute()
        sources = sources_resp.data or []

        if search:
            term = search.lower()
            sources = [s for s in sources if term in s.get("name", "").lower()
                       or term in s.get("category", "").lower()]

        # Fetch user's subscriptions (with email_muted flag)
        subs_resp = (
            sb.table("newsletter_subscriptions")
            .select("source_id, email_muted")
            .eq("user_id", current_user_id)
            .execute()
        )
        sub_map = {s["source_id"]: s for s in (subs_resp.data or [])}

        result = []
        for s in sources:
            sub = sub_map.get(s["id"])
            result.append(NewsletterSourceOut(
                id=s["id"],
                name=s["name"],
                url=s.get("url", ""),
                feed_type=s.get("feed_type", "rss"),
                category=s.get("category", "general"),
                logo_url=s.get("logo_url"),
                subscribed=sub is not None,
                email_muted=sub.get("email_muted", False) if sub else False,
            ))
        return result

    except Exception as e:
        logger.error(f"list_sources error: {e}")
        return []


@router.get("/sources/categories")
async def list_source_categories(
    current_user_id: str = Depends(get_current_user_id),
):
    """Get distinct newsletter categories."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()
        resp = sb.table("newsletter_sources").select("category").execute()
        cats = sorted({r["category"] for r in (resp.data or []) if r.get("category")})
        return {"categories": cats}
    except Exception as e:
        logger.error(f"list_source_categories error: {e}")
        return {"categories": []}


# ── Routes: Subscriptions ───────────────────────────────────────────────────

@router.get("/subscriptions")
async def list_subscriptions(
    current_user_id: str = Depends(get_current_user_id),
):
    """List user's newsletter subscriptions with source info."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()
        resp = (
            sb.table("newsletter_subscriptions")
            .select("*, newsletter_sources(*)")
            .eq("user_id", current_user_id)
            .execute()
        )
        subs = resp.data or []
        result = []
        for sub in subs:
            src = sub.get("newsletter_sources") or {}
            result.append({
                "id": sub["id"],
                "source_id": sub["source_id"],
                "calendar_sync": sub.get("calendar_sync", True),
                "email_muted": sub.get("email_muted", False),
                "source_name": src.get("name", ""),
                "source_category": src.get("category", ""),
                "source_logo_url": src.get("logo_url"),
                "created_at": sub.get("created_at"),
            })
        return {"subscriptions": result}
    except Exception as e:
        logger.error(f"list_subscriptions error: {e}")
        return {"subscriptions": []}


@router.post("/subscriptions")
async def create_subscription(
    data: SubscriptionCreate,
    current_user_id: str = Depends(get_current_user_id),
):
    """Subscribe to a newsletter source."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()

        # Check source exists
        src_resp = sb.table("newsletter_sources").select("id, name, url").eq("id", data.source_id).execute()
        if not src_resp.data:
            raise HTTPException(status_code=404, detail="Newsletter source not found")
        source = src_resp.data[0]

        # Upsert subscription
        sb.table("newsletter_subscriptions").upsert({
            "user_id": current_user_id,
            "source_id": data.source_id,
            "calendar_sync": data.calendar_sync,
            "email_muted": data.email_muted,
        }, on_conflict="user_id,source_id").execute()

        return {"status": "subscribed", "source_id": data.source_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_subscription error: {e}")
        raise HTTPException(status_code=500, detail="Failed to subscribe")


@router.patch("/subscriptions/{source_id}")
async def update_subscription(
    source_id: str,
    data: SubscriptionUpdate,
    current_user_id: str = Depends(get_current_user_id),
):
    """Update subscription settings (e.g. toggle calendar sync)."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()
        updates = {}
        if data.calendar_sync is not None:
            updates["calendar_sync"] = data.calendar_sync
        if data.email_muted is not None:
            updates["email_muted"] = data.email_muted

        if updates:
            sb.table("newsletter_subscriptions").update(updates).eq(
                "user_id", current_user_id
            ).eq("source_id", source_id).execute()

        return {"status": "updated"}
    except Exception as e:
        logger.error(f"update_subscription error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update subscription")


@router.delete("/subscriptions/{source_id}")
async def delete_subscription(
    source_id: str,
    current_user_id: str = Depends(get_current_user_id),
):
    """Unsubscribe from a newsletter source."""
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()
        sb.table("newsletter_subscriptions").delete().eq(
            "user_id", current_user_id
        ).eq("source_id", source_id).execute()
        return {"status": "unsubscribed"}
    except Exception as e:
        logger.error(f"delete_subscription error: {e}")
        raise HTTPException(status_code=500, detail="Failed to unsubscribe")


# ── Routes: Newsletter Events ──────────────────────────────────────────────

@router.get("/events")
async def list_newsletter_events(
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Get all newsletter events for the user's subscribed sources
    (only sources with calendar_sync enabled).
    """
    await _require_mcgill_email(current_user_id)
    try:
        sb = get_supabase()

        # Get user's synced subscriptions
        subs_resp = (
            sb.table("newsletter_subscriptions")
            .select("source_id, newsletter_sources(name)")
            .eq("user_id", current_user_id)
            .eq("calendar_sync", True)
            .execute()
        )
        subs = subs_resp.data or []
        if not subs:
            return {"events": []}

        source_map = {}
        for sub in subs:
            sid = sub["source_id"]
            src = sub.get("newsletter_sources") or {}
            source_map[sid] = src.get("name", "Newsletter")

        source_ids = list(source_map.keys())

        # Fetch events for those sources
        events_resp = (
            sb.table("newsletter_events")
            .select("*")
            .in_("source_id", source_ids)
            .order("date", desc=False)
            .execute()
        )
        events = events_resp.data or []

        result = []
        for ev in events:
            result.append(NewsletterEventOut(
                id=ev["id"],
                source_id=ev["source_id"],
                source_name=source_map.get(ev["source_id"], "Newsletter"),
                title=ev.get("title", ""),
                description=ev.get("description"),
                date=ev.get("date", ""),
                time=ev.get("time"),
                end_time=ev.get("end_time"),
                location=ev.get("location"),
                link=ev.get("link"),
            ))
        return {"events": result}

    except Exception as e:
        logger.error(f"list_newsletter_events error: {e}")
        return {"events": []}


# ── Scraper ────────────────────────────────────────────────────────────────

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_strip = lambda html: re.sub(r'<[^>]+>', '', html).strip()
_THIS_YEAR = str(date.today().year)


def _parse_date_fuzzy(text: str) -> Optional[str]:
    """Extract a date from free-text. Returns YYYY-MM-DD or None."""
    text = text.strip().lower()
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r'(\w+)\s+(\d{1,2}),?\s*(\d{4})', text)
    if m and m.group(1) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text)
    if m and m.group(2) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    # "March 25" (no year) → assume current year
    m = re.search(r'(\w+)\s+(\d{1,2})', text)
    if m and m.group(1) in _MONTHS:
        return f"{_THIS_YEAR}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return None


def _parse_time_fuzzy(text: str) -> Optional[str]:
    """Extract a time from free-text. Returns HH:MM or None."""
    m = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)', text.lower())
    if m:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == 'pm' and h != 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        return f"{h:02d}:{mi:02d}"
    m = re.search(r'(\d{1,2}):(\d{2})', text)
    if m and 0 <= int(m.group(1)) <= 23:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    return None


def _is_safe_url(url: str) -> bool:
    """Block SSRF: reject private/internal IPs and non-HTTP schemes.

    A symbolic hostname that isn't itself a private-IP literal can still
    resolve to one (DNS rebinding, or just pointing a domain at 127.0.0.1 /
    a cloud metadata address), so resolve it here and check every address
    it comes back with rather than trusting the string form of the host.
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    if not hostname:
        return False
    # Block obvious internal hostnames
    if hostname in ("localhost", "metadata.google.internal") or hostname.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_global
    except ValueError:
        pass
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(addr[4][0]).is_global for addr in addrs)


def _fetch_page(url: str, _redirects_remaining: int = 5) -> Optional[str]:
    """Fetch a URL, return HTML or None.

    Redirects are followed manually (not via httpx's follow_redirects) so
    each hop's target is re-validated through _is_safe_url — a URL that
    passes the safety check can still redirect straight to an internal
    address on the next hop, and blind auto-following would fetch it anyway.
    """
    import httpx
    if not _is_safe_url(url):
        logger.warning(f"Blocked unsafe URL: {url}")
        return None
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=False, headers={
            "User-Agent": "Symbolos-Bot/1.0 (McGill student calendar tool)",
        })
        if resp.is_redirect:
            if _redirects_remaining <= 0:
                logger.warning(f"Too many redirects fetching {url}")
                return None
            location = resp.headers.get("location")
            if not location:
                return None
            next_url = str(httpx.URL(url).join(location))
            return _fetch_page(next_url, _redirects_remaining - 1)
        return resp.text if resp.status_code == 200 else None
    except Exception as e:
        logger.warning(f"Fetch {url} failed: {e}")
        return None


# ── Site-specific parsers ──────────────────────────────────────────────────

def _parse_channels_events(html: str, base_url: str) -> list:
    """
    Parse McGill Channels Events / Student Services Events pages.
    Drupal 7 pattern: div.channel_event.views-row with structured date spans.
    """
    events = []
    # Split by event rows
    rows = re.findall(
        r'<div[^>]*class="[^"]*(?:channel_event|channel-item)[^"]*views-row[^"]*"[^>]*>([\s\S]*?)(?=<div[^>]*class="[^"]*views-row|$)',
        html, re.IGNORECASE
    )
    if not rows:
        # Student services uses slightly different structure
        rows = re.findall(
            r'<div[^>]*class="[^"]*node-channel-event[^"]*"[^>]*>([\s\S]*?)(?=<div[^>]*class="[^"]*node-channel-event|<div[^>]*class="[^"]*view-footer)',
            html, re.IGNORECASE
        )

    for block in rows:
        # Title: h2 > a
        title_match = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>', block)
        if not title_match:
            title_match = re.search(r'<h2[^>]*>([^<]+)</h2>', block)
        if not title_match:
            continue

        if title_match.lastindex >= 2:
            link = title_match.group(1)
            title = title_match.group(2).strip()
        else:
            link = None
            title = title_match.group(1).strip()

        if not title or len(title) < 3:
            continue

        # Make link absolute
        if link and not link.startswith("http"):
            link = f"https://www.mcgill.ca{link}"

        # Date from structured spans: <span class="month">Mar</span> <span class="day">23</span> <span class="year">2026</span>
        month_m = re.search(r'<span[^>]*class="month"[^>]*>([^<]+)</span>', block)
        day_m = re.search(r'<span[^>]*class="day"[^>]*>(\d+)', block)
        year_m = re.search(r'<span[^>]*class="year"[^>]*>(\d{4})</span>', block)

        d = None
        if month_m and day_m:
            mon = month_m.group(1).strip().lower().rstrip(',')
            day = day_m.group(1).strip()
            yr = year_m.group(1) if year_m else _THIS_YEAR
            if mon in _MONTHS:
                d = f"{yr}-{_MONTHS[mon]:02d}-{int(day):02d}"

        # Fallback: "Monday, March 23, 2026" in div.date
        if not d:
            date_div = re.search(r'<div[^>]*class="date"[^>]*>([^<]+)</div>', block)
            if date_div:
                d = _parse_date_fuzzy(date_div.group(1))

        if not d:
            # Try fuzzy from whole block text
            d = _parse_date_fuzzy(_strip(block))

        if not d:
            continue

        # Time from <span class="time">
        time_spans = re.findall(r'<span[^>]*class="time"[^>]*>([^<]+)</span>', block)
        t = _parse_time_fuzzy(time_spans[0]) if time_spans else None
        end_t = _parse_time_fuzzy(time_spans[1]) if len(time_spans) > 1 else None

        # Fallback: "13:00 to 14:30" in field-item
        if not t:
            time_field = re.search(r'(?:Time|Heure)[:\s]*</div>\s*<div[^>]*>\s*<div[^>]*>([^<]+)</div>', block, re.IGNORECASE)
            if time_field:
                times_text = time_field.group(1)
                t = _parse_time_fuzzy(times_text)
                end_match = re.search(r'to\s+(\d{1,2}:\d{2})', times_text)
                if end_match:
                    end_t = end_match.group(1)

        # Location
        loc = None
        loc_m = re.search(r'field-name-field-location[\s\S]*?<div[^>]*class="field-item[^"]*"[^>]*>([\s\S]*?)</div>', block)
        if loc_m:
            loc = _strip(loc_m.group(1))[:100]

        # Description
        desc_m = re.search(r'field-name-body[\s\S]*?<div[^>]*class="field-item[^"]*"[^>]*>([\s\S]*?)</div>', block)
        desc = _strip(desc_m.group(1))[:200] if desc_m else None

        events.append({
            "title": title,
            "date": d,
            "time": t,
            "end_time": end_t,
            "location": loc,
            "link": link or base_url,
            "description": desc,
        })

    return events


def _parse_reporter(html: str) -> list:
    """
    Parse McGill Reporter (WordPress).
    Articles in <article> with h3 > a title and div.entry-footer.date.
    """
    events = []
    articles = re.findall(r'<article[^>]*>([\s\S]*?)</article>', html, re.IGNORECASE)

    for block in articles:
        # Title: h3 > a
        title_m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>', block)
        if not title_m:
            continue
        link = title_m.group(1)
        title = title_m.group(2).strip()

        # Date: div.entry-footer.date
        date_m = re.search(r'<div[^>]*class="entry-footer[^"]*date"[^>]*>([^<]+)</div>', block)
        if not date_m:
            date_m = re.search(r'class="date"[^>]*>([^<]+)<', block)
        d = _parse_date_fuzzy(date_m.group(1)) if date_m else None
        if not d:
            continue

        # Category
        cat_m = re.search(r'class="section[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>', block)
        category = cat_m.group(1).strip() if cat_m else None

        # Excerpt
        desc_m = re.search(r'<div[^>]*class="entry-content"[^>]*>([\s\S]*?)</div>', block)
        desc = _strip(desc_m.group(1))[:200] if desc_m else None

        events.append({
            "title": title,
            "date": d,
            "time": None,
            "end_time": None,
            "location": None,
            "link": link,
            "description": desc or category,
        })

    return events


def _parse_generic(html: str, base_url: str) -> list:
    """
    Generic fallback parser — looks for headings with nearby dates.
    Works for newsletter archive pages, blogs, etc.
    """
    events = []
    clean = re.sub(r'<(script|style|nav|footer|header)[^>]*>[\s\S]*?</\1>', '', html, flags=re.IGNORECASE)

    # Strategy: find all <h2>/<h3>/<h4> headings and look for dates nearby
    pattern = r'<(h[2-4])[^>]*>([\s\S]*?)</\1>([\s\S]{0,400}?)(?=<h[2-4]|$)'
    for _, title_html, body_html in re.findall(pattern, clean, re.IGNORECASE):
        title = _strip(title_html)
        if not title or len(title) < 5 or len(title) > 200:
            continue

        body_text = _strip(body_html)
        combined = f"{title} {body_text}"
        d = _parse_date_fuzzy(combined)
        if not d:
            continue

        t = _parse_time_fuzzy(body_text)
        link_m = re.search(r'href="(https?://[^"]+)"', title_html + body_html)
        link = link_m.group(1) if link_m else base_url

        events.append({
            "title": title,
            "date": d,
            "time": t,
            "end_time": None,
            "location": None,
            "link": link,
            "description": body_text[:200] if body_text else None,
        })

    # Fallback: date-stamped list items
    if not events:
        for item_html in re.findall(r'<li[^>]*>([\s\S]*?)</li>', clean, re.IGNORECASE):
            text = _strip(item_html)
            d = _parse_date_fuzzy(text)
            if not d:
                continue
            title = re.split(r'[.!?\n]', text)[0].strip()[:150]
            if len(title) < 5:
                continue
            link_m = re.search(r'href="(https?://[^"]+)"', item_html)
            events.append({
                "title": title, "date": d, "time": _parse_time_fuzzy(text),
                "end_time": None, "location": None,
                "link": link_m.group(1) if link_m else base_url,
                "description": text[:200] if len(text) > 30 else None,
            })

    return events[:50]


def _scrape_source(url: str, max_pages: int = 5) -> list:
    """Route a URL to the appropriate parser. Follows pagination for event pages."""
    is_event_page = "channels" in url or "studentservices" in url or "/cle/" in url

    if is_event_page:
        all_events = []
        for page in range(max_pages):
            page_url = f"{url}?page={page}" if page > 0 else url
            html = _fetch_page(page_url)
            if not html:
                break
            events = _parse_channels_events(html, url)
            if not events:
                break  # No more events on this page
            all_events.extend(events)
            # Check if there's a next page link
            if not re.search(r'class="[^"]*pager-next[^"]*"', html):
                break
        return all_events
    else:
        html = _fetch_page(url)
        if not html:
            return []
        if "reporter.mcgill.ca" in url:
            return _parse_reporter(html)
        return _parse_generic(html, url)


@router.post("/scrape")
async def scrape_newsletter_sources(request: Request):
    """
    Cron-triggered endpoint: scrape all newsletter sources for new events.
    Protected by X-Cron-Secret header.
    """
    if not settings.CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET not configured")
    # Vercel cron sends Authorization: Bearer <CRON_SECRET>
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token or "", settings.CRON_SECRET):
        raise HTTPException(status_code=401, detail="Invalid cron secret")

    sb = get_supabase()
    sources_resp = sb.table("newsletter_sources").select("id, name, url, feed_type").execute()
    sources = sources_resp.data or []

    total_new = 0
    source_results = {}

    for src in sources:
        source_id = src["id"]
        source_name = src["name"]
        url = src.get("url", "")

        if not url:
            continue

        try:
            scraped = _scrape_source(url)
        except Exception as e:
            logger.error(f"Scrape error for {source_name}: {e}")
            continue

        today_str = date.today().isoformat()
        new_events = []
        for ev in scraped:
            # Skip past events
            if ev.get("date", "") < today_str:
                continue
            # Generate stable external_id for dedup
            ext_id = hashlib.sha256(f"{ev['title']}::{ev['date']}::{ev.get('time','')}".encode()).hexdigest()[:24]

            row = {
                "source_id": source_id,
                "title": ev["title"],
                "description": ev.get("description"),
                "date": ev["date"],
                "time": ev.get("time"),
                "end_time": ev.get("end_time"),
                "location": ev.get("location"),
                "link": ev.get("link"),
                "external_id": ext_id,
            }

            try:
                sb.table("newsletter_events").upsert(
                    row, on_conflict="source_id,external_id"
                ).execute()
                new_events.append(ev)
            except Exception as e:
                logger.debug(f"Event upsert skipped: {e}")

        if new_events:
            total_new += len(new_events)
            source_results[source_name] = len(new_events)
            # Notify subscribers about new events
            try:
                _notify_newsletter_subscribers(source_id, source_name, new_events)
            except Exception as e:
                logger.error(f"Newsletter notification error for {source_name}: {e}")

    logger.info(f"Newsletter scrape complete: {total_new} events from {len(source_results)} sources")
    return {
        "status": "ok",
        "total_new_events": total_new,
        "sources": source_results,
    }
