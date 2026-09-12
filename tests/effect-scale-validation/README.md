# Reference-AF effect-scale validation tests

Fixture-based tests for the shared engine at `resources/lib/effect_scale_validation.R`
(issues #16-#21). They are plain `Rscript`/`python3` checks that exit non-zero
on failure, matching the existing `validate_emit()` smoke-check convention in
`resources/generators/gwas-ssf-ragged/generate.R`; `pixi run test` (issue #41)
runs them, along with every other suite, from the repository's locked `dev`
environment.

Run from the repository root:

```sh
pixi run Rscript tests/effect-scale-validation/run_tests.R
pixi run python tests/effect-scale-validation/test_validation_merge.py
```

`run_tests.R` regenerates `fixtures/` (deterministic; see
`fixtures/generate_fixtures.R`), runs the real generator's
`--mode=effect-scale` stage against `fixtures/config.yaml`, and asserts on the
emitted release-bundle outputs — `sidecars/sd_estimation.tsv`,
`analyses.tsv`, `release.yaml`, and `validation.yaml` — rather than on
internal helper functions, per this repo's testing convention of treating the
emitted metadata as the contract.

`fixtures/reference-af/` is a tiny synthetic Reference Resource (same
`<ancestry>/<chr>/<start>-<end>.tsv` layout as the real UKB hg38 EUR panel at
`/data/opengwasdb/reference/ukb-hg38`) covering forward, swapped, palindromic,
mismatched, non-overlapping, out-of-MAF-bounds, and multi-chromosome cases.

`test_validation_merge.py` unit-tests the shared
`resources/lib/release_yaml.py::merge_validation_yaml` logic in isolation, so
no later writer — the effect-scale generator stage, the workflow's validate
phase (`workflow/phase.py`), or the ancestry-assignment stage — discards
another's checks, reports, or warnings.
