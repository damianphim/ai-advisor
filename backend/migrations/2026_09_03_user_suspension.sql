-- ─────────────────────────────────────────────────────────────────────────────
-- 2026-09-03 — Trust & safety: proportionate user restrictions (suspension)
--
-- damianphim/symbolos#161, Phase 2b. Adds the ability for a moderator to
-- suspend a user reported via content_type == "user" (the one case
-- remove_content explicitly rejects, since there's no content row to
-- delete for a report about an account).
--
-- Enforcement is deliberately NOT a check in get_current_user_id (which
-- runs on every single authenticated request in the app and already makes
-- one Supabase call for JWT verification — adding a second DB read there
-- would roughly double network calls app-wide). A suspension is a
-- targeted restriction on posting, not a full account lockout, so it's
-- checked only at the specific content-creation endpoints where abuse
-- actually happens (see api/utils/moderation.py's require_not_suspended
-- and its call sites), matching this codebase's existing pattern of
-- inline per-route checks (is_email_verified, check_and_record_llm_usage)
-- rather than blanket middleware.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_suspended     boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at     timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason text;

CREATE INDEX IF NOT EXISTS users_is_suspended_idx ON users (id) WHERE is_suspended = true;


-- Widen moderation_actions.action to allow 'user_restricted'. Matched by
-- COLUMN reference (pg_attribute/conkey), not by parsing rendered
-- constraint text — see 2026_08_29_moderation_content_removal.sql's own
-- fix commit for why the text-matching approach silently fails (Postgres
-- rewrites CHECK (col IN (...)) into col = ANY (ARRAY[...]) when storing
-- it, so a LIKE '%IN%' pattern never matches).
DO $$
DECLARE
    con RECORD;
    action_attnum smallint;
BEGIN
    SELECT attnum INTO action_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.moderation_actions'::regclass
      AND attname = 'action';

    FOR con IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'public.moderation_actions'::regclass
          AND contype = 'c'
          AND action_attnum = ANY(conkey)
    LOOP
        EXECUTE format('ALTER TABLE public.moderation_actions DROP CONSTRAINT %I', con.conname);
    END LOOP;
END $$;

ALTER TABLE moderation_actions ADD CONSTRAINT moderation_actions_action_check
    CHECK (action IN (
        'assigned', 'unassigned', 'note_added', 'status_changed',
        'content_removed', 'user_restricted'
    ));


-- ── Verify ───────────────────────────────────────────────────────────────────
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'users' AND column_name LIKE 'suspended%' OR column_name = 'is_suspended';
--
--   SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid = 'public.moderation_actions'::regclass AND contype = 'c';
-- Expect exactly ONE row back, with 'user_restricted' in its list.
