"""
damianphim/symbolos#161, Phase 1: persistent reports + moderator queue.

Covers: report persistence (survives what used to be an email-only, lost-
on-failure notification), anti-abuse (rate limit, duplicate, self-report),
moderator-only access to the queue (and that no endpoint here can ever
hand reporter identity to a non-moderator — including the reported
content's own owner), and the forum.py integration.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth

MODERATOR_ID = "65ad96d2-1704-4ff2-b661-42626f153fe8"  # hardcoded in clubs.ADMIN_USER_IDS


@pytest.fixture(autouse=True)
def _no_report_rate_limit(monkeypatch):
    """The real limiter needs a real Supabase rate_limits table; give
    create_report a permissive fake so tests exercise the OTHER anti-abuse
    checks (dup/self-report) without tripping the rate limit incidentally."""
    class _AlwaysAllow:
        def is_allowed(self, *_a, **_k):
            return True
    import api.main as app_main
    monkeypatch.setattr(app_main, "_limiter", _AlwaysAllow(), raising=False)


def _seed_post(fake_supabase, post_id="post-1", owner="author-1"):
    fake_supabase.set_table("forum_posts", [
        {"id": post_id, "user_id": owner, "author": "Author", "title": "T", "body": "B", "category": "general"},
    ])


class TestCreateReportHappyPath:
    def test_persists_a_report_with_content_snapshot(self, client, fake_supabase):
        _seed_post(fake_supabase)
        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1", "reason_category": "spam"},
            headers=auth("reporter-1"),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "open"

        rows = fake_supabase._tables["reports"]
        assert len(rows) == 1
        assert rows[0]["reporter_id"] == "reporter-1"
        assert rows[0]["content_type"] == "forum_post"
        assert rows[0]["content_id"] == "post-1"
        assert rows[0]["reason_category"] == "spam"
        assert rows[0]["content_snapshot"]["title"] == "T"

    def test_context_is_stored(self, client, fake_supabase):
        _seed_post(fake_supabase)
        client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1",
                  "reason_category": "harassment", "context": "targeted at me specifically"},
            headers=auth("reporter-1"),
        )
        assert fake_supabase._tables["reports"][0]["context"] == "targeted at me specifically"


class TestAntiAbuse:
    def test_self_report_blocked_via_owner_column(self, client, fake_supabase):
        _seed_post(fake_supabase, owner="author-1")
        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1", "reason_category": "spam"},
            headers=auth("author-1"),
        )
        assert resp.status_code == 400
        assert "own content" in resp.json()["detail"]
        assert fake_supabase._tables.get("reports", []) == []

    def test_self_report_blocked_for_user_content_type(self, client, fake_supabase):
        fake_supabase.set_table("users", [{"id": "user-1", "email": "a@mail.mcgill.ca", "username": "A"}])
        resp = client.post(
            "/api/reports",
            json={"content_type": "user", "content_id": "user-1", "reason_category": "harassment"},
            headers=auth("user-1"),
        )
        assert resp.status_code == 400
        assert "yourself" in resp.json()["detail"]

    def test_duplicate_report_rejected(self, client, fake_supabase):
        _seed_post(fake_supabase)
        payload = {"content_type": "forum_post", "content_id": "post-1", "reason_category": "spam"}
        first = client.post("/api/reports", json=payload, headers=auth("reporter-1"))
        assert first.status_code == 201
        second = client.post("/api/reports", json=payload, headers=auth("reporter-1"))
        assert second.status_code == 409
        assert len(fake_supabase._tables["reports"]) == 1

    def test_different_reporters_can_both_report_same_content(self, client, fake_supabase):
        _seed_post(fake_supabase)
        payload = {"content_type": "forum_post", "content_id": "post-1", "reason_category": "spam"}
        r1 = client.post("/api/reports", json=payload, headers=auth("reporter-1"))
        r2 = client.post("/api/reports", json=payload, headers=auth("reporter-2"))
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert len(fake_supabase._tables["reports"]) == 2

    def test_rate_limited_returns_429(self, client, fake_supabase, monkeypatch):
        _seed_post(fake_supabase)
        class _AlwaysDeny:
            def is_allowed(self, *_a, **_k):
                return False
        import api.main as app_main
        monkeypatch.setattr(app_main, "_limiter", _AlwaysDeny(), raising=False)

        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1", "reason_category": "spam"},
            headers=auth("reporter-1"),
        )
        assert resp.status_code == 429


class TestValidation:
    def test_unknown_content_type_rejected(self, client, fake_supabase):
        resp = client.post(
            "/api/reports",
            json={"content_type": "not_a_real_type", "content_id": "x", "reason_category": "spam"},
            headers=auth("reporter-1"),
        )
        assert resp.status_code == 400

    def test_unknown_reason_category_rejected(self, client, fake_supabase):
        _seed_post(fake_supabase)
        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1", "reason_category": "not_a_real_reason"},
            headers=auth("reporter-1"),
        )
        assert resp.status_code == 400

    def test_reporting_nonexistent_content_404s(self, client, fake_supabase):
        fake_supabase.set_table("forum_posts", [])
        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "does-not-exist", "reason_category": "spam"},
            headers=auth("reporter-1"),
        )
        assert resp.status_code == 404

    def test_oversized_context_rejected(self, client, fake_supabase):
        _seed_post(fake_supabase)
        resp = client.post(
            "/api/reports",
            json={"content_type": "forum_post", "content_id": "post-1",
                  "reason_category": "spam", "context": "x" * 1001},
            headers=auth("reporter-1"),
        )
        assert resp.status_code in (400, 422)


class TestModeratorQueueRequiresModerator:
    """Also proves, structurally, that reporter identity can never reach a
    non-moderator through this API — every read is gated the same way,
    including for the reported content's own owner."""

    def test_list_requires_moderator(self, client, fake_supabase):
        resp = client.get("/api/moderation/reports", headers=auth("author-1"))
        assert resp.status_code == 403

    def test_detail_requires_moderator(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "reporter-1", "status": "open"}])
        resp = client.get("/api/moderation/reports/r1", headers=auth("author-1"))
        assert resp.status_code == 403

    def test_assign_requires_moderator(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "reporter-1", "status": "open"}])
        resp = client.post("/api/moderation/reports/r1/assign", json={}, headers=auth("author-1"))
        assert resp.status_code == 403

    def test_note_requires_moderator(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "reporter-1", "status": "open"}])
        resp = client.post("/api/moderation/reports/r1/notes", json={"note": "hi"}, headers=auth("author-1"))
        assert resp.status_code == 403

    def test_dismiss_requires_moderator(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "reporter-1", "status": "open"}])
        resp = client.post("/api/moderation/reports/r1/dismiss", json={}, headers=auth("author-1"))
        assert resp.status_code == 403


