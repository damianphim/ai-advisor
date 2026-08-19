"""Club member roster and role management (Owner / Manager / Member)."""
from fastapi import HTTPException, Request, Depends
import logging

from ...utils.supabase_client import get_supabase
from ...auth import get_current_user_id
from ._router import router
from .helpers import display_name_from_email
from .permissions import is_admin_user, is_club_owner_or_admin

logger = logging.getLogger(__name__)


@router.get("/{club_id}/members")
async def get_club_members(club_id: str, current_user_id: str = Depends(get_current_user_id)):
    """Get all members of a club. Any club member can view the list."""
    supabase = get_supabase()

    # Check if caller is a member (or owner/admin/global admin)
    is_admin_or_owner = is_club_owner_or_admin(club_id, current_user_id)
    membership_check = supabase.table("user_clubs").select("user_id, role").eq("user_id", current_user_id).eq("club_id", club_id).execute()
    is_member = bool(membership_check.data)
    if not is_member and not is_admin_or_owner:
        raise HTTPException(status_code=403, detail="Only club members can view the member list")

    # Determine caller's role
    club_result = supabase.table("clubs").select("created_by").eq("id", club_id).execute()
    owner_id = club_result.data[0].get("created_by") if club_result.data else None

    # Stale-owner reassignment used to happen here as a side effect of this
    # read-only GET, letting any Member become Owner just by viewing the
    # member list once the real Owner's profile row was gone. Orphaned-owner
    # recovery, if still needed, belongs in a separate admin-gated endpoint
    # with deterministic successor selection — not a side effect of a GET.

    if current_user_id == owner_id:
        caller_role = "owner"
    elif is_admin_or_owner:
        caller_role = "admin"
    elif membership_check.data and membership_check.data[0].get("role") == "admin":
        caller_role = "admin"
    else:
        caller_role = "member"

    try:
        memberships = supabase.table("user_clubs").select("user_id, role").eq("club_id", club_id).execute()
    except Exception as e:
        logger.exception(f"Error fetching club members: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch members")

    members = []
    for m in (memberships.data or []):
        profile = {"id": m["user_id"], "role": m.get("role") or "member"}
        if m["user_id"] == owner_id:
            profile["role"] = "owner"
        try:
            p = supabase.table("users").select("email").eq("id", m["user_id"]).execute()
            if p.data:
                email = p.data[0].get("email", "") or ""
                # Always show the email-derived first name to club staff — never
                # the user's custom username (privacy: usernames are only for
                # the user's own settings + forum posts).
                profile["name"] = display_name_from_email(email)
                profile["email"] = email
        except Exception:
            pass
        members.append(profile)
    # Sort: owner first, then admins, then members
    role_order = {"owner": 0, "admin": 1, "member": 2}
    members.sort(key=lambda m: role_order.get(m["role"], 2))
    return {"members": members, "count": len(members), "can_manage": is_admin_or_owner or caller_role in ("owner", "admin"), "caller_role": caller_role}


@router.patch("/{club_id}/members/{member_user_id}/role")
async def update_member_role(club_id: str, member_user_id: str, req: Request, current_user_id: str = Depends(get_current_user_id)):
    """Set a member's role. Accepts { "role": "admin"|"member"|"owner" }."""
    supabase = get_supabase()

    # Get club owner
    club_result = supabase.table("clubs").select("created_by").eq("id", club_id).execute()
    if not club_result.data:
        raise HTTPException(status_code=404, detail="Club not found")
    owner_id = club_result.data[0].get("created_by")

    # Determine caller's role
    caller_is_owner = current_user_id == owner_id
    caller_is_global_admin = is_admin_user(current_user_id)
    caller_membership = supabase.table("user_clubs").select("role").eq("user_id", current_user_id).eq("club_id", club_id).execute()
    caller_is_club_admin = caller_membership.data and caller_membership.data[0].get("role") == "admin"

    if not (caller_is_owner or caller_is_global_admin or caller_is_club_admin):
        raise HTTPException(status_code=403, detail="Only club owner or admins can change roles")

    # Parse requested role
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    new_role = body.get("role") if isinstance(body, dict) else None
    if new_role not in ("admin", "member", "owner"):
        raise HTTPException(status_code=400, detail="Role must be 'admin', 'member', or 'owner'")

    # Can't change the owner's role (unless transferring ownership TO someone else)
    target_is_owner = member_user_id == owner_id
    if target_is_owner and new_role != "owner":
        raise HTTPException(status_code=400, detail="Cannot change the club owner's role directly. Transfer ownership instead.")

    # Only the current owner can transfer ownership
    if new_role == "owner":
        if not caller_is_owner:
            raise HTTPException(status_code=403, detail="Only the current owner can transfer ownership")
        # Transfer: promote target to owner, demote current owner to admin
        supabase.table("user_clubs").update({"role": "owner"}).eq("user_id", member_user_id).eq("club_id", club_id).execute()
        supabase.table("user_clubs").update({"role": "admin"}).eq("user_id", current_user_id).eq("club_id", club_id).execute()
        supabase.table("clubs").update({"created_by": member_user_id}).eq("id", club_id).execute()
        return {"success": True, "role": "owner"}

    # DELIBERATE: a Manager may demote another Manager. This is not an
    # oversight — it exists so a club isn't stranded when the Owner goes
    # inactive and the remaining Managers need to reorganise themselves.
    # remove_club_member allows the mirror case for the same reason.
    #
    # The Owner stays protected regardless: they cannot be demoted (guarded
    # above), cannot be removed (remove_club_member), ownership transfer
    # requires caller_is_owner, and deleting a club is platform-admin only.
    # So the blast radius of a rogue Manager is the manager roster, never the
    # club itself.
    #
    # There used to be a block here reading "Admins cannot change other admins
    # or the owner" — but it only ever tested target_is_owner (already
    # unreachable by this point, since the owner-guard above returns first) and
    # loaded target_role into a variable it never used. It was dead code
    # contradicting the intended behaviour, plus a wasted Supabase round-trip
    # on every role change. Removed rather than implemented; see
    # test_club_role_permissions.py for the behaviour this locks in.

    # Get current role of target
    membership = supabase.table("user_clubs").select("role").eq("user_id", member_user_id).eq("club_id", club_id).execute()
    if not membership.data:
        raise HTTPException(status_code=404, detail="Member not found")

    supabase.table("user_clubs").update({"role": new_role}).eq("user_id", member_user_id).eq("club_id", club_id).execute()
    return {"success": True, "role": new_role}


@router.delete("/{club_id}/members/{member_user_id}")
async def remove_club_member(club_id: str, member_user_id: str, current_user_id: str = Depends(get_current_user_id)):
    """Remove a member from a club. Owner/admins can remove (admins can remove members and other admins, but not owner)."""
    if not is_club_owner_or_admin(club_id, current_user_id):
        raise HTTPException(status_code=403, detail="Only club owner or admins can remove members")
    supabase = get_supabase()
    # Cannot remove the owner
    club_result = supabase.table("clubs").select("created_by").eq("id", club_id).execute()
    if club_result.data and club_result.data[0].get("created_by") == member_user_id:
        raise HTTPException(status_code=400, detail="Cannot remove the club owner")
    supabase.table("user_clubs").delete().eq("user_id", member_user_id).eq("club_id", club_id).execute()
    return {"success": True}
