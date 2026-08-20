# Docs index

| Doc | What it's for |
|---|---|
| [architecture/context.md](architecture/context.md) | Domain glossary — canonical terms for Clubs, Managers, degree-planning concepts, etc. Read this before naming anything new. |
| [mobile/handoff.md](mobile/handoff.md) | Capacitor/iOS wrapper: how to build and run it. Historical — `mobile-app` branch is merged into `main`. |
| [mobile/audit.md](mobile/audit.md) | Phase-0 findings that shaped the mobile plan. Predates handoff.md; read that first. |
| [security/review-2026-07-25.md](security/review-2026-07-25.md) | Internal security review — dependency CVEs, IDOR/RLS coverage, webhook signatures, PII paths. |
| [compliance/](compliance/) | Subprocessor risk assessments and related compliance docs. |
| [adr/](adr/) | Architecture decision records — read these for *why*, not just *what*. |
| [agents/](agents/) | Docs for AI agents working in this repo — issue tracker conventions, triage labels, domain-doc policy. |

## here

Operational runbooks — secrets inventory, DNS/DMARC hardening, incident response, infrastructure ownership — live in a private companion repo, not this one, because this repo is public. Ask a maintainer for access if you need them.

## Where things go

If a change to the code could make a doc wrong, the doc lives here, next to the code, so the same PR that changes behavior updates the doc. Live discussion, roadmap debate, and anything not yet decided stays elsewhere — write it up here (or in an [ADR](adr/)) once it's settled.
