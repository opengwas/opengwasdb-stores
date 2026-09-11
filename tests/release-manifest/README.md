# Builder-manifest translation tests

Byte-equivalence and contract tests for `resources/lib/release_manifest.py`,
the one shared module that turns a release's `analyses.tsv` into the builder
manifest each OpenGWASDB layout command expects (issue #96).

Run from the repository root:

```sh
pixi run python tests/release-manifest/run_tests.py
```

The module replaces the manifest translation that was copy-pasted into
`resources/generators/opengwas-gwas-vcf-dense/build-store.py`,
`resources/generators/gwas-ssf-hybrid/build-store.py` and
`resources/generators/gwas-ssf-ragged/build-store.py`. Those adapters stay in
the tree (issue #103 deletes them), so this suite keeps them as the oracle:

- **Dense** — for every already-built Dense Store Release, the shared module's
  23-column manifest must be byte-identical to what
  `opengwas-gwas-vcf-dense/build-store.py` writes.
- **Hybrid** — likewise against `gwas-ssf-hybrid/build-store.py`, including its
  release-level `source_reader_capability`/`source_assembly`.
- **Ragged** — `gwas-ssf-ragged/build-store.py` hands the release's own
  `analyses.tsv` straight to `build_ragged_from_ssf`, so equivalence is asserted
  against that file's bytes.

`eqtlgen-cis-pilot` is skipped: it is built from BESD through
`eqtlgen-besd-ragged/generate.py`, not by any of the three adapters.

The suite also pins the behaviours the ticket names: `exclude_from_build` rows
are omitted, per-row reader capability/assembly survive (so a GRCh38 source is
not re-lifted, opengwasdb#85), `ancestry_prop_*` columns are discovered from the
data rather than hardcoded, and the canonical lossless representation keeps the
six Analytical Metadata columns the legacy Hybrid projection omits.

## The deferred Hybrid loss

`ADAPTER_PROJECTIONS["hybrid"]` reproduces the 17-column manifest
`gwas-ssf-hybrid/build-store.py` writes: it omits `sample_size_kind`,
`sample_size_scope`, `n_cases`, `n_controls`, `original_effect_scale` and
`ancestry_assignment_method` (issue #82), so the built Hybrid stores cannot
carry them. The suite regression-tests that omission as *legacy compatibility*,
not as accidental loss. Switching live Hybrid builds to the lossless
`canonical_manifest()` representation changes what an accepted Store contains
and is deferred to the catalogue-path/rebuild work (issue #104).

## Upstream

Native `analyses.tsv` input to the OpenGWASDB builders would let this module
shrink to a no-op; that request is filed as
<https://github.com/opengwas/opengwasdb/issues/170>.
