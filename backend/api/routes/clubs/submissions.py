"""Club Submissions — pending requests to create a new Club, plus admin
moderation (cron-secret-gated) and the email-link approval flow."""
from fastapi import HTTPException, Request, Depends
from fastapi.responses import HTMLResponse
from html import escape
import logging

from ...utils.supabase_client import get_supabase
from ...auth import get_current_user_id, require_mcgill_email
from ...config import settings
from ._router import router
from .permissions import is_admin_user, verify_admin_token
from .schemas import ClubSubmission

logger = logging.getLogger(__name__)


@router.post("/submit")
async def submit_club(submission: ClubSubmission, current_user_id: str = Depends(get_current_user_id)):
    """Submit a new club for admin review."""
    from html import escape as _esc
    from ...utils.verified_user import is_email_verified
    from ...utils.anomaly import record_action
    from ...utils.moderation import require_not_suspended
    require_not_suspended(current_user_id)
    require_mcgill_email(current_user_id)
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to submit a club."})
    record_action(current_user_id, "club_submit")
    try:
        supabase = get_supabase()

        # SEC FIX #9: escape user-controlled free-text on write so even if
        # a future renderer treats these as HTML it can't smuggle markup.
        insert_result = supabase.table("club_submissions").insert({
            "name":             _esc(submission.name or ""),
            "description":      _esc(submission.description or ""),
            "category":         submission.category or "Social",
            "contact_email":    submission.contact_email,
            "website_url":      submission.website_url,
            "meeting_schedule": _esc(submission.meeting_schedule or "") if submission.meeting_schedule else None,
            "location":         _esc(submission.location or "") if submission.location else None,
            "is_private":       submission.is_private,
            "submitted_by":     current_user_id,
            "executive_emails": submission.executive_emails,
            "join_instructions":_esc(submission.join_instructions or "") if submission.join_instructions else None,
            "application_url":  submission.application_url,
            "status":           "pending",
        }).execute()

        # Generate tokens and send approval email to admins
        from .email import _generate_action_tokens, _send_admin_club_email
        email_sent = False
        email_error = None
        token_generated = False
        if insert_result.data:
            sub_id = insert_result.data[0].get("id", "")
            # Generate tokens directly here so we can track failures
            try:
                _generate_action_tokens(sub_id)
                token_generated = True
            except Exception as token_err:
                email_error = f"Token generation failed: {token_err}"
                logger.exception(f"Failed to generate action tokens: {token_err}")

            resend_debug = None
            if token_generated:
                try:
                    # Re-fetch the submission with tokens
                    updated = supabase.table("club_submissions").select("*").eq("id", sub_id).execute()
                    if updated.data:
                        sub_data = updated.data[0]
                        _send_admin_club_email(sub_data)
                        email_sent = True
                        resend_debug = sub_data.get("_resend_response")
                except Exception as email_err:
                    email_error = f"Email send failed: {email_err}"
                    logger.exception(f"Failed to send admin email: {email_err}")

        return {"success": True, "email_sent": email_sent, "token_generated": token_generated, "email_error": email_error, "resend_debug": resend_debug, "admin_emails": settings.ADMIN_EMAILS, "version": "v5", "message": "Club submission received. We'll review it shortly!"}
    except Exception as e:
        logger.exception(f"Error submitting club: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit club")


@router.get("/admin/submissions")
async def admin_list_submissions(req: Request, status_filter: str = "pending"):
    """List club submissions for admin review."""
    verify_admin_token(req)
    try:
        supabase = get_supabase()
        query = supabase.table("club_submissions").select("*").order("created_at", desc=True)
        if status_filter and status_filter != "all":
            query = query.eq("status", status_filter)
        result = query.execute()
        return {"submissions": result.data or []}
    except Exception as e:
        logger.exception(f"Error listing submissions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list submissions")


