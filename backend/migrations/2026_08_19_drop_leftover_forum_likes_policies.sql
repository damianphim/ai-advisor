-- ─────────────────────────────────────────────────────────────────────────────
-- 2026-08-19 — Drop leftover permissive policies on forum_post_likes / forum_reply_likes
--
-- MIGRATION DRIFT, same root cause as 2026_08_11_drop_public_forum_policies.sql:
-- 2026_06_23_rls_forum_and_club_tables.sql created the intended {authenticated}
-- policies on these two tables, but its DROP list only named the NEW policy
-- names — so whatever older, more permissive policy existed before that
-- migration was never removed, and survived alongside it. Postgres OR's
-- permissive policies together, so the most permissive one wins.
--
-- 2026_08_11's fix only covered forum_posts / forum_replies — it never
-- checked forum_post_likes / forum_reply_likes, which had the identical
-- drift. Confirmed against production with the (public) anon key: reading
-- forum_post_likes returned its one real row (user_id, post_id, created_at)
-- with zero auth. forum_reply_likes could not be empirically confirmed the
-- same way (it currently has 0 rows in production either way), but it went
-- through the exact same migration with the exact same DROP-list flaw, so
-- it's fixed here defensively rather than left for whenever it happens to
-- get its first row.
--
-- Unlike the 2026_08_11 fix, this one does NOT guess the leftover policy's
-- name — it drops every existing policy on each table via pg_policies, then
-- recreates only the intended {authenticated} ones. That's more robust than
-- naming candidates and avoids repeating this exact class of drift a third
-- time if the leftover name turns out to be something unexpected.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN
        SELECT policyname FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'forum_post_likes'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.forum_post_likes', pol.policyname);
    END LOOP;

    FOR pol IN
        SELECT policyname FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'forum_reply_likes'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.forum_reply_likes', pol.policyname);
    END LOOP;
END $$;

ALTER TABLE forum_post_likes ENABLE ROW LEVEL SECURITY;
ALTER TABLE forum_reply_likes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "forum_post_likes_select_auth"
ON forum_post_likes FOR SELECT
TO authenticated
USING (true);

CREATE POLICY "forum_post_likes_insert_own"
ON forum_post_likes FOR INSERT
TO authenticated
WITH CHECK (user_id = auth.uid());

CREATE POLICY "forum_post_likes_delete_own"
ON forum_post_likes FOR DELETE
TO authenticated
USING (user_id = auth.uid());

CREATE POLICY "forum_reply_likes_select_auth"
ON forum_reply_likes FOR SELECT
TO authenticated
USING (true);

CREATE POLICY "forum_reply_likes_insert_own"
ON forum_reply_likes FOR INSERT
TO authenticated
WITH CHECK (user_id = auth.uid());

CREATE POLICY "forum_reply_likes_delete_own"
ON forum_reply_likes FOR DELETE
TO authenticated
USING (user_id = auth.uid());


-- ── Verify ───────────────────────────────────────────────────────────────────
-- Expect ONLY the three {authenticated} policies per table, nothing else:
--
--   SELECT tablename, policyname, roles, cmd
--   FROM pg_policies
--   WHERE schemaname='public' AND tablename IN ('forum_post_likes','forum_reply_likes')
--   ORDER BY tablename, policyname;
--
-- Then, with the anon key, this must return 0 rows:
--   SELECT * FROM forum_post_likes;
--   SELECT * FROM forum_reply_likes;
