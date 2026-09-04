-- ─────────────────────────────────────────────────────────────────────────────
-- 2026-09-05 — Close direct-PostgREST authorization gaps flagged by an
-- external security audit (Codex), independently verified against the
-- actual code paths before writing this fix.
--
-- Root cause, all five: RLS controls ROWS, not columns or values beyond
-- what a WITH CHECK expression tests. Several policies here only checked
-- "is this your own row" and never constrained WHICH columns/values could
-- be set, so a caller with nothing but a valid Supabase JWT — no FastAPI
-- involved at all — could reach privilege paths the backend's Python code
-- was the only thing actually gating.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────


-- ── 1. user_clubs: block self-inserting as admin/owner into ANY club ────────
--
-- user_clubs_insert_own only checked `user_id = auth.uid()` — no
-- restriction on club_id or role. Any authenticated user could INSERT
-- themselves into an arbitrary (including private) club with
-- role='admin', immediately granting manager-level access: read private
-- club/member data, create/edit/delete events and announcements, manage
-- other members. Confirmed unused for anything but this: the backend's
-- own public-club join flow (clubs/membership.py, join_club) inserts via
-- the RLS-scoped client but never sets `role`, relying on it landing
-- NULL — application code already treats a NULL role as "member"
-- (clubs/members.py: `m.get("role") or "member"`). Narrowing the check to
-- require role IS NULL or 'member' preserves that flow exactly and closes
-- the escalation path; every legitimate admin/owner grant already goes
-- through service_role (clubs/managers.py, clubs/members.py's
-- update_member_role) and is unaffected.
ALTER POLICY "user_clubs_insert_own" ON user_clubs
WITH CHECK (user_id = auth.uid() AND (role IS NULL OR role = 'member'));

-- user_clubs_update_manager's self-branch (`user_id = auth.uid()`) had NO
-- WITH CHECK at all — a self-row update could set role to 'admin'/'owner'
-- directly, a second path to the same escalation. The only legitimate
-- self-service UPDATE on this table (clubs/membership.py,
-- toggle_calendar_sync) only ever touches `calendar_synced`; every
-- role-changing UPDATE already goes through service_role
-- (clubs/members.py update_member_role), which bypasses grants entirely.
-- Column-level REVOKE/GRANT closes this regardless of which branch of the
-- USING clause matched — the same defense-in-depth pattern already used
-- for clubs/club_events/club_announcements (2026_08_10_clubs_column_grants.sql).
REVOKE UPDATE ON user_clubs FROM authenticated;
GRANT UPDATE (calendar_synced) ON user_clubs TO authenticated;


-- ── 2. users: only cookie_consent is a legitimate self-service UPDATE ───────
--
-- users_update_own checked `id = auth.uid()` with no column restriction —
-- any authenticated user could UPDATE their own is_suspended,
-- email_verified, suspended_at/suspended_reason, or any other column
-- directly, undoing a moderator's suspension or self-verifying an
-- unconfirmed email. The only backend code path that writes to `users`
-- via the RLS-scoped client (not service_role) is the cookie-consent
-- endpoint (users.py, POST /{user_id}/consent) — every other write
-- (suspension, email verification, profile edits, etc.) already goes
-- through service_role and is unaffected by this.
REVOKE UPDATE ON users FROM authenticated;
GRANT UPDATE (cookie_consent, cookie_consent_at) ON users TO authenticated;


-- ── 3. clubs: drop unused direct-write policies that contradict their own
--    migration's stated intent ──────────────────────────────────────────────
--
-- 2026_06_01_sec_rls_clubs_pii.sql's own comment says "Writes go through
-- the API (service role only), never via anon/auth" — but the policies it
-- then created granted INSERT/UPDATE/DELETE TO authenticated anyway,
-- directly contradicting that stated intent. Confirmed via grep that
-- every INSERT/UPDATE/DELETE on `clubs` anywhere in the backend already
-- uses get_supabase() (service_role): club creation only happens via the
-- admin-token-gated submission-approval flow (clubs/submissions.py),
-- edits/deletes via clubs/discovery.py, clubs/members.py (ownership
-- transfer), clubs/translation.py, and the stale-club cleanup cron
-- (clubs/cron.py) — none of them use the RLS-scoped client for this
-- table. These policies were reachable by any authenticated user via
-- direct PostgREST and let them create clubs with arbitrary is_verified,
-- or (via clubs_update_managers + a self-escalated user_clubs role from
-- finding #1) edit any club's fields directly.
DROP POLICY IF EXISTS "clubs_insert_managers" ON clubs;
DROP POLICY IF EXISTS "clubs_update_managers" ON clubs;
DROP POLICY IF EXISTS "clubs_delete_owner"    ON clubs;


-- ── 4. forum_posts / forum_replies: drop direct-insert policies ────────────
--
-- Paired with a code change (api/routes/forum.py's create_post/
-- create_reply now insert via service_role instead of the RLS-scoped
-- client). Before that change, these policies only checked
-- `user_id = auth.uid()` — nothing stopped a caller from hitting
-- PostgREST directly with a valid JWT and skipping every Python-side
-- check create_post/create_reply perform first: require_not_suspended,
-- is_email_verified, require_mcgill_email, the anomaly/rate-limit
-- counter, and HTML-escaping the title/body. A suspended or unverified
-- account, or one over the rate limit, could still post by bypassing
-- FastAPI entirely. UPDATE/DELETE policies on these two tables are
-- intentionally left as-is — editing or deleting your own existing post
-- doesn't carry the same new-content-creation risk this fix targets.
DROP POLICY IF EXISTS "forum_posts_insert_own"   ON forum_posts;
DROP POLICY IF EXISTS "forum_replies_insert_own" ON forum_replies;


-- ── Verify ───────────────────────────────────────────────────────────────────
-- With the anon/authenticated key (not service_role):
--   1. INSERT INTO user_clubs (user_id, club_id, role) VALUES (auth.uid(), '<any club>', 'admin');
--      -> must be rejected (42501 or 0 rows, not a successful admin row).
--   2. UPDATE users SET is_suspended = false WHERE id = auth.uid();
--      -> must be rejected with 42501 (column privilege), not silently succeed.
--   3. INSERT INTO clubs (name, is_verified, created_by) VALUES ('x', true, auth.uid());
--      -> must be rejected — no INSERT policy remains for `authenticated`.
--   4. INSERT INTO forum_posts (user_id, title, body) VALUES (auth.uid(), 'x', 'x');
--      -> must be rejected — no INSERT policy remains for `authenticated`.
-- Then confirm the app's own normal flows still work end to end: joining a
-- public club, toggling a club's calendar sync, accepting the cookie
-- consent banner, and creating a forum post/reply as a verified,
-- non-suspended user.
