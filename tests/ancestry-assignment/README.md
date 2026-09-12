# AF-based ancestry assignment tests

Fixture-based tests for `resources/generators/gwas-ssf-ragged/ancestry-assign.py`
(issues #23, #25). Run from the repository root:

```sh
pixi run python tests/ancestry-assignment/run_tests.py
```

This regenerates `fixtures/` (deterministic; see `fixtures/generate_fixtures.py`),
runs the real adapter against `fixtures/release/build.yaml`, and asserts on
the emitted release-bundle outputs — `sidecars/ancestry.tsv`, `analyses.tsv`,
and `validation.yaml` — per this repository's convention of treating emitted
metadata as the contract, not internal helper functions.

`fixtures/reference/` is a tiny synthetic ancestry-mixture reference: four
fine groups (one per super-population EUR/AFR/EAS/SAS), forty diagnostic
variants (ten per group, high frequency in their own group and low
elsewhere), same shape as the real UK Biobank panel at
`/data/opengwasdb/reference/ancestry-mixture/`. `fixtures/release/` covers a
clean per-population match, an ambiguous ~50/50 mixture (gated out on
margin), too few overlapping variants (gated out on overlap), an Analysis
with no usable source allele frequencies (left untouched at
`source_trusted_no_af`), and a source-label/AF-based disagreement.

Requires numpy/scipy/`opengwasdb.ancestry`, provided by the `dev`/`default`
Pixi environments (issue #41).

`ancestry-assign.py` and the workflow's validate phase
(`workflow/phase.py`) both mutate `validation.yaml` in place via the shared
`resources/lib/release_yaml.py::merge_validation_yaml` helper, so re-running
either stage on a real release never discards the other's checks. The fixture
generators reset `validation.yaml` to a pristine `not_run` baseline on every
regeneration for the same reason: without that reset, repeated test runs
would accumulate warnings from prior runs instead of starting fresh.
