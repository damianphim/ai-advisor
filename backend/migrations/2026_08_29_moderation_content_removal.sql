-- ─────────────────────────────────────────────────────────────────────────────
-- 2026-08-29 — Trust & safety: allow 'content_removed' as a moderation action
--
-- damianphim/symbolos#161, Phase 2a (content removal only — user
-- restrictions are a separate follow-up, deliberately not bundled here
-- since it needs its own decision about where suspension gets enforced).
--
-- moderation_actions.action's CHECK constraint (from
-- 2026_08_26_trust_safety_reports.sql) only allowed
-- 'assigned'/'unassigned'/'note_added'/'status_changed' — Phase 1
-- deliberately shipped with only "dismiss" as a resolution path. This
-- widens it to also allow 'content_removed', logged when a moderator
-- deletes the reported row via POST /api/moderation/reports/{id}/resolve.
--
-- The constraint name isn't guessed — CREATE TABLE's inline CHECK gets an
-- auto-generated name that could plausibly differ across how the original
-- migration was actually applied, so this looks it up via pg_constraint
-- instead, the same approach used in
-- 2026_08_19_drop_leftover_forum_likes_policies.sql for exactly this kind
-- of "don't assume a name" robustness.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'public.moderation_actions'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%action%IN%'
    LOOP
        EXECUTE format('ALTER TABLE public.moderation_actions DROP CONSTRAINT %I', con.conname);
    END LOOP;
END $$;

ALTER TABLE moderation_actions ADD CONSTRAINT moderation_actions_action_check
    CHECK (action IN ('assigned', 'unassigned', 'note_added', 'status_changed', 'content_removed'));


-- ── Verify ───────────────────────────────────────────────────────────────────
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid = 'public.moderation_actions'::regclass AND contype = 'c';
-- Expect the CHECK's IN-list to include 'content_removed'.
