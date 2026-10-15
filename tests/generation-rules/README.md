# Manifest Generation rules

Tests for #250: Manifest Generation's own rules for a candidate's
`analyses.tsv` live in `src/manifest_generation/generation_rules.py`, not in
the Release Bundle contract (`ogstores.bundle.check()`). Store Release Build
does not need them to consume a bundle; the emitter runs them before a
candidate lands, and `pixi run bundle-check` runs them over every committed
bundle beside the contract.

Covered by `test_generation_rules.py`:
- A conforming candidate passes, and each of `assigned_ancestry`,
  `ancestry_assignment_method`, `stored_effect_scale` and `original_sd_method`
  is required.
- `info_score_threshold` is literal `NaN` or a number in `[0, 1]`, and a
  numeric threshold and its imputation score declaration go together, with a
  known `imputation_score_kind`.
- `maf_threshold` is literal `NaN` or a number in `[0, 0.5]`.
- An excluded audit row's requests are checked too: the shared schema reads
  only the rows that will be built, these rules read every row.
- An unreadable `analyses.tsv` is an error, never an exception.
- `check_bundles.py` reports a generation-rule failure and a contract failure
  in one run over a registry.

```sh
pixi run test --suite generation-rules
```
