-- ─────────────────────────────────────────────────────────────────────────────
-- 2026-08-26 — Trust & safety: persistent reports + moderator action log
--
-- damianphim/symbolos#161, Phase 1 (engineering backbone only — see the PR
-- description for what's deliberately deferred: resolution actions like
-- content removal / user suspension, and all frontend UI).
--
-- Today, forum report_post/report_reply only send an email to
-- ADMIN_EMAILS and persist nothing — if the email send fails, the report
-- is silently lost, and there is no queue to review it in even when it
-- succeeds. This adds durable storage for reports across every
-- user-generated content surface, plus an append-only log of moderator
-- actions on them.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reports (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id       uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    content_type      text        NOT NULL CHECK (content_type IN (
                                       'forum_post', 'forum_reply', 'club',
                                       'club_event', 'club_announcement', 'user'
                                   )),
    -- text, not uuid: covers every content type (including reporting a
    -- user directly, where content_id is that user's auth.users id) with
    -- one column, and survives the reported row itself being deleted later.
    content_id        text        NOT NULL,
    reason_category   text        NOT NULL CHECK (reason_category IN (
                                       'spam', 'harassment', 'hate_speech',
                                       'misinformation', 'inappropriate_content',
                                       'impersonation', 'other'
                                   )),
    context           text,
    -- Evidence snapshot: the reported content as it existed at report
    -- time, since the underlying row can be edited or deleted before a
    -- moderator reviews it.
    content_snapshot  jsonb,
    status            text        NOT NULL DEFAULT 'open' CHECK (status IN (
                                       'open', 'in_review', 'dismissed', 'resolved'
                                   )),
    assigned_to       uuid        REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (reporter_id, content_type, content_id)
);

ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
-- Deliberately NO client-facing policies at all — every read/write goes
-- through the backend using the service_role key (matches audit_log /
-- rate_limits). Reporter identity must never be exposed to the reported
-- user (an acceptance criterion of #161); an RLS policy letting the
-- content owner read reports about their own content would violate that
-- directly, so there is no SELECT policy for `authenticated` here at all.

CREATE INDEX IF NOT EXISTS reports_status_idx  ON reports (status, created_at);
CREATE INDEX IF NOT EXISTS reports_content_idx ON reports (content_type, content_id);
CREATE INDEX IF NOT EXISTS reports_assigned_idx ON reports (assigned_to) WHERE assigned_to IS NOT NULL;


-- moderation_actions: immutable audit trail of moderator activity.
-- Deliberately a SEPARATE table from audit_log, not a reuse of it —
-- audit_log's own header comment scopes it to PIPEDA/Law-25 sensitive
-- DATA-ACCESS logging (transcript reads, exports, account deletion), a
-- different concern with likely a different retention policy than
-- trust-and-safety ACTIONS. Conflating the two would make a future
-- retention-policy decision (#161's own acceptance criteria) harder to
-- apply correctly to one without affecting the other.
CREATE TABLE IF NOT EXISTS moderation_actions (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id    uuid        NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    moderator_id uuid        REFERENCES auth.users(id) ON DELETE SET NULL,
    action       text        NOT NULL CHECK (action IN (
                                  'assigned', 'unassigned', 'note_added', 'status_changed'
                              )),
    details      jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE moderation_actions ENABLE ROW LEVEL SECURITY;
-- Append-only, service_role-write-only, no client policies at all —
-- matches audit_log's own pattern exactly. Moderators read this through
-- the backend API, never directly via PostgREST.

CREATE INDEX IF NOT EXISTS moderation_actions_report_idx ON moderation_actions (report_id, created_at);


-- ── Verify ───────────────────────────────────────────────────────────────────
-- With zero SELECT policies, RLS filters every row out for anon/authenticated
-- — PostgREST returns 200 with an empty array, not a 401/403 (this matches
-- audit_log's own security model: grants exist at the schema level, RLS is
-- what actually withholds the rows). Both of these must return `[]` when
-- run with the anon key, never real report rows:
--   SELECT * FROM reports;
--   SELECT * FROM moderation_actions;
