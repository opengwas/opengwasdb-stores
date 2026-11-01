- **The `opengwasdb` pin moves to `v0.3.2` (issue #401, epic #371).** `v0.3.2`
  resolves to `44f25a9`, the merge of opengwasdb PR #339 (opengwasdb#337). The
  Observed-View Check now reads Z/SE missingness through each plane's missing
  marker (NaN) rather than `isfinite`, so a cell with a finite Z and an SE of
  `+inf`, held identically before and after completion, passes and is counted
  as `infinite_cells`; `validate` warns about such cells per Analysis. OGS-00011's
  completion check had failed on eight of them. The range above `v0.3.0` is
  that fix and `v0.3.1` (CI and docs only): builders and the Store format are
  unchanged.
