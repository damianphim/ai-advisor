# Symbolos — McGill AI Advisor — Agent Context

## What this is

A full-stack web app that helps McGill University students navigate course selection, track degree progress, and get personalized AI advising. Live at symbolos.ca. Student-run project — see `CONTRIBUTING.md` for the "don't break prod" bar.

## Stack

- **Backend**: FastAPI (Python 3.12) — `backend/api/`, served via `uvicorn`, deployed as Vercel serverless functions
- **Frontend**: React 19 + Vite 8 — `frontend/src/`
- **DB**: Supabase (PostgreSQL + auth), Row Level Security on all tables
- **AI**: Anthropic Claude (`claude-haiku-4-5-20251001` by default — `CLAUDE_MODEL`/`CLAUDE_CARDS_MODEL` in `config.py`)
- **Background jobs**: Inngest (transcript/syllabus PDF parsing)
- **Observability**: Sentry (errors), PostHog (product analytics), Langfuse (LLM tracing)
- **Native**: Capacitor wraps the same web build for iOS/Android (`frontend/ios/`, `frontend/android/`)
- **Deploy**: Vercel — frontend and backend deploy as separate projects (`frontend/vercel.json`, `backend/vercel.json`)

## Architecture

```
backend/api/
├── main.py              # FastAPI app: middleware, security headers, rate limiter, router registration
├── config.py             # Settings via pydantic-settings; validates required env vars at startup
├── auth.py                # get_current_user_id, require_self, require_mcgill_email
├── inngest_app.py         # Inngest client + background function registration
├── routes/                # One file per resource; chat.py, cards.py, transcript.py, etc.
│   └── clubs/              # Clubs is its own package (discovery, membership, managers, cron, …)
├── seeds/                  # Degree requirement data, one file per faculty
├── prompts/                # Static prompt .md files, loaded once at startup
│   └── tab_guidance/        # One .md per frontend tab
└── utils/                  # supabase_client, sanitise, cache, lang, verified_user, llm_budget, anomaly

frontend/src/
├── components/Dashboard/   # Main app tabs (ChatTab, CoursesTab, CalendarTab, DegreePlanningView, …)
├── components/Auth/, Landing/, Forum/, Admin/, Legal/, ui/, shared/
├── lib/                     # API clients (api.js, cardsAPI.js, clubsAPI.js, …) + telemetry.js
├── contexts/                # AuthContext, LanguageContext, ThemeContext, TimezoneContext
├── locales/                 # en.js, fr.js, zh.js
└── test/                    # setup.js (vitest env), smoke.test.jsx
```

- Auth: Supabase JWT. Every protected route takes `Depends(get_current_user_id)`, and routes operating on a specific user's data call `require_self(current_user_id, user_id)`.
- Chat context (`build_system_context()` in `chat.py`): base student data (favorites/completed/current/calendar) is cached per-user for 5 minutes in-memory; static prompt files load once at process start. History sent to Claude is capped by `settings.CHAT_CONTEXT_MESSAGES` (default 6).
- Rate limiting (`main.py`): `SupabaseRateLimiter`, a Postgres-backed sliding-window limiter (table `rate_limits`), tiered per endpoint cost — `/chat/*` gets `CHAT_RATE_LIMIT_PER_MINUTE` (50/IP), Claude-heavy endpoints (cards generate/stream/ask/retranslate, electives/recommend, transcript/syllabus parse) get `CLAUDE_RATE_LIMIT_PER_MINUTE` (30/IP), everything else gets `RATE_LIMIT_PER_MINUTE` (100/IP). Falls back to a conservative 3rpm in-memory limiter per instance if Supabase is unreachable. Also enforces a per-user limit (half the IP limit) so one user can't exhaust a shared-IP bucket.
- Async jobs: transcript/syllabus PDF parsing returns `202 {job_id}` immediately; frontend polls `/api/jobs/{id}` (`pollJob` in `ProfileSetup.jsx`/`TranscriptUpload.jsx`). Requires the local Inngest dev server (`:8288`) or it 503s.
- `/api/clubs` and `/api/courses/search` are edge-cached with `Cache-Control: public` — see "Known gotchas" below.

## Conventions

- **Adding a route**: create `backend/api/routes/<name>.py`, register it in `main.py`'s `include_router` block.
- **Editing AI prompts**: edit `.md` files under `backend/api/prompts/`; restart the server (loaded once at startup).
- **Editing degree requirements**: edit `backend/api/seeds/<faculty>_degree_requirements.py`, then reseed with `POST /api/degree-requirements/seed?faculty=<name>` using `Authorization: Bearer <CRON_SECRET>`. Seed format: list of program dicts with `blocks` → `{block_type, credits_needed, courses: [{course_code, credits, title}]}`.
- **Migrations**: `backend/migrations/*.sql`, date-prefixed (`YYYY_MM_DD_description.sql`), must be idempotent (`IF NOT EXISTS` / guard clauses) and applied manually in Supabase — there's no migration runner. After a schema change, regenerate `backend/migrations/SCHEMA.sql` via `pg_dump --schema-only --no-owner --no-privileges "$SUPABASE_DB_URL" > backend/migrations/SCHEMA.sql`.
- **Domain vocabulary**: `docs/architecture/context.md` defines canonical terms (Club, Manager, Owner, Join Request, etc.) with terms to avoid — read it before touching the clubs feature.
- **ADRs**: `docs/adr/000N-*.md` for structural decisions (e.g. `0002-clubs-manager-storage.md` explains why the Manager role is stored as `role: "admin"` internally).

