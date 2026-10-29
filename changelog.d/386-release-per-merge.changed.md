- **Each merge to `main` is tagged `vX.Y.Z` and published as a GitHub Release
  whose notes are that pull request's fragments, by a workflow that never
  commits to `main` (issue #386, epic #385).** `.github/workflows/release.yml`
  runs on the push to `main` and does nothing but call `tools/release.py plan`
  and then `publish`, so every rule it applies is tested. Runs are serialised
  and never cancelled, `contents: write` is the whole permission set, and a
  merge that arrives while a run is in flight is published by the next run
  instead of being dropped. The version the package reports follows the tag
  through `setuptools-scm`, so a release checkout reports its own release
  number.
