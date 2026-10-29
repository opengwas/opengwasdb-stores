- **Each merge to `main` is tagged `vX.Y.Z` and published as a GitHub Release
  whose notes are that pull request's fragments, by a workflow that never
  commits to `main` (issue #386, epic #385).** `.github/workflows/release.yml`
  runs on the push to `main`, finds the merged pull request, and hands its
  fragments to `tools/release.py`; it tags the merge commit, publishes the
  notes, and stops if `HEAD` is already tagged, so a re-run or a hand-made tag
  cannot release a commit twice. Runs are serialised and never cancelled, so
  two quick merges become two consecutive versions on their own commits. The
  version the package reports follows the tag through `setuptools-scm`, so a
  release checkout reports its own release number.
