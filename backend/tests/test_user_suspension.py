"""
damianphim/symbolos#161, Phase 2b: proportionate user restrictions.

Suspension is checked only at specific content-creation routes
(require_not_suspended), not in get_current_user_id — a suspended user can
still log in and read the app, they just can't post. See
api/utils/moderation.py's module docstring for why.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth

MODERATOR_ID = "65ad96d2-1704-4ff2-b661-42626f153fe8"  # hardcoded in clubs.ADMIN_USER_IDS


@pytest.fixture(autouse=True)
def _no_resend(monkeypatch):
    from api import config
    monkeypatch.setattr(config.settings, "RESEND_API_KEY", "")


def _seed_suspended_user(fake_supabase, user_id="suspended-1"):
    fake_supabase.set_table("users", [
        {"id": user_id, "email": f"{user_id}@mail.mcgill.ca", "is_suspended": True},
    ])


class TestModerationUtils:
    def test_suspend_user_sets_flag_and_reason(self, fake_supabase):
        from api.utils.moderation import suspend_user, is_user_suspended
        fake_supabase.set_table("users", [{"id": "u1", "email": "u1@mail.mcgill.ca", "is_suspended": False}])

        assert is_user_suspended("u1") is False
        suspend_user("u1", reason="repeated harassment")
        assert is_user_suspended("u1") is True
        row = fake_supabase._tables["users"][0]
        assert row["suspended_reason"] == "repeated harassment"
        assert row["suspended_at"] is not None

    def test_is_user_suspended_false_for_unknown_user(self, fake_supabase):
        from api.utils.moderation import is_user_suspended
        fake_supabase.set_table("users", [])
        assert is_user_suspended("does-not-exist") is False

    def test_require_not_suspended_raises_403_for_suspended_user(self, fake_supabase):
        from fastapi import HTTPException
        from api.utils.moderation import require_not_suspended
        _seed_suspended_user(fake_supabase, "u1")
        with pytest.raises(HTTPException) as exc_info:
            require_not_suspended("u1")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "account_suspended"

    def test_require_not_suspended_passes_for_normal_user(self, fake_supabase):
        from api.utils.moderation import require_not_suspended
        fake_supabase.set_table("users", [{"id": "u1", "email": "u1@mail.mcgill.ca", "is_suspended": False}])
        require_not_suspended("u1")  # must not raise


class TestSuspendedUserBlockedFromPosting:
    def test_forum_post_blocked(self, fake_supabase, client):
        _seed_suspended_user(fake_supabase, "suspended-1")
        resp = client.post(
            "/api/forum/posts",
            json={
                "author": "Some Student", "avatar_color": "#ed1b2f", "category": "general",
                "title": "Hi", "body": "Body", "tags": [],
            },
            headers=auth("suspended-1"),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "account_suspended"

    def test_forum_reply_blocked(self, fake_supabase, client):
        _seed_suspended_user(fake_supabase, "suspended-1")
        fake_supabase.set_table("forum_posts", [{"id": "post-1", "user_id": "author-1"}])
        resp = client.post(
            "/api/forum/posts/post-1/replies",
            json={"author": "Some Student", "avatar_color": "#ed1b2f", "body": "reply"},
            headers=auth("suspended-1"),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "account_suspended"

    def test_club_submission_blocked(self, fake_supabase, client):
        _seed_suspended_user(fake_supabase, "suspended-1")
        resp = client.post(
            "/api/clubs/submit",
            json={"name": "Club", "description": "desc", "executive_emails": "a@mail.mcgill.ca"},
            headers=auth("suspended-1"),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "account_suspended"

    def test_club_event_creation_blocked(self, fake_supabase, client):
        fake_supabase.set_table("users", [{"id": "owner-1", "email": "owner@mail.mcgill.ca", "is_suspended": True}])
        fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
        resp = client.post(
            "/api/clubs/c1/events",
            json={"title": "Mixer", "date": "2026-09-01"},
            headers=auth("owner-1"),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "account_suspended"

    def test_club_announcement_creation_blocked(self, fake_supabase, client):
        fake_supabase.set_table("users", [{"id": "owner-1", "email": "owner@mail.mcgill.ca", "is_suspended": True}])
        fake_supabase.set_table("clubs", [{"id": "c1", "created_by": "owner-1"}])
        resp = client.post(
            "/api/clubs/c1/announcements",
            json={"title": "Hi", "body": "Body"},
            headers=auth("owner-1"),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "account_suspended"

    def test_non_suspended_user_can_still_post(self, fake_supabase, client):
        """Suspension must not accidentally block everyone — a user with
        is_suspended absent/false posts normally."""
        fake_supabase.set_table("users", [{"id": "user-1", "email": "user-1@mail.mcgill.ca", "is_suspended": False}])
        resp = client.post(
            "/api/forum/posts",
            json={
                "author": "Some Student", "avatar_color": "#ed1b2f", "category": "general",
                "title": "Hi", "body": "Body", "tags": [],
            },
            headers=auth("user-1"),
        )
        assert resp.status_code == 201


class TestResolveRestrictUser:
    def test_restrict_user_requires_moderator(self, fake_supabase, client):
        fake_supabase.set_table("reports", [
            {"id": "r1", "reporter_id": "reporter-1", "status": "open", "content_type": "user", "content_id": "bad-actor-1"},
        ])
        resp = client.post(
            "/api/moderation/reports/r1/resolve",
            json={"action": "restrict_user"},
            headers=auth("random-user"),
        )
        assert resp.status_code == 403

    def test_restrict_user_suspends_and_resolves(self, fake_supabase, client):
        fake_supabase.set_table("reports", [
            {"id": "r1", "reporter_id": "reporter-1", "status": "open", "content_type": "user", "content_id": "bad-actor-1"},
        ])
        fake_supabase.set_table("users", [{"id": "bad-actor-1", "email": "bad@mail.mcgill.ca", "is_suspended": False}])

        resp = client.post(
            "/api/moderation/reports/r1/resolve",
            json={"action": "restrict_user", "reason": "harassment"},
            headers=auth(MODERATOR_ID),
        )
        assert resp.status_code == 200

        user_row = fake_supabase._tables["users"][0]
        assert user_row["is_suspended"] is True
        assert user_row["suspended_reason"] == "harassment"
        assert fake_supabase._tables["reports"][0]["status"] == "resolved"

        actions = fake_supabase._tables["moderation_actions"]
        assert actions[0]["action"] == "user_restricted"
        assert actions[0]["details"]["content_id"] == "bad-actor-1"

    def test_restrict_user_rejected_for_non_user_content_type(self, fake_supabase, client):
        fake_supabase.set_table("reports", [
            {"id": "r1", "reporter_id": "reporter-1", "status": "open", "content_type": "forum_post", "content_id": "post-1"},
        ])
        resp = client.post(
            "/api/moderation/reports/r1/resolve",
            json={"action": "restrict_user"},
            headers=auth(MODERATOR_ID),
        )
        assert resp.status_code == 400

    def test_remove_content_still_rejected_for_user_content_type(self, fake_supabase, client):
        """Regression: restrict_user's addition must not loosen
        remove_content's existing guard against content_type == 'user'."""
        fake_supabase.set_table("reports", [
            {"id": "r1", "reporter_id": "reporter-1", "status": "open", "content_type": "user", "content_id": "bad-actor-1"},
        ])
        resp = client.post(
            "/api/moderation/reports/r1/resolve",
            json={"action": "remove_content"},
            headers=auth(MODERATOR_ID),
        )
        assert resp.status_code == 400
