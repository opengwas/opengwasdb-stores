# No-cis sparse-region policy tests

Fixture-based test for the `gwas-ssf-ragged` generator's no-cis sparse-region
policy (issue #26): Store Families with no single encoding gene per Analysis
(e.g. small-molecule metabolomics) omit `inputs.analysis_targets` and retain
only significant/suggestive regions, never a fabricated cis window.

Run from the repository root:

```sh
pixi run Rscript tests/no-cis-region-policy/run_tests.R
```

This regenerates a tiny synthetic "full" GWAS-SSF source file (5 clustered
significant hits, 3 well-separated suggestive hits, 20 null variants) served
over a local `file://` URL — no network access needed — and runs the real
generator (`emit` -> `validate` -> `filter`) against it, asserting on the
emitted release-bundle outputs: `analyses.tsv` has no single-gene-target
columns, `release.yaml` has no `sidecars.analysis_targets` pointer, and
`sidecars/sparse_regions.tsv` has zero `cis` rows and only the expected
significant/suggestive regions. It then builds the release with the
production `opengwasdb build-ragged-ssf` CLI — the command the shared workflow
shells out to (issue #103) — and asserts the trans-only Store validates, since
the retired `build-store.py` adapter's read-back smoke test used to require a
non-empty cis region and failed this family.

The existing pqtl-interval-2018 family (which does declare
`inputs.analysis_targets`) is unaffected by this change — its
`--mode=validate` output and committed `analyses.tsv` are unchanged; this
policy is purely additive configuration for families with no gene target.
