- **A pull request now adds a changelog fragment instead of editing a shared
  changelog section, and CI refuses a source change without one (issue #386,
  epic #385).** `changelog.d/<slug>.<type>.md` -- type `breaking`, `added`,
  `changed`, `fixed` or `internal` -- and the merge to `main` publishes that
  pull request's fragments as the version's release notes, so parallel pull
  requests never conflict over one file. A source is a change under `src/`,
  `workflow/`, `tools/`, `resources/` or `stores/`; `docs/` alone needs none. A
  pull request with no user-visible effect says so with the new `no-changelog`
  label, which `tools/check_changelog.py` honours as tested code rather than by
  skipping the CI job, and which does not excuse a fragment the release cannot
  read.
