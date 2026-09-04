"""Club events."""
from fastapi import HTTPException, Depends
import logging

from ...utils.supabase_client import get_supabase
from ...auth import get_current_user_id
from ._router import router
from .permissions import is_club_owner_or_admin
from .schemas import ClubEventCreate

logger = logging.getLogger(__name__)


@router.post("/{club_id}/events")
async def create_club_event(club_id: str, body: ClubEventCreate, current_user_id: str = Depends(get_current_user_id)):
    """Create a club event. Only club owner or Managers can create."""
    from ...utils.moderation import require_not_suspended
    require_not_suspended(current_user_id)
    if not is_club_owner_or_admin(club_id, current_user_id):
        raise HTTPException(status_code=403, detail="Only club owner or admins can create events")
    try:
        supabase = get_supabase()
        row = {
            "club_id": club_id,
            "title": body.title,
            "description": body.description,
            "date": body.date,
            "time": body.time,
            "end_time": body.end_time,
            "location": body.location,
            "recurrence": body.recurrence,
            "created_by": current_user_id,
        }
        if body.join_link:
            row["join_link"] = body.join_link
        result = supabase.table("club_events").insert(row).execute()

        # Email all club members about the new event (non-blocking)
        try:
            from .email import _notify_club_members_new_event
            club_result = supabase.table("clubs").select("name").eq("id", club_id).execute()
            club_name = club_result.data[0]["name"] if club_result.data else "Your Club"
            _notify_club_members_new_event(
                supabase, club_id, club_name,
                title=body.title, date=body.date, time=body.time,
                location=body.location, description=body.description,
                join_link=body.join_link,
            )
        except Exception as e:
            logger.warning(f"Failed to send event notification emails: {e}")

        return {"success": True, "event": result.data[0] if result.data else None}
    except Exception as e:
        logger.exception(f"Error creating club event: {e}")
        raise HTTPException(status_code=500, detail="Failed to create event")


@router.delete("/{club_id}/events/{event_id}")
async def delete_club_event(club_id: str, event_id: str, current_user_id: str = Depends(get_current_user_id)):
    """Delete a club event. Only club owner or Managers can delete."""
    if not is_club_owner_or_admin(club_id, current_user_id):
        raise HTTPException(status_code=403, detail="Only club owner or admins can delete events")
    try:
        supabase = get_supabase()
        supabase.table("club_events").delete().eq("id", event_id).eq("club_id", club_id).execute()
        return {"success": True}
    except Exception as e:
        logger.exception(f"Error deleting club event: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete event")
