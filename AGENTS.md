# Working in this repository

Read [CONTRIBUTING.md](./CONTRIBUTING.md) before making changes. Work targets
`main`: every merge there is a version, tagged by CI, and a pull request that
changes a source (`src/`, `workflow/`, `tools/`, `resources/`, `stores/`) adds a
`changelog.d/` fragment or carries the `no-changelog` label.

## Merging

A ruleset protects `main`: a pull request, and green `pixi test + docs smoke
test` and `changelog fragment` checks, with no approval required. **Merge your
own pull request** once its work is finished: for an orchestrate run, after its
final review. Run `gh pr merge <n> --auto --merge`, and GitHub merges it when
the checks pass. Never `--admin`, never a draft. An opengwasdb pin move merges
itself this way, whichever engine version or `format_version` it moves to.

**Leave it for the owner instead**, and name the reason in the PR body, when it:

- adds a `breaking` changelog fragment;
- adds or supersedes an ADR whose decision the owner has not made. An ADR that
  cites the owner's ruling (who, when, where) records the owner's decision;
- changes `.github/workflows/`. CI is the reviewer, so its configuration is the
  owner's.

Merging never approves a Store build operation. One that publishes, replaces or
deletes a Store runs only once the owner has approved its command, host, budget
and publication effect (the orchestrate skill's Store build gates).

The `changelog fragment` check reads labels only when it runs. Add
`no-changelog` when you open the PR; if you add it later, close and reopen the
PR to re-run the check.

## Agent skills

**Issue tracker.** GitHub issues, here and in `opengwas/opengwasdb`. Status
lives only on the [OpenGWASDB project board](https://github.com/orgs/opengwas/projects/1).
Its README holds the conventions: workstreams, milestones, epics, tickets as
sub-issues, and triage labels. Read it with
`gh project view 1 --owner opengwas --format json --jq .readme` and follow it.
Don't copy it here.

**Domain docs.** Single-context: [CONTEXT.md](./CONTEXT.md) and
[docs/adr/](./docs/adr/).
