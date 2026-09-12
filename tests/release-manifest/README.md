# Builder-manifest translation tests

Byte-equivalence and contract tests for `resources/lib/release_manifest.py`,
the one shared module that turns a release's `analyses.tsv` into the builder
manifest each OpenGWASDB layout command expects (issue #96).

Run from the repository root:

```sh
pixi run python tests/release-manifest/run_tests.py
```

The module replaced the manifest translation that was copy-pasted into
`resources/generators/opengwas-gwas-vcf-dense/build-store.py`,
`resources/generators/gwas-ssf-hybrid/build-store.py` and
`resources/generators/gwas-ssf-ragged/build-store.py`. Issue #103 deleted those
adapters, so their exact output bytes are pinned in
`adapter_manifest_sha256.json` (generated from the adapters before deletion),
and the suite keeps asserting the shared module reproduces them:

- **Dense** — for every already-built Dense Store Release, the shared module's
  23-column manifest must match what `opengwas-gwas-vcf-dense/build-store.py`
  wrote, byte for byte.
- **Hybrid** — likewise against `gwas-ssf-hybrid/build-store.py`, including its
  release-level `source_reader_capability`/`source_assembly`.
- **Ragged** — `gwas-ssf-ragged/build-store.py` handed the release's own
  `analyses.tsv` straight to `build_ragged_from_ssf`, so equivalence is asserted
  against that file's bytes.

`eqtlgen-cis-pilot` is skipped: it is built from BESD through
`eqtlgen-besd-ragged/generate.py`, not by any of the three adapters.

The suite also pins the behaviours the ticket names: `exclude_from_build` rows
are omitted, per-row reader capability/assembly survive (so a GRCh38 source is
not re-lifted, opengwasdb#85), `ancestry_prop_*` columns are discovered from the
data rather than hardcoded, and the canonical lossless representation keeps the
six Analytical Metadata columns the legacy Hybrid projection omits.

## The Hybrid loss, and its fix

`gwas-ssf-hybrid/build-store.py` wrote a 17-column manifest: it omitted
`sample_size_kind`, `sample_size_scope`, `n_cases`, `n_controls`,
`original_effect_scale` and `ancestry_assignment_method` (issue #82), so the
built Hybrid stores could not carry them. Issue #104 switched the *live* Hybrid
projection (`ADAPTER_PROJECTIONS["hybrid"]`) to the lossless canonical
representation, so a live Hybrid build now carries the same Analytical Metadata
a Dense build does.

The pre-#104 17-column adapter-compatible serialisation is retained as
`LEGACY_HYBRID_PROJECTION`, and this suite still proves it reproduces the
adapter's bytes exactly -- the historical #96 equivalence evidence for what an
already-accepted Hybrid Store Release contains. It is never selected by a live
build.

## Upstream

Native `analyses.tsv` input to the OpenGWASDB builders would let this module
shrink to a no-op; that request is filed as
<https://github.com/opengwas/opengwasdb/issues/170>.
