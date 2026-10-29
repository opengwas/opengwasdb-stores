- **The package version comes from the `v*` git tag, through
  `setuptools-scm`, instead of the literal `0.1.0` in `pyproject.toml` (issue
  #386, the stores half of opengwasdb#333).** Every merge to `main` is tagged
  `vX.Y.Z`, so a release checkout reports the tag, a checkout past it reports a
  development version (`0.2.1.dev4+g1a2b3c4`), and a build with no git metadata
  reports `0.0.0+unknown` -- valid PEP 440, below every release, and obviously
  not one. No Store Release, record or catalog version depends on that string:
  the Analysis Catalog hashes the package's *source files* and keeps its own
  `BUILDER_VERSION`, and every recorded build identity names `opengwasdb`, not
  `ogstores`. The one thing that does report it -- a yardstick result -- records
  the checkout's commit and whether the tree was dirty beside it, because an
  editable install's version is frozen at the commit it was built from. `setuptools-scm` is pinned to `>=8,<9`: 10
  applies the fallback as a base version, which would make a tagless checkout
  report `0.0.1.devN+unknown.g…`.