class TestModeratorQueue:
    def test_list_and_filter(self, client, fake_supabase):
        fake_supabase.set_table("reports", [
            {"id": "r1", "reporter_id": "u1", "status": "open", "content_type": "forum_post", "created_at": "2026-01-01"},
            {"id": "r2", "reporter_id": "u2", "status": "dismissed", "content_type": "club", "created_at": "2026-01-02"},
        ])
        resp = client.get("/api/moderation/reports?status_filter=open", headers=auth(MODERATOR_ID))
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["reports"][0]["id"] == "r1"

    def test_detail_includes_action_history(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "open"}])
        fake_supabase.set_table("moderation_actions", [
            {"id": "a1", "report_id": "r1", "moderator_id": MODERATOR_ID, "action": "note_added", "details": {"note": "hi"}},
        ])
        resp = client.get("/api/moderation/reports/r1", headers=auth(MODERATOR_ID))
        assert resp.status_code == 200
        body = resp.json()
        assert body["report"]["id"] == "r1"
        assert len(body["actions"]) == 1

    def test_assign_to_self_sets_in_review_and_logs_action(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "open", "assigned_to": None}])
        resp = client.post("/api/moderation/reports/r1/assign", json={}, headers=auth(MODERATOR_ID))
        assert resp.status_code == 200

        report = fake_supabase._tables["reports"][0]
        assert report["status"] == "in_review"
        assert report["assigned_to"] == MODERATOR_ID

        actions = fake_supabase._tables["moderation_actions"]
        assert len(actions) == 1
        assert actions[0]["action"] == "assigned"
        assert actions[0]["moderator_id"] == MODERATOR_ID

    def test_assign_to_non_moderator_rejected(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "open"}])
        resp = client.post(
            "/api/moderation/reports/r1/assign",
            json={"assignee_id": "not-a-moderator"},
            headers=auth(MODERATOR_ID),
        )
        assert resp.status_code == 400

    def test_add_note_logs_action_without_changing_status(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "open"}])
        resp = client.post("/api/moderation/reports/r1/notes", json={"note": "investigating"}, headers=auth(MODERATOR_ID))
        assert resp.status_code == 200
        assert fake_supabase._tables["reports"][0]["status"] == "open"
        actions = fake_supabase._tables["moderation_actions"]
        assert actions[0]["action"] == "note_added"
        assert actions[0]["details"]["note"] == "investigating"

    def test_dismiss_sets_status_and_logs_action(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "open"}])
        resp = client.post("/api/moderation/reports/r1/dismiss", json={"reason": "not a violation"}, headers=auth(MODERATOR_ID))
        assert resp.status_code == 200
        assert fake_supabase._tables["reports"][0]["status"] == "dismissed"
        actions = fake_supabase._tables["moderation_actions"]
        assert actions[0]["action"] == "status_changed"
        assert actions[0]["details"]["to"] == "dismissed"

    def test_dismissing_twice_conflicts(self, client, fake_supabase):
        fake_supabase.set_table("reports", [{"id": "r1", "reporter_id": "u1", "status": "dismissed"}])
        resp = client.post("/api/moderation/reports/r1/dismiss", json={}, headers=auth(MODERATOR_ID))
        assert resp.status_code == 409

    def test_report_not_found_404s(self, client, fake_supabase):
        fake_supabase.set_table("reports", [])
        resp = client.get("/api/moderation/reports/does-not-exist", headers=auth(MODERATOR_ID))
        assert resp.status_code == 404


