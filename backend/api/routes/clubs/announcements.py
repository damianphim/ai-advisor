"""Club announcements."""
from fastapi import HTTPException, Depends
import logging

from ...utils.supabase_client import get_supabase
from ...auth import get_current_user_id
from ._router import router
from .permissions import is_club_owner_or_admin
from .schemas import ClubAnnouncementCreate

logger = logging.getLogger(__name__)


@router.post("/{club_id}/announcements")
async def create_club_announcement(club_id: str, body: ClubAnnouncementCreate, current_user_id: str = Depends(get_current_user_id)):
    """Create a club announcement. Only club owner or Managers can create."""
    from ...utils.moderation import require_not_suspended
    require_not_suspended(current_user_id)
    if not is_club_owner_or_admin(club_id, current_user_id):
        raise HTTPException(status_code=403, detail="Only club owner or admins can create announcements")
    try:
        supabase = get_supabase()
        row = {
            "club_id": club_id,
            "title": body.title,
            "body": body.body,
            "created_by": current_user_id,
        }
        if body.event_id:
            row["event_id"] = body.event_id
        if body.join_link:
            row["join_link"] = body.join_link
        result = supabase.table("club_announcements").insert(row).execute()

        from .email import _notify_club_members_announcement

        # Send email to all club members
        club_row = supabase.table("clubs").select("name").eq("id", club_id).execute()
        club_name = club_row.data[0]["name"] if club_row.data else "Club"

        # If event attached, fetch its details for the email
        event_data = None
        if body.event_id:
            try:
                ev_row = supabase.table("club_events").select("title, date, time, location").eq("id", body.event_id).execute()
                if ev_row.data:
                    event_data = ev_row.data[0]
            except Exception:
                pass

        _notify_club_members_announcement(supabase, club_id, club_name, body.title, body.body, event=event_data, join_link=body.join_link)

        return {"success": True, "announcement": result.data[0] if result.data else None}
    except Exception as e:
        logger.exception(f"Error creating club announcement: {e}")
        raise HTTPException(status_code=500, detail="Failed to create announcement")


@router.delete("/{club_id}/announcements/{ann_id}")
async def delete_club_announcement(club_id: str, ann_id: str, current_user_id: str = Depends(get_current_user_id)):
    """Delete a club announcement. Only club owner or Managers can delete."""
    if not is_club_owner_or_admin(club_id, current_user_id):
        raise HTTPException(status_code=403, detail="Only club owner or admins can delete announcements")
    try:
        supabase = get_supabase()
        supabase.table("club_announcements").delete().eq("id", ann_id).eq("club_id", club_id).execute()
        return {"success": True}
    except Exception as e:
        logger.exception(f"Error deleting club announcement: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete announcement")
