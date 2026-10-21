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

This proves (its checks that a clean manifest emits with `checks.schema:
passed` and that a `log_or` row without counts fails the emit ran the R
`gwas-ssf-ragged` generator, retired with #300; the shared pipeline runs the
same schema in `candidate_workflow.validate_candidate_analyses`):

- issue #51 AC3's two named example violations -- an out-of-vocabulary
  `original_sd_method` and a missing required column (`sample_size_scope`)
  -- each fail `src/manifest_generation/schema_validate.py` directly with a message
  identifying the violation, checked against hand-written fixture TSVs in
  isolation from the full generator pipeline;
- the real, already-accepted release bundles this repository tracks
  (`pqtl-interval-2018`, every `metabolome-plasma-2023` release) still
  conform to opengwasdb's shared schema (issue #51 AC4), checked directly
  against `src/manifest_generation/schema_validate.py` without mutating those bundles'
  own `validation.yaml`.
