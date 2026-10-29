- **The `opengwasdb` pin names the release tag `v0.3.0` instead of a raw commit
  (issue #387, epic #385).** opengwasdb now tags every merge to `main` and takes
  its version from the tag, so the installed version names the release it was
  built from rather than a string hardcoded in `pyproject.toml`. `v0.3.0`
  resolves to `bc22e84`, the merge of opengwasdb PR #335; the range above the
  previous pin `0d59f151` is release tooling only, so the engine's behaviour and
  the Store format are unchanged.
