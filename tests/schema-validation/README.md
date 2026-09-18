# Schema validation tests

Fixture-based test for `checks.schema` validating an emitted `analyses.tsv`
against opengwasdb's own shared core schema (issue #51:
`opengwasdb.model.analyses.validate_analyses()`, published via issue
opengwasdb#16) instead of `resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R`'s
former hand-rolled, per-generator required-column list.

Run from the repository root:

```sh
pixi run Rscript tests/schema-validation/run_tests.R
```

This proves:

- a manifest that resolves cleanly emits with `checks.schema: passed`;
- a manifest with a real opengwasdb schema violation --
  `stored_effect_scale: log_or` with no `n_cases`/`n_controls` in the source
  row, the same shape of gap issue #15's `ieu-a-7` example exposed -- fails
  `--mode=emit` loudly with a message naming the violation, and
  `validation.yaml` records `checks.schema: failed` with that message rather
  than silently staying `not_run`;
- issue #51 AC3's two named example violations -- an out-of-vocabulary
  `original_sd_method` and a missing required column (`sample_size_scope`)
  -- each fail `resources/generators/lib/schema_validate.py` directly with a message
  identifying the violation, checked against hand-written fixture TSVs in
  isolation from the full generator pipeline;
- the real, already-accepted release bundles this repository tracks
  (`pqtl-interval-2018`, every `metabolome-plasma-2023` release) still
  conform to opengwasdb's shared schema (issue #51 AC4), checked directly
  against `resources/generators/lib/schema_validate.py` without mutating those bundles'
  own `validation.yaml`.
