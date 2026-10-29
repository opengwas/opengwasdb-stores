- **The package version comes from the `v*` git tag, and every merge to `main`
  is tagged and published as a GitHub Release whose notes are that pull request's
  changelog fragments (issue #386, the stores half of opengwasdb#333).**
  `pyproject.toml` no longer holds `0.1.0`: `setuptools-scm` reports the tag, a
  development version past it (`0.2.1.dev4+g1a2b3c4`), or `0.0.0+unknown` with
  no git metadata, with `git describe`'s glob restricted so a stray tag such as
  `99.0.0` cannot break a build. A pull request adds
  `changelog.d/<slug>.<type>.md` -- type `breaking`, `added`, `changed`, `fixed`
  or `internal` -- and `tools/check_changelog.py` refuses a source change
  without one, `no-changelog` exempting only the requirement, never a fragment
  the release cannot read; the type sets the bump (`breaking`, `added` or
  `changed` is a minor, `fixed`, `internal` or no fragment a patch, and from no
  tag the first feature release is `0.1.0`). `tools/release.py` owns that and the
  notes, and `.github/workflows/release.yml` only calls its `plan` and
  `publish`: each run publishes every first-parent commit after the newest tag
  that has no tag of its own and every tagged commit whose Release is missing,
  so a lost run is caught up, and each publication step is skipped when the
  remote says it is already done, so a run that dies between the tag and its
  Release is resumed by the next one. A yardstick result records the `ogstores`
  commit and whether the tree was dirty beside the version, because an editable
  install's version is frozen at the commit it was built from.