class TestForumIntegration:
    """report_post/report_reply now persist through create_report(), not
    just send an email — this pins that #161's core acceptance criterion
    ("reports survive email/provider failure") is actually met."""

    def test_report_post_persists_a_report(self, client, fake_supabase, monkeypatch):
        from api import config
        monkeypatch.setattr(config.settings, "RESEND_API_KEY", "")  # no-op the email
        _seed_post(fake_supabase, post_id="post-1", owner="author-1")

        resp = client.post("/api/forum/posts/post-1/report", headers=auth("reporter-1"))
        assert resp.status_code == 200

        rows = fake_supabase._tables["reports"]
        assert len(rows) == 1
        assert rows[0]["content_type"] == "forum_post"
        assert rows[0]["content_id"] == "post-1"
        assert rows[0]["reporter_id"] == "reporter-1"

    def test_report_reply_persists_a_report(self, client, fake_supabase, monkeypatch):
        from api import config
        monkeypatch.setattr(config.settings, "RESEND_API_KEY", "")
        fake_supabase.set_table("forum_replies", [
            {"id": "reply-1", "user_id": "author-1", "author": "Author", "body": "hi"},
        ])

        resp = client.post("/api/forum/replies/reply-1/report", headers=auth("reporter-1"))
        assert resp.status_code == 200
        assert fake_supabase._tables["reports"][0]["content_type"] == "forum_reply"

    def test_report_own_post_still_rejected(self, client, fake_supabase):
        _seed_post(fake_supabase, post_id="post-1", owner="author-1")
        resp = client.post("/api/forum/posts/post-1/report", headers=auth("author-1"))
        assert resp.status_code == 400
        assert fake_supabase._tables.get("reports", []) == []

    def test_reporting_same_post_twice_conflicts(self, client, fake_supabase, monkeypatch):
        from api import config
        monkeypatch.setattr(config.settings, "RESEND_API_KEY", "")
        _seed_post(fake_supabase, post_id="post-1", owner="author-1")

        first = client.post("/api/forum/posts/post-1/report", headers=auth("reporter-1"))
        second = client.post("/api/forum/posts/post-1/report", headers=auth("reporter-1"))
        assert first.status_code == 200
        assert second.status_code == 409
