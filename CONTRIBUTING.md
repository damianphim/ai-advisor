# Contributing to Symbolos

Thanks for helping out. This is a small student-run project; the bar is
"don't break prod and leave the code a little better than you found it."

## One-time setup

```bash
git clone <repo>
cd ai-advisor

# Enable the pre-commit hooks (Python compile + lint + secret scan).
git config core.hooksPath .githooks

# Backend
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio ruff   # dev tools

# Frontend
cd ../frontend
npm install
```

Copy `.env.local.example` → `.env.local` and fill in the keys through the
maintainer-approved sharing method. Never put credentials in GitHub issues,
pull requests, chat transcripts, or commits.

## Running locally

```bash
# Backend (from backend/)
uvicorn api.main:app --reload --port 8000

# Frontend (from frontend/)
npm run dev          # http://localhost:5173
```

## Before you open a PR

The pre-commit hook runs these automatically, but you can run them by hand:

```bash
# Backend
cd backend
python -m compileall -q api/
ruff check --select=E9,F63,F7,F82 api/
pytest tests/ -v

# Frontend
cd frontend
npm run lint
npm run build
```

CI runs the same checks plus dependency audits, a secret scan, and a Lighthouse
budget. The intended `main` ruleset requires a pull request, one approving
review, a branch current with `main`, and the blocking Backend, Frontend, and
Secret scan checks. Confirm the ruleset is active before relying on enforcement,
and never merge while a required or deployment check is pending or failing.

## Team workflow

GitHub is the source of truth for planned and active engineering work. Use the
shared project board for prioritization and GitHub Issues for decisions and
acceptance criteria. Google Docs can support brainstorming, but an item is not
ready for implementation until its actionable requirements are captured in an
issue.

1. Search existing issues and pull requests before starting.
2. Create or claim an issue with one owner, a concrete outcome, and acceptance
   criteria. Small dependency and documentation maintenance may skip an issue
   when the pull request fully explains the change.
3. Create a short-lived branch from a freshly fetched canonical `main`.
4. Open a focused pull request early. Link the issue and state the change, the
   reason, validation performed, and any rollout or migration steps.
5. Get review from another developer and resolve conversations. Authors do not
   approve their own work.
6. Merge only after the required checks pass on the current commit. Prefer
   squash merge, then delete the branch.
7. Verify production when behavior, infrastructure, data, or deployment changed
   and record any follow-up work in an issue.

Do not maintain a long-lived `dev` branch. Short-lived branches and preview
deployments provide isolation while keeping `main` continuously releasable.

## Branch + commit conventions

- Fetch before branching; do not branch from a stale local or fork `main`.
- Use a descriptive prefix: `feat/clubs-search`, `fix/calendar-tz`,
  `docs/contributor-workflow`, or `chore/deps`.
- Write a real commit message — what changed and *why*. The "why" is the
  part future-you will thank present-you for.
- Keep generated or AI-assisted changes under the same human review and testing
  standard as handwritten changes.

## Pull request checklist

- The pull request has one clear outcome and links its issue when one exists.
- Tests cover changed behavior, and the relevant commands above pass locally.
- User-facing text is present in English, French, and Chinese.
- Database migrations are idempotent and include the updated schema dump.
- Screenshots or a short recording accompany visible UI changes.
- No credential, private operational detail, or personal information appears in
  the diff or discussion.
- Deployment, migration, feature-flag, and rollback steps are documented when
  applicable.

## Public and private documentation

Keep architecture, development conventions, product behavior, and public
security guidance in this repository. Keep credential inventories, account
ownership, billing, DNS/mail state, incident procedures, and personal contact
details in the private operational repository. `AGENTS.md` is the canonical
shared instruction file for coding agents; tool-specific files such as
`CLAUDE.md` should point to it and contain only tool-specific additions.

## Where things live

| Path | What |
|---|---|
| `backend/api/routes/` | One file per resource (chat, clubs, users, …) |
| `backend/api/utils/` | Shared helpers (supabase client, sanitise, email, telemetry gates) |
| `backend/migrations/` | SQL migrations — date-prefixed, idempotent, run manually in Supabase |
| `frontend/src/components/` | UI, grouped by feature |
| `frontend/src/lib/` | API clients + telemetry |
| `frontend/src/locales/` | EN / FR / ZH strings — **all three must be updated together** |

## House rules that matter

1. **Security invariants are tested.** `backend/tests/test_security.py`
   asserts the audit fixes (auth on every route, PII stripping, etc.).
   Don't weaken them to make a test pass — fix the code.
2. **i18n is not optional.** Any user-facing string goes in all three
   locale files. A missing key renders the raw `key.name` to users.
3. **No secrets in the repo.** The pre-commit hook + gitleaks will catch
   the obvious shapes, but be careful.
4. **Migrations are manual + idempotent.** Add `IF NOT EXISTS` / guard
   clauses so re-running is safe. After a schema change, dump the schema
   (see below).
5. **Don't add per-user data to edge-cached endpoints** (`/api/clubs`,
   `/api/courses/search`). They're `public`-cached — per-user data would
   leak across users. There's a CI test guarding this.

## Updating the schema dump

After any migration, regenerate the committed schema reference:

```bash
# Requires the Supabase connection string (read-only is fine).
pg_dump --schema-only --no-owner --no-privileges \
  "$SUPABASE_DB_URL" > backend/migrations/SCHEMA.sql
git add backend/migrations/SCHEMA.sql
```

This keeps a human-readable record of the DB shape in the repo so nobody
has to open Supabase Studio to answer "what columns does X have."

## Reporting a security issue

Don't open a public issue. See `/.well-known/security.txt` —
email the address there. We aim to respond within 72 hours.