## Security / sensitive areas

- **Auth on every route**: `Depends(get_current_user_id)` + `require_self()` for user-scoped data. `require_mcgill_email()` gates McGill-only features and checks `auth.users.email` (Supabase-verified identity), never the editable `users` table column — this was a real fixed vulnerability (see `auth.py` SEC comment).
- **PII / transcript redaction** (`backend/api/routes/transcript.py`): text is extracted locally with `pypdf` and student-ID/permanent-code patterns (`26\d{7}`, `[A-Z]{4}\d{8}`) are stripped by `_redact_transcript_text` **before** anything is sent to Claude — the raw PDF is never sent. PDFs with no extractable text layer (scanned/photographed) are refused via `UnreadableTranscriptError`, not sent unredacted. `_scrub_pii` on the Claude output is a defense-in-depth backstop. Keep all three layers working if you touch this file.
- **Prompt injection defense** (`backend/api/utils/sanitise.py`): `sanitise_user_message` / `sanitise_context_field` — normalizes l33tspeak, unicode lookalikes, diacritics before pattern matching. Explicitly a best-effort pre-filter, not the primary defense (the system prompt's own refusal instructions are).
- **Security invariants are tested**: `backend/tests/test_security.py`. Don't weaken these to make a test pass — fix the underlying code instead.
- **Admin identity**: two hardcoded admin user IDs live in both `auth.py` (`_ADMIN_USER_IDS`) and `routes/clubs/permissions.py` (`ADMIN_USER_IDS`) — kept in sync manually, no single source of truth.
- **New tables need RLS.** RLS migrations are named `*_rls_*` in `backend/migrations/`.
- **CSP / security headers**: set in `main.py`'s `add_security_headers` middleware — no `unsafe-inline` for scripts, `frame-ancestors 'none'`. If you add a new external script/connect source, it needs to go in the CSP there.
- **Secrets**: `.githooks/pre-commit` runs a naive regex secret scan on staged diffs (`sk-ant-`, `re_`, JWT-shaped strings, `service_role`); CI additionally runs gitleaks (`.gitleaks.toml`) on every push/PR.

## How to validate changes

```bash
# One-time: enable pre-commit hooks
git config core.hooksPath .githooks

# Backend (from backend/, with venv active)
python -m compileall -q api/
ruff check --select=E9,F63,F7,F82 api/     # errors only — style isn't enforced on this legacy codebase
pytest tests/ -v                            # hermetic — mocks supabase/anthropic/resend, runs in <1s

# Frontend (from frontend/)
npm run lint
npm test          # vitest run
npm run build
```

Local full-stack dev needs three processes running together: backend (`uvicorn api.main:app --reload --port 8000`), the Inngest dev server (`:8288`, required for transcript/syllabus upload — otherwise those endpoints 503), and frontend (`npm run dev`, `:5173`).

CI (`.github/workflows/ci.yml`) runs: backend compile + ruff (errors only) + `pip-audit` (blocks on any known CVE) + `pytest tests/`; frontend `npm audit --audit-level=critical` + lint (non-blocking) + `npm test` + `npm run build` + Lighthouse budget (non-blocking, `lighthouserc.json`); a repo-wide gitleaks secret scan. The intended `main` ruleset requires a pull request, an approving review, the blocking Backend, Frontend, and Secret scan checks, and a branch current with `main`. Confirm the ruleset is active and those checks apply to the pull request's current commit before merging.

"Done" for a UI change means: lint + test + build pass, AND the feature was exercised in a running browser (golden path + edge cases) — type checks and unit tests verify correctness, not that the feature actually works.

## Known gotchas

- **`/api/clubs` and `/api/courses/search` are public-edge-cached** (`Cache-Control: public, s-maxage=...`) — see `test_clubs.py::test_list_clubs_sets_public_cache_headers` and `test_security.py::test_clubs_response_is_public_cacheable`. Never add per-user data to these responses; it would leak across users on the CDN.
- **No `.claude/launch.json` exists in this checkout** despite being referenced as the multi-process local-dev launcher — if you need it, check whether it's gitignored/local-only or ask the user; don't assume its absence means dev tooling is broken.
- **Preview Vercel deployments don't get `INNGEST_*` env vars** (Production-only) — `main.py` deliberately skips serving `/api/inngest` there to avoid noisy "missing signing key" Sentry errors; this is intentional, not a bug. Local dev still serves it because the local Inngest dev server skips signature checks entirely.
- **The Manager club role is stored as `role: "admin"`** in the DB — a historical misnomer unrelated to the platform-wide Admin role. See `docs/adr/0002-clubs-manager-storage.md` before assuming `role == "admin"` means platform admin.
- **`ruff` only checks `E9,F63,F7,F82`** (syntax errors, undefined names) in CI and the pre-commit hook, not the full style ruleset — the codebase predates ruff adoption, so don't expect or enforce broader lint cleanliness in unrelated diffs.
- **Frontend lint (`npm run lint`) is non-blocking in CI** (`|| true`) but tests and build are blocking.
- **The fork (`alexdduda/ai-advisor`) and upstream (`damianphim/symbolos`) do not stay in sync** — local `main`/`origin/main` tracks the fork and can be far behind. Always branch from a freshly-fetched `upstream/main`, not local `main`. Full detail in `CLAUDE.md`.

## Lessons learned (append here over time)

- (none yet — append here when an agent gets something wrong in this repo)