@router.patch("/admin/submissions/{submission_id}")
async def admin_review_submission(submission_id: str, req: Request):
    """Approve or reject a club submission. On approve, create the club."""
    verify_admin_token(req)
    try:
        body = await req.json()
        new_status = body.get("status")
        if new_status not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="Status must be 'approved' or 'rejected'")

        supabase = get_supabase()

        # Get the submission
        sub_result = supabase.table("club_submissions").select("*").eq("id", submission_id).execute()
        if not sub_result.data:
            raise HTTPException(status_code=404, detail="Submission not found")
        submission = sub_result.data[0]

        # Update status
        supabase.table("club_submissions").update({"status": new_status}).eq("id", submission_id).execute()

        # If approved, create the actual club and auto-add owner as member
        if new_status == "approved":
            club_result = supabase.table("clubs").insert({
                "name": submission["name"],
                "description": submission["description"],
                "category": submission.get("category") or "Social",
                "contact_email": submission.get("contact_email"),
                "website_url": submission.get("website_url"),
                "meeting_schedule": submission.get("meeting_schedule"),
                "location": submission.get("location"),
                "is_private": submission.get("is_private", False),
                "is_verified": True,
                "created_by": submission.get("submitted_by"),
                "executive_emails": submission.get("executive_emails"),
            }).execute()
            new_club = club_result.data[0] if club_result.data else None
            if new_club and submission.get("submitted_by"):
                supabase.table("user_clubs").insert({
                    "user_id": submission["submitted_by"],
                    "club_id": new_club["id"],
                    "role": "owner",
                    "calendar_synced": True,
                }).execute()

        return {"success": True, "status": new_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error reviewing submission: {e}")
        raise HTTPException(status_code=500, detail="Failed to review submission")


# ── Email-based club approval (no auth required — token IS the auth) ─────────

def _action_result_html(title: str, message: str, is_success: bool) -> str:
    """Generate a simple HTML result page for the email action."""
    bg = "#dcfce7" if is_success else "#fee2e2"
    color = "#166534" if is_success else "#dc2626"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;">
  <div style="max-width:440px;width:100%;margin:40px auto;padding:32px;background:#fff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,0.08);text-align:center;">
    <div style="display:inline-block;background:{bg};color:{color};font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;padding:5px 12px;border-radius:20px;margin-bottom:16px;">{escape(title)}</div>
    <p style="margin:0;font-size:16px;color:#374151;line-height:1.6;">{message}</p>
    <a href="https://symbolos.ca" style="display:inline-block;margin-top:20px;background:#ED1B2F;color:#fff;font-size:14px;font-weight:600;text-decoration:none;padding:10px 24px;border-radius:8px;">Go to Symbolos</a>
  </div>
</body></html>"""


@router.get("/admin/action")
async def admin_email_action(token: str):
    """
    Process an approve/reject action from an email link.
    No auth required — the HMAC-signed token is the credential.
    Returns an HTML page with the result.
    """
    from .email import _verify_action_token, _send_submitter_notification_email

    logger.info(f"Admin action called with token: {token[:60]}...")
    submission_id, action = _verify_action_token(token)
    if not submission_id:
        return HTMLResponse(_action_result_html(
            "Link Expired",
            "This link has expired or is invalid. Please check your email for a newer link.",
            False,
        ))

    try:
        supabase = get_supabase()

        # Get the submission
        sub_result = supabase.table("club_submissions").select("*").eq("id", submission_id).execute()
        if not sub_result.data:
            return HTMLResponse(_action_result_html(
                "Not Found",
                "This club submission was not found. It may have been deleted.",
                False,
            ))

        submission = sub_result.data[0]

        # Check if already processed
        if submission.get("status") != "pending":
            current = submission["status"].capitalize()
            return HTMLResponse(_action_result_html(
                "Already Processed",
                f"This club submission has already been <strong>{current}</strong>.",
                False,
            ))

        # If approved, create the actual club BEFORE updating status
        if action == "approved":
            club_data = {
                "name": submission["name"],
                "description": submission["description"],
                "category": submission.get("category") or "Social",
                "contact_email": submission.get("contact_email"),
                "website_url": submission.get("website_url"),
                "meeting_schedule": submission.get("meeting_schedule"),
                "location": submission.get("location"),
                "is_private": submission.get("is_private", False),
                "is_verified": True,
                "created_by": submission.get("submitted_by"),
            }
            if submission.get("executive_emails"):
                club_data["executive_emails"] = submission["executive_emails"]
            if submission.get("application_url"):
                club_data["application_url"] = submission["application_url"]
            if submission.get("join_instructions"):
                club_data["join_instructions"] = submission["join_instructions"]
            logger.info(f"Inserting club: {club_data}")
            club_result = supabase.table("clubs").insert(club_data).execute()
            new_club = club_result.data[0] if club_result.data else None
            logger.info(f"Club inserted successfully: {submission['name']}")
            # Auto-add owner as member
            if new_club and submission.get("submitted_by"):
                supabase.table("user_clubs").insert({
                    "user_id": submission["submitted_by"],
                    "club_id": new_club["id"],
                    "role": "owner",
                    "calendar_synced": True,
                }).execute()
                logger.info(f"Owner auto-added as member for club: {submission['name']}")

        # Update status only after successful club creation
        supabase.table("club_submissions").update({"status": action}).eq("id", submission_id).execute()

        # Notify the submitter (non-critical)
        contact_email = submission.get("contact_email")
        if contact_email:
            try:
                _send_submitter_notification_email(contact_email, submission["name"], action)
            except Exception as email_err:
                logger.exception(f"Failed to send submitter notification: {email_err}")

        if action == "approved":
            return HTMLResponse(_action_result_html(
                "Club Approved",
                f"<strong>{escape(submission['name'])}</strong> has been approved and is now live on Symbolos!",
                True,
            ))
        else:
            return HTMLResponse(_action_result_html(
                "Club Rejected",
                f"<strong>{escape(submission['name'])}</strong> has been rejected.",
                True,
            ))

    except Exception as e:
        logger.exception(f"Error processing email action: {e}")
        return HTMLResponse(_action_result_html(
            "Error",
            f"Something went wrong: {e}",
            False,
        ))
