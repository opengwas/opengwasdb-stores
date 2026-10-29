# Changelog fragments

Every pull request that changes a **source** adds **one or more fragments**
here, and every merge to `main` becomes a version whose release notes are
exactly the fragments that merge added. There is no `Unreleased` section to
edit: the history up to the cutover is in git, and each version's notes are its
GitHub Release.

A source is a change under `src/`, `workflow/`, `tools/`, `resources/` or
`stores/` — the code, the wiring, the inputs a Release Bundle is generated from,
and the bundles themselves. `docs/` alone is not a source: a report, a
specification or an ADR changes no Store, no generator and no tool.

A pull request with no user-visible effect is the exception, and it says so with
the **`no-changelog`** label on the pull request, not with an empty fragment.
`tools/check_changelog.py` enforces both, in CI and locally.

## The file

`changelog.d/<slug>.<type>.md`, directly in this directory.

- **`<slug>`** names the change; `386-version-from-tags` and
  `312-worktree-prune` are the shape. It only has to be unique among the
  fragments a pull request adds: the file is never edited or deleted, and it
  stays until it disappears with the branch.
- **`<type>`** is one of the five below. It is not decoration — it is the
  version bump (see "What the type decides").
- The **body** is Markdown, written the way a release note is: lead with the
  change and its consequence, name the issue and ADR, and give the measured
  numbers when there are numbers. It becomes the bullet in the release notes as
  written, so there is no need to repeat it anywhere else.

```markdown
- **`pixi run worktree-prune --apply` removes a worktree only when it is
  merged, clean and unused (issue #312).** It fetches `origin` first, checks
  each worktree again before removing it, and reports the reason and the
  matched path for every one it keeps.
```

A fragment that is empty, or whose name the release cannot read, is refused
rather than skipped: a silently omitted note is a release that does not say
what it changed.

## The five types

| type | write it when | bump |
|---|---|---|
| `breaking` | a Release Bundle field, a task name or a CLI surface changes incompatibly | minor |
| `added` | a new capability, backwards compatible | minor |
| `changed` | an existing behaviour changes | minor |
| `fixed` | a defect is fixed, and nothing else changes | patch |
| `internal` | the change is real but invisible to an operator or a reader | patch |

## What the type decides

Below `1.0.0` the minor number carries breaking changes, as `CONTRIBUTING.md`
says. `tools/release.py` reads the fragment types a pull request added and
picks the strongest bump:

- any `breaking`, `added` or `changed` → the minor number moves (`0.1.2` →
  `0.2.0`);
- only `fixed` or `internal`, or no fragment at all → the patch number moves
  (`0.1.0` → `0.1.1`).

`.github/workflows/release.yml` runs that script on the merge to `main`, tags
the merge commit `vX.Y.Z` and publishes a GitHub Release whose notes are the
fragments, with a link to the pull request. It never commits to `main`, and it
publishes every merge that is still owed a version rather than only the one it
was triggered by, so a lost run cannot lose a version.
