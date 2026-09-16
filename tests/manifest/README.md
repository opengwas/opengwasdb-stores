# Build manifest test suite

Test suite for `ogstores.manifest` and the derived build-manifest seam in
`ogstores.plan`, governed by [ADR 0025](../../docs/adr/0025-registry-filters-excluded-analyses.md)
and amending [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md).

## The regression this suite exists for

`analyses.tsv` carries a documented `exclude_from_build` column, but nothing
honoured it: `plan()` handed the bundle's table to the builder verbatim, and
`opengwasdb` strips the column as REGISTRY_ONLY without acting on it. An
excluded row was therefore built anyway. `OGS-00004` row 0 (`GCST003566`) is the
worked case: its EAF is reported against the other allele, so the build aborted
45 minutes in with `EafOrientationError` (r = -0.9954). The user-visible symptom
is that an excluded `analysis_id` reached the builder's manifest.

`test_excluded_analysis_id_never_reaches_builder_manifest` asserts that symptom,
not an internal detail. Before the fix it fails with:

```text
AssertionError: PosixPath('.../stores/OGS-00077/analyses.tsv') == PosixPath('.../stores/OGS-00077/analyses.tsv')
: build step consumes the bundle's audit analyses.tsv; excluded rows reach the builder
```

## Contracts and invariants covered

1. **The seam**: `plan()` resolves the `analyses` token — positional for
   Dense/Hybrid/Ragged-SSF and the `--analyses` flag for Ragged BESD — and the
   step's declared inputs to `<artifact-root>/<store_id>/work/analyses.tsv`. The
   bundle's audit `analyses.tsv` never appears in a build argv.
2. **Completion takes no manifest**: `complete-*` commands consume only a parent
   Store, so no manifest step applies.
3. **Filtering**: rows with `exclude_from_build` `true` (case-insensitive,
   whitespace-trimmed) are dropped; every other column and the surviving row
   order are preserved; `analysis_index` is re-densified `0..n-1` when it exists,
   and is not invented when it does not.
4. **Audit evidence**: the sidecar JSON names every dropped `analysis_id`, its
   `row_index`, and its `inclusion_reason`; the bundle keeps the audit row.
5. **No leakage**: an exclusion decision is never written into another column.
6. **Pass-through**: a manifest with no excluded rows is written byte-for-byte
   unchanged; a manifest without the column has nothing to exclude.
7. **Loud failure**: a malformed `exclude_from_build` value names the offending
   row and value; an all-excluded manifest, a header-only manifest, and a
   missing `analysis_id` column raise rather than writing a manifest.
8. **Atomicity**: writes leave no temp files behind.

## Running the suite

```sh
pixi run python tests/manifest/test_manifest.py
# or through the repo orchestrator
pixi run test-python
```
