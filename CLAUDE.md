# Claude guidance for Symbolos

Read [`AGENTS.md`](AGENTS.md) before changing this repository. It is the
canonical source for architecture, security invariants, validation commands,
and repository-specific gotchas. Do not duplicate that material here; update
`AGENTS.md` when shared agent guidance changes.

Also read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the human workflow around
issues, branches, pull requests, reviews, and merges.

## Claude-specific workflow

- Before starting work, inspect open pull requests and the current working tree
  so concurrent work is not duplicated or overwritten.
- This checkout may be shared with another session. Prefer an isolated worktree
  for non-trivial changes, and check the active branch and status before editing.
- Start each branch from a freshly fetched `damianphim/symbolos` `main`. In a
  fork-based checkout, fetch the canonical repository as `upstream` and branch
  from `upstream/main`; do not assume the fork's `main` is current.
- Keep changes scoped to one issue or outcome. Record unrelated discoveries in
  a GitHub issue rather than expanding the pull request silently.
- Never merge based only on local results. Confirm the required GitHub checks
  passed on the pull request's current commit and that the branch is mergeable.
- Do not put credentials, private operational context, personal contact
  details, or provider ownership information in this public repository. Those
  belong in the private operational repository.

If Claude discovers a durable repository-specific lesson, add it to the
appropriate section of `AGENTS.md`. If the lesson changes how humans
collaborate, update `CONTRIBUTING.md` as well.
