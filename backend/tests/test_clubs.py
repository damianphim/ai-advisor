"""
Characterization tests for backend/api/routes/clubs.py.

These pin down CURRENT behavior before clubs.py is refactored (see
docs/adr/0001-incremental-test-first-refactor.md). They are not a spec of
what's "correct" — some of what's tested here is just what the code happens
to do today, recorded so a refactor doesn't change it silently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth


ADMIN_ID = "65ad96d2-1704-4ff2-b661-42626f153fe8"  # hardcoded in clubs.ADMIN_USER_IDS


@pytest.fixture(autouse=True)
def no_resend(monkeypatch):
    """clubs.py emails are no-ops when RESEND_API_KEY is falsy — keep these
    tests hermetic regardless of how the env happens to be configured."""
    from api import config
    monkeypatch.setattr(config.settings, "RESEND_API_KEY", "")


# ── list_clubs ────────────────────────────────────────────────────────────────

def test_list_clubs_requires_auth(client):
    resp = client.get("/api/clubs")
    assert resp.status_code == 401


def test_list_clubs_shows_private_but_hides_unverified_and_strips_pii(client, fake_supabase):
    # "Private" means join-by-application only, not hidden — verified
    # private clubs should be just as discoverable as public ones. Only
    # unverified (not-yet-approved) clubs are excluded.
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "Public Club", "is_verified": True, "is_private": False,
         "contact_email": "secret@mcgill.ca", "category": "Social"},
        {"id": "c2", "name": "Private Club", "is_verified": True, "is_private": True,
         "contact_email": "hidden@mcgill.ca", "category": "Social"},
        {"id": "c3", "name": "Unverified Club", "is_verified": False, "is_private": False},
    ])
    resp = client.get("/api/clubs", headers=auth("user-1"))
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["clubs"]}
    assert names == {"Public Club", "Private Club"}
    assert all("contact_email" not in c for c in body["clubs"])
    private_club = next(c for c in body["clubs"] if c["name"] == "Private Club")
    assert private_club["is_private"] is True
    assert body["clubs"][0]["subscriber_count"] == 0


def test_list_clubs_sets_public_cache_headers(client, fake_supabase):
    fake_supabase.set_table("clubs", [])
    resp = client.get("/api/clubs", headers=auth("user-1"))
    assert "public" in resp.headers["Cache-Control"]
    assert "s-maxage=60" in resp.headers["Cache-Control"]


def test_list_clubs_search_is_literal_not_wildcard(client, fake_supabase):
    """Search escapes %, _ and backslash so a search for '_' doesn't act as
    a single-char wildcard matching every club name."""
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "AI_Club", "is_verified": True, "is_private": False},
        {"id": "c2", "name": "Random Club", "is_verified": True, "is_private": False},
    ])
    resp = client.get("/api/clubs", params={"search": "_"}, headers=auth("user-1"))
    body = resp.json()
    assert [c["name"] for c in body["clubs"]] == ["AI_Club"]


# ── get_starter_clubs ─────────────────────────────────────────────────────────

def test_starter_clubs_matches_major_keyword(client, fake_supabase):
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "HackMcGill", "is_verified": True},
        {"id": "c2", "name": "McGill Debate Society", "is_verified": True},
    ])
    resp = client.get(
        "/api/clubs/starter", params={"user_id": "user-1", "major": "Software Engineering"},
        headers=auth("user-1"),
    )
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["starter_clubs"]]
    assert "HackMcGill" in names
    assert "McGill Debate Society" not in names


def test_starter_clubs_falls_back_to_defaults_for_unknown_major(client, fake_supabase):
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "McGill Debate Society", "is_verified": True},
    ])
    resp = client.get(
        "/api/clubs/starter", params={"user_id": "user-1", "major": "Underwater Basket Weaving"},
        headers=auth("user-1"),
    )
    names = [c["name"] for c in resp.json()["starter_clubs"]]
    assert names == ["McGill Debate Society"]


def test_starter_clubs_requires_self(client, fake_supabase):
    fake_supabase.set_table("clubs", [])
    resp = client.get(
        "/api/clubs/starter", params={"user_id": "someone-else"}, headers=auth("user-1"),
    )
    assert resp.status_code == 403


# ── get_categories ────────────────────────────────────────────────────────────

def test_categories_is_static_and_cacheable(client):
    resp = client.get("/api/clubs/categories")
    assert resp.status_code == 200
    assert "Academic" in resp.json()["categories"]
    assert "s-maxage=86400" in resp.headers["Cache-Control"]


# ── get_created_clubs ─────────────────────────────────────────────────────────

def test_created_clubs_merges_owned_and_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "Owned Club", "created_by": "user-1"},
        {"id": "c2", "name": "Admin Club", "created_by": "someone-else"},
    ])
    fake_supabase.set_table("user_clubs", [
        {"user_id": "user-1", "club_id": "c2", "role": "admin"},
    ])
    resp = client.get("/api/clubs/created/user-1", headers=auth("user-1"))
    assert resp.status_code == 200
    body = resp.json()
    roles = {c["id"]: c["_manage_role"] for c in body["clubs"]}
    assert roles == {"c1": "owner", "c2": "admin"}


# ── join_club ─────────────────────────────────────────────────────────────────

def test_join_club_rejects_non_mcgill_email(client, fake_supabase):
    fake_supabase.set_auth_email("someone@gmail.com")
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": False}])
    resp = client.post(
        "/api/clubs/user/user-1/join", json={"club_id": "c1"}, headers=auth("user-1"),
    )
    assert resp.status_code == 403


def test_join_public_club_joins_directly(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": False}])
    resp = client.post(
        "/api/clubs/user/user-1/join", json={"club_id": "c1"}, headers=auth("user-1"),
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "status": "joined"}
    rows = fake_supabase.table("user_clubs").execute().data
    assert {"user_id": "user-1", "club_id": "c1"}.items() <= rows[0].items()


def test_join_already_joined_club_409s(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": False}])
    fake_supabase.set_table("user_clubs", [{"user_id": "user-1", "club_id": "c1"}])
    resp = client.post(
        "/api/clubs/user/user-1/join", json={"club_id": "c1"}, headers=auth("user-1"),
    )
    assert resp.status_code == 409


def test_join_private_club_creates_request_not_membership(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": True, "created_by": "owner-1"}])
    resp = client.post(
        "/api/clubs/user/user-1/join",
        json={"club_id": "c1", "requester_name": "Pat"},
        headers=auth("user-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"
    assert fake_supabase.table("user_clubs").execute().data == []
    reqs = fake_supabase.table("club_join_requests").execute().data
    assert reqs[0]["status"] == "pending"
    assert reqs[0]["requester_name"] == "Pat"


def test_join_private_club_rate_limited_after_three_requests_per_year(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": True, "created_by": "owner-1"}])
    now = datetime.now(timezone.utc).isoformat()
    fake_supabase.set_table("club_join_requests", [
        {"id": f"r{i}", "user_id": "user-1", "club_id": "c1", "status": "denied", "created_at": now}
        for i in range(3)
    ])
    resp = client.post(
        "/api/clubs/user/user-1/join", json={"club_id": "c1"}, headers=auth("user-1"),
    )
    assert resp.status_code == 429


def test_join_club_requires_self(client, fake_supabase):
    fake_supabase.set_table("clubs", [])
    resp = client.post(
        "/api/clubs/user/user-1/join", json={"club_id": "c1"}, headers=auth("someone-else"),
    )
    assert resp.status_code == 403


def test_admin_user_can_join_without_mcgill_email(client, fake_supabase):
    fake_supabase.set_auth_email("not-mcgill@gmail.com")
    fake_supabase.set_table("clubs", [{"id": "c1", "name": "Club", "is_private": False}])
    resp = client.post(
        "/api/clubs/user/{}/join".format(ADMIN_ID), json={"club_id": "c1"}, headers=auth(ADMIN_ID),
    )
    assert resp.status_code == 200


# ── handle_join_request ───────────────────────────────────────────────────────

def test_handle_join_request_approve_adds_member_and_deletes_request(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("club_join_requests", [
        {"id": "r1", "club_id": "c1", "user_id": "user-1", "status": "pending"},
    ])
    resp = client.post(
        "/api/clubs/join-requests/r1/action", json={"action": "approve"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert fake_supabase.table("club_join_requests").execute().data == []
    members = fake_supabase.table("user_clubs").execute().data
    assert members[0]["user_id"] == "user-1" and members[0]["club_id"] == "c1"


def test_handle_join_request_deny_removes_request_without_membership(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("club_join_requests", [
        {"id": "r1", "club_id": "c1", "user_id": "user-1", "status": "pending"},
    ])
    resp = client.post(
        "/api/clubs/join-requests/r1/action", json={"action": "deny"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
    assert fake_supabase.table("user_clubs").execute().data == []


def test_handle_join_request_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("club_join_requests", [
        {"id": "r1", "club_id": "c1", "user_id": "user-1", "status": "pending"},
    ])
    resp = client.post(
        "/api/clubs/join-requests/r1/action", json={"action": "approve"}, headers=auth("random-user"),
    )
    assert resp.status_code == 403


def test_handle_join_request_invalid_action_400s(client, fake_supabase):
    resp = client.post(
        "/api/clubs/join-requests/r1/action", json={"action": "bogus"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 400


def test_handle_join_request_not_found_404s(client, fake_supabase):
    fake_supabase.set_table("club_join_requests", [])
    resp = client.post(
        "/api/clubs/join-requests/missing/action", json={"action": "approve"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 404


# ── leave_club / toggle_calendar_sync ─────────────────────────────────────────

def test_leave_club_removes_membership(client, fake_supabase):
    fake_supabase.set_table("user_clubs", [{"user_id": "user-1", "club_id": "c1"}])
    resp = client.delete("/api/clubs/user/user-1/leave/c1", headers=auth("user-1"))
    assert resp.status_code == 200
    assert fake_supabase.table("user_clubs").execute().data == []


def test_toggle_calendar_sync_updates_flag(client, fake_supabase):
    fake_supabase.set_table("user_clubs", [{"user_id": "user-1", "club_id": "c1", "calendar_synced": False}])
    resp = client.patch(
        "/api/clubs/user/user-1/calendar/c1", params={"synced": "true"}, headers=auth("user-1"),
    )
    assert resp.status_code == 200
    assert fake_supabase.table("user_clubs").execute().data[0]["calendar_synced"] is True


# ── submit_club ───────────────────────────────────────────────────────────────

def test_submit_club_escapes_free_text(client, fake_supabase):
    resp = client.post(
        "/api/clubs/submit",
        json={
            "name": "<b>Evil</b> Club",
            "description": "<script>alert(1)</script>",
            "executive_emails": "a@mail.mcgill.ca",
        },
        headers=auth("user-1"),
    )
    assert resp.status_code == 200
    stored = fake_supabase.table("club_submissions").execute().data[0]
    assert stored["name"] == "&lt;b&gt;Evil&lt;/b&gt; Club"
    assert "<script>" not in stored["description"]


def test_submit_club_generates_action_tokens(client, fake_supabase):
    """Regression test for the email.py import-fix: _generate_action_tokens
    used to raise NameError on every call (missing `import secrets` /
    datetime / get_supabase), silently caught here, so token_generated was
    always False and the admin approval email was never actually sent.
    Tokens must now actually be generated and stored on the submission."""
    resp = client.post(
        "/api/clubs/submit",
        json={"name": "Club", "description": "desc", "executive_emails": "a@mail.mcgill.ca"},
        headers=auth("user-1"),
    )
    body = resp.json()
    assert body["token_generated"] is True
    stored = fake_supabase.table("club_submissions").execute().data[0]
    assert stored.get("approve_token") and stored.get("reject_token")


def test_submit_club_rejects_non_mcgill(client, fake_supabase):
    """Non-McGill email is rejected with 403 mcgill_email_required.
    (Previously pinned as 500 due to a starlette <1.0 exception-swallowing
    bug where HTTPException raised before the try block was caught by the
    outer `except Exception`. Fixed in starlette 1.x.)"""
    fake_supabase.set_auth_email("nobody@gmail.com")
    resp = client.post(
        "/api/clubs/submit",
        json={"name": "Club", "description": "desc", "executive_emails": "a@mail.mcgill.ca"},
        headers=auth("user-1"),
    )
    assert resp.status_code == 403


# ── club members / roles ──────────────────────────────────────────────────────

def test_get_club_members_requires_membership(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [])
    resp = client.get("/api/clubs/c1/members", headers=auth("random-user"))
    assert resp.status_code == 403


def test_get_club_members_sorts_owner_first(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("users", [
        {"id": "owner-1", "email": "owner.one@mail.mcgill.ca"},
        {"id": "member-1", "email": "member.one@mail.mcgill.ca"},
    ])
    fake_supabase.set_table("user_clubs", [
        {"user_id": "member-1", "club_id": "c1", "role": "member"},
        {"user_id": "owner-1", "club_id": "c1", "role": "owner"},
    ])
    resp = client.get("/api/clubs/c1/members", headers=auth("owner-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert [m["role"] for m in body["members"]] == ["owner", "member"]
    assert body["members"][0]["name"] == "Owner"


def test_get_club_members_does_not_promote_a_member_when_owner_row_is_gone(client, fake_supabase):
    """Regression: GET /members used to silently reassign clubs.created_by
    (and the new owner's user_clubs role) to an arbitrary member whenever
    the recorded owner's `users` row was missing — letting any Member
    become Owner just by viewing the roster once the real Owner's account
    was deleted. A read-only GET must never mutate ownership."""
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "gone-owner"}])
    fake_supabase.set_table("users", [
        {"id": "member-1", "email": "member.one@mail.mcgill.ca"},
    ])
    fake_supabase.set_table("user_clubs", [
        {"user_id": "member-1", "club_id": "c1", "role": "member"},
    ])

    resp = client.get("/api/clubs/c1/members", headers=auth("member-1"))

    assert resp.status_code == 200
    assert fake_supabase._tables["clubs"][0]["created_by"] == "gone-owner"
    assert fake_supabase._tables["user_clubs"][0]["role"] == "member"
    body = resp.json()
    assert [m["role"] for m in body["members"]] == ["member"]


def test_update_member_role_owner_can_promote(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [
        {"user_id": "owner-1", "club_id": "c1", "role": "owner"},
        {"user_id": "member-1", "club_id": "c1", "role": "member"},
    ])
    resp = client.request(
        "PATCH", "/api/clubs/c1/members/member-1/role",
        json={"role": "admin"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


def test_update_member_role_rejects_non_owner_admin_changing_owner(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [
        {"user_id": "owner-1", "club_id": "c1", "role": "owner"},
        {"user_id": "admin-1", "club_id": "c1", "role": "admin"},
    ])
    resp = client.request(
        "PATCH", "/api/clubs/c1/members/owner-1/role",
        json={"role": "member"}, headers=auth("admin-1"),
    )
    assert resp.status_code == 400


def test_remove_club_member_cannot_remove_owner(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [{"user_id": "owner-1", "club_id": "c1", "role": "owner"}])
    resp = client.delete("/api/clubs/c1/members/owner-1", headers=auth("owner-1"))
    assert resp.status_code == 400


# ── club events / announcements ───────────────────────────────────────────────

def test_create_club_event_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.post(
        "/api/clubs/c1/events",
        json={"title": "Mixer", "date": "2026-09-01"},
        headers=auth("random-user"),
    )
    assert resp.status_code == 403


def test_create_club_event_normalizes_12h_time(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1", "name": "Club"}])
    resp = client.post(
        "/api/clubs/c1/events",
        json={"title": "Mixer", "date": "2026-09-01", "time": "2:00 PM"},
        headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["event"]["time"] == "14:00"


def test_delete_club_event_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.delete("/api/clubs/c1/events/e1", headers=auth("random-user"))
    assert resp.status_code == 403


# ── subscriptions ─────────────────────────────────────────────────────────────

def test_toggle_subscribe_subscribes_then_unsubscribes(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1"}])
    resp1 = client.post("/api/clubs/c1/subscribe", headers=auth("user-1"))
    assert resp1.json() == {"success": True, "is_subscribed": True}
    resp2 = client.post("/api/clubs/c1/subscribe", headers=auth("user-1"))
    assert resp2.json() == {"success": True, "is_subscribed": False}


def test_subscribers_count_requires_auth(client):
    resp = client.get("/api/clubs/c1/subscribers")
    assert resp.status_code == 401


# ── club managers ──────────────────────────────────────────────────────────────

def test_get_club_managers_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.get("/api/clubs/c1/managers", headers=auth("random-user"))
    assert resp.status_code == 403


def test_get_club_managers_includes_creator(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("users", [{"id": "owner-1", "email": "owner.one@mail.mcgill.ca"}])
    fake_supabase.set_table("club_managers", [])
    resp = client.get("/api/clubs/c1/managers", headers=auth("owner-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["managers"][0]["role"] == "owner"
    assert body["managers"][0]["user_id"] == "owner-1"


def test_add_club_manager_endpoint_removed(client, fake_supabase):
    """docs/adr/0002: add_club_manager (the legacy club_managers-table write
    path) is dead code — never called by the frontend — and is deleted."""
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.post(
        "/api/clubs/c1/managers", json={"email": "new.manager@mail.mcgill.ca"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 405


def test_manager_invite_accept_then_shows_up_in_get_club_managers(client, fake_supabase):
    """The bug docs/adr/0002 fixes: a Manager added via the only working
    creation path (invite accept) must actually appear in the manager list."""
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("users", [{"id": "owner-1", "email": "owner@mail.mcgill.ca"},
                                       {"id": "user-2", "email": "new.manager@mail.mcgill.ca"}])
    fake_supabase.set_table("club_manager_requests", [
        {"id": "inv-1", "club_id": "c1", "target_user_id": "user-2", "status": "pending"},
    ])
    accept = client.post(
        "/api/clubs/manager-requests/inv-1/action", json={"action": "accept"}, headers=auth("user-2"),
    )
    assert accept.status_code == 200

    resp = client.get("/api/clubs/c1/managers", headers=auth("owner-1"))
    assert resp.status_code == 200
    user_ids = {m["user_id"] for m in resp.json()["managers"]}
    assert user_ids == {"owner-1", "user-2"}
    roles = {m["user_id"]: m["role"] for m in resp.json()["managers"]}
    assert roles["user-2"] == "manager"
    assert roles["owner-1"] == "owner"


def test_remove_club_manager_revokes_permission(client, fake_supabase):
    """The other half of the bug fix: removing a Manager added via the
    invite flow must actually revoke their access, not silently no-op."""
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [{"id": "uc1", "club_id": "c1", "user_id": "user-2", "role": "admin"}])

    resp = client.delete("/api/clubs/c1/managers/user-2", headers=auth("owner-1"))
    assert resp.status_code == 200

    membership = fake_supabase.table("user_clubs").execute().data[0]
    assert membership["role"] == "member"

    # No longer a manager, so manager-only actions are now refused
    managers_resp = client.get("/api/clubs/c1/managers", headers=auth("user-2"))
    assert managers_resp.status_code == 403


def test_remove_club_manager_cannot_remove_owner(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.delete("/api/clubs/c1/managers/owner-1", headers=auth("owner-1"))
    assert resp.status_code == 400


def test_remove_club_manager_requires_owner_or_global_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.delete("/api/clubs/c1/managers/mgr-1", headers=auth("some-manager"))
    assert resp.status_code == 403


# ── delete_club ────────────────────────────────────────────────────────────────

def test_delete_club_requires_global_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.delete("/api/clubs/c1", headers=auth("owner-1"))
    assert resp.status_code == 403


def test_delete_club_as_admin_removes_club_and_memberships(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [{"user_id": "owner-1", "club_id": "c1"}])
    resp = client.delete("/api/clubs/c1", headers=auth(ADMIN_ID))
    assert resp.status_code == 200
    assert fake_supabase.table("clubs").execute().data == []
    assert fake_supabase.table("user_clubs").execute().data == []


# ── admin submissions ──────────────────────────────────────────────────────────

def test_admin_list_submissions_requires_token(client, fake_supabase):
    resp = client.get("/api/clubs/admin/submissions")
    assert resp.status_code == 401


def test_admin_review_submission_approve_creates_club(client, fake_supabase, monkeypatch):
    from api.routes import admin as admin_module
    monkeypatch.setattr(admin_module, "verify_admin_token", lambda token: True)

    fake_supabase.set_table("club_submissions", [
        {"id": "s1", "name": "New Club", "description": "desc", "status": "pending",
         "submitted_by": "user-1"},
    ])
    resp = client.patch(
        "/api/clubs/admin/submissions/s1",
        json={"status": "approved"},
        headers={"x-cron-secret": "anything"},
    )
    assert resp.status_code == 200
    clubs = fake_supabase.table("clubs").execute().data
    assert clubs[0]["name"] == "New Club"
    memberships = fake_supabase.table("user_clubs").execute().data
    assert memberships[0]["role"] == "owner"


# ── manager invite flow ───────────────────────────────────────────────────────

def test_create_manager_request_invites_existing_user(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("users", [{"id": "user-2", "email": "invitee@mail.mcgill.ca"}])
    resp = client.post(
        "/api/clubs/c1/manager-requests",
        json={"email": "invitee@mail.mcgill.ca"},
        headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    invites = fake_supabase.table("club_manager_requests").execute().data
    assert invites[0]["target_user_id"] == "user-2"
    assert invites[0]["status"] == "pending"


def test_create_manager_request_rejects_self_invite(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("users", [{"id": "owner-1", "email": "owner@mail.mcgill.ca"}])
    resp = client.post(
        "/api/clubs/c1/manager-requests",
        json={"email": "owner@mail.mcgill.ca"},
        headers=auth("owner-1"),
    )
    assert resp.status_code == 400


def test_respond_to_manager_request_accept_grants_admin(client, fake_supabase):
    fake_supabase.set_table("club_manager_requests", [
        {"id": "inv-1", "club_id": "c1", "target_user_id": "user-2", "status": "pending"},
    ])
    resp = client.post(
        "/api/clubs/manager-requests/inv-1/action", json={"action": "accept"}, headers=auth("user-2"),
    )
    assert resp.status_code == 200
    memberships = fake_supabase.table("user_clubs").execute().data
    assert {"club_id": "c1", "user_id": "user-2", "role": "admin"}.items() <= memberships[0].items()


def test_respond_to_manager_request_rejects_wrong_user(client, fake_supabase):
    fake_supabase.set_table("club_manager_requests", [
        {"id": "inv-1", "club_id": "c1", "target_user_id": "user-2", "status": "pending"},
    ])
    resp = client.post(
        "/api/clubs/manager-requests/inv-1/action", json={"action": "accept"}, headers=auth("user-3"),
    )
    assert resp.status_code == 403


# ── stale club cleanup cron ────────────────────────────────────────────────────

def test_stale_club_cleanup_dry_run_does_not_delete(fake_supabase):
    from api.routes.clubs import run_stale_club_cleanup_cron

    old_signin = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "Stale Club", "created_by": "owner-1",
         "created_at": (datetime.now(timezone.utc) - timedelta(days=900)).isoformat()},
    ])

    class FakeAdminUser:
        def get_user_by_id(self, user_id):
            from types import SimpleNamespace
            return SimpleNamespace(user=SimpleNamespace(last_sign_in_at=old_signin))

    fake_supabase.auth.admin.get_user_by_id = FakeAdminUser().get_user_by_id

    result = run_stale_club_cleanup_cron(dry_run=True)
    assert result["deleted"] == 0
    assert result["deleted_names"] == ["Stale Club"]
    assert fake_supabase.table("clubs").execute().data  # still present


def test_stale_club_cleanup_skips_clubs_within_grace_period(fake_supabase):
    from api.routes.clubs import run_stale_club_cleanup_cron

    fake_supabase.set_table("clubs", [
        {"id": "c1", "name": "Brand New Club", "created_by": "owner-1",
         "created_at": datetime.now(timezone.utc).isoformat()},
    ])
    result = run_stale_club_cleanup_cron(dry_run=False)
    assert result["kept_too_new"] == 1
    assert result["deleted"] == 0


# ── edit_club / get_join_requests (GET) ───────────────────────────────────────

def test_edit_club_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.put("/api/clubs/edit/c1", json={"name": "New Name"}, headers=auth("random-user"))
    assert resp.status_code == 403


def test_edit_club_updates_only_provided_fields(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1", "name": "Old", "category": "Social"}])
    resp = client.put("/api/clubs/edit/c1", json={"name": "New Name"}, headers=auth("owner-1"))
    assert resp.status_code == 200
    updated = fake_supabase.table("clubs").execute().data[0]
    assert updated["name"] == "New Name"
    assert updated["category"] == "Social"


def test_get_join_requests_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.get("/api/clubs/join-requests/c1", headers=auth("random-user"))
    assert resp.status_code == 403


def test_get_join_requests_returns_pending_only(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    fake_supabase.set_table("club_join_requests", [
        {"id": "r1", "club_id": "c1", "status": "pending"},
        {"id": "r2", "club_id": "c1", "status": "denied"},
    ])
    resp = client.get("/api/clubs/join-requests/c1", headers=auth("owner-1"))
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# ── user-scoped club views ────────────────────────────────────────────────────

def test_get_user_clubs_includes_calendar_sync_flag(client, fake_supabase):
    fake_supabase.set_table("user_clubs", [
        {"id": "uc1", "user_id": "user-1", "club_id": "c1", "calendar_synced": True,
         "joined_at": "2026-01-01", "clubs": {"id": "c1", "name": "Club"}},
    ])
    resp = client.get("/api/clubs/user/user-1", headers=auth("user-1"))
    assert resp.status_code == 200
    clubs = resp.json()["clubs"]
    assert clubs[0]["calendar_synced"] is True
    assert clubs[0]["user_club_id"] == "uc1"


def test_get_user_pending_requests_returns_club_ids(client, fake_supabase):
    fake_supabase.set_table("club_join_requests", [
        {"user_id": "user-1", "club_id": "c1", "status": "pending"},
        {"user_id": "user-1", "club_id": "c2", "status": "denied"},
    ])
    resp = client.get("/api/clubs/user/user-1/pending-requests", headers=auth("user-1"))
    assert resp.json()["pending_club_ids"] == ["c1"]


def test_get_user_subscriptions_returns_club_ids(client, fake_supabase):
    fake_supabase.set_table("club_subscriptions", [{"user_id": "user-1", "club_id": "c1"}])
    resp = client.get("/api/clubs/user/user-1/subscriptions", headers=auth("user-1"))
    assert resp.json()["subscribed_club_ids"] == ["c1"]


# ── subscribed events / announcements feed ────────────────────────────────────

def test_subscribed_events_only_for_calendar_synced_clubs(client, fake_supabase):
    fake_supabase.set_table("user_clubs", [
        {"user_id": "user-1", "club_id": "c1", "calendar_synced": True},
        {"user_id": "user-1", "club_id": "c2", "calendar_synced": False},
    ])
    fake_supabase.set_table("club_events", [
        {"id": "e1", "club_id": "c1", "date": "2026-09-01"},
        {"id": "e2", "club_id": "c2", "date": "2026-09-02"},
    ])
    resp = client.get("/api/clubs/events/subscribed", headers=auth("user-1"))
    assert [e["id"] for e in resp.json()["events"]] == ["e1"]


def test_subscribed_announcements_only_for_calendar_synced_clubs(client, fake_supabase):
    fake_supabase.set_table("user_clubs", [{"user_id": "user-1", "club_id": "c1", "calendar_synced": True}])
    fake_supabase.set_table("club_announcements", [{"id": "a1", "club_id": "c1", "created_at": "2026-01-01"}])
    resp = client.get("/api/clubs/announcements/subscribed", headers=auth("user-1"))
    assert [a["id"] for a in resp.json()["announcements"]] == ["a1"]


# ── club announcements ─────────────────────────────────────────────────────────

def test_create_club_announcement_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.post(
        "/api/clubs/c1/announcements", json={"title": "Hi", "body": "Body"}, headers=auth("random-user"),
    )
    assert resp.status_code == 403


def test_create_club_announcement_succeeds(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1", "name": "Club"}])
    resp = client.post(
        "/api/clubs/c1/announcements", json={"title": "Hi", "body": "Body"}, headers=auth("owner-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["announcement"]["title"] == "Hi"


def test_delete_club_announcement_requires_owner_or_admin(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
    resp = client.delete("/api/clubs/c1/announcements/a1", headers=auth("random-user"))
    assert resp.status_code == 403


# ── club activity feed ─────────────────────────────────────────────────────────

def test_club_activity_merges_and_sorts_events_and_announcements(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "is_private": False}])
    fake_supabase.set_table("club_announcements", [
        {"id": "a1", "club_id": "c1", "title": "Older Ann", "body": "x", "created_at": "2026-01-01T00:00:00Z"},
    ])
    fake_supabase.set_table("club_events", [
        {"id": "e1", "club_id": "c1", "title": "Newer Event", "date": "2026-06-01", "time": "10:00"},
    ])
    resp = client.get("/api/clubs/c1/activity", headers=auth("user-1"))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["id"] == "e1"
    assert items[1]["id"] == "a1"


def test_club_activity_private_club_requires_membership(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "is_private": True, "created_by": "owner-1"}])
    fake_supabase.set_table("user_clubs", [])
    resp = client.get("/api/clubs/c1/activity", headers=auth("random-user"))
    assert resp.status_code == 403


# ── faculty stats ──────────────────────────────────────────────────────────────

def test_faculty_stats_suppresses_breakdown_for_small_clubs(client, fake_supabase):
    fake_supabase.set_table("clubs", [{"id": "c1", "is_private": False}])
    fake_supabase.set_table("users", [{"id": "user-1", "faculty": "Science"}])
    fake_supabase.set_table("user_clubs", [{"user_id": "user-1", "club_id": "c1"}])
    resp = client.get("/api/clubs/c1/faculty-stats", headers=auth("user-1"))
    body = resp.json()
    assert body["by_faculty"] == []
    assert body["your_faculty_count"] == 1


def test_faculty_stats_buckets_small_counts_for_large_clubs(client, fake_supabase):
    """Counts >= MIN_BUCKET show exactly; smaller counts get bucketed as
    "<5" UNLESS it's the caller's own faculty, which is always exact (the
    caller already knows they're in that bucket, so it's safe to disclose)."""
    fake_supabase.set_table("clubs", [{"id": "c1", "is_private": False}])
    members = [{"id": f"u{i}", "faculty": "Science"} for i in range(9)]
    members.append({"id": "u9", "faculty": "Law"})
    fake_supabase.set_table("users", members + [{"id": "caller", "faculty": "Arts"}])
    fake_supabase.set_table("user_clubs", [{"user_id": m["id"], "club_id": "c1"} for m in members])
    resp = client.get("/api/clubs/c1/faculty-stats", headers=auth("caller"))
    body = resp.json()
    by_faculty = {b["faculty"]: b["count"] for b in body["by_faculty"]}
    assert by_faculty["Science"] == 9
    assert by_faculty["Law"] == "<5"
    assert body["your_faculty_count"] == 0  # caller (Arts) isn't a member of this club


# ── email-based admin action (HTML response) ──────────────────────────────────

def test_admin_email_action_invalid_token_shows_expired_page(client, monkeypatch):
    from api.routes.clubs import email as email_module
    monkeypatch.setattr(email_module, "_verify_action_token", lambda token: (None, None))
    resp = client.get("/api/clubs/admin/action", params={"token": "bogus"})
    assert resp.status_code == 200
    assert "Link Expired" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_admin_email_action_approve_creates_club_and_renders_success(client, fake_supabase, monkeypatch):
    from api.routes.clubs import email as email_module
    monkeypatch.setattr(email_module, "_verify_action_token", lambda token: ("s1", "approved"))
    fake_supabase.set_table("club_submissions", [
        {"id": "s1", "name": "New Club", "description": "desc", "status": "pending", "submitted_by": "user-1"},
    ])
    resp = client.get("/api/clubs/admin/action", params={"token": "good-token"})
    assert resp.status_code == 200
    assert "Club Approved" in resp.text
    clubs = fake_supabase.table("clubs").execute().data
    assert clubs[0]["name"] == "New Club"


def test_admin_email_action_already_processed(client, fake_supabase, monkeypatch):
    from api.routes.clubs import email as email_module
    monkeypatch.setattr(email_module, "_verify_action_token", lambda token: ("s1", "approved"))
    fake_supabase.set_table("club_submissions", [
        {"id": "s1", "name": "New Club", "status": "approved"},
    ])
    resp = client.get("/api/clubs/admin/action", params={"token": "good-token"})
    assert "Already Processed" in resp.text
