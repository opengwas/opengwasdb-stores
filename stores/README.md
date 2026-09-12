# Store Releases

One directory per Store Release, named by its globally unique opaque
identifier (ADR 0022). The identifier is also the directory name under the
artifact root and the `release_id` written into the built Store's manifest, so
a Store found on disk joins back to its registry record without a lookup table.

```text
OGS-00042/
  release.yaml      identity, label, family, status, lineage, provenance
  build.yaml        the recipe: an opengwasdb subcommand and its flags
  analyses.tsv      membership; opengwasdb owns the schema
  validation.yaml   evidence, written back by the run
```

Nothing may resolve a release by its `label`. The label exists for display and
for the generated `by-label/` symlinks; identity is the id.

## Migration in progress

The seven Trial Store Releases have moved here. Thirteen further bundles are
still under `families/*/releases/` awaiting triage, and the built Store
artifacts have **not** yet moved to `<artifact-root>/<store-id>/` -- each
`release.yaml` records its `migration.previous_store_uri`, which is where the
artifact actually lives until then.

| | family | label |
|---|---|---|
| `OGS-00001` | eqtlgen-cis-pilot | pilot-10 |
| `OGS-00002` | eqtlgen-cis-pilot | pilot-10-completed |
| `OGS-00003` | finngen-r13 | r13-pilot-20 |
| `OGS-00004` | gwas-catalog-eur-hybrid | eur-hybrid-pilot-10 |
| `OGS-00005` | gwas-catalog-eur-hybrid | eur-hybrid-quant-pilot-10 |
| `OGS-00006` | metabolome-plasma-2023 | 2023-chen-full-european |
| `OGS-00007` | pqtl-interval-2018 | 2018-sun-pilot-10 |

`stores.tsv` and `STORES.md` are generated from these bundles by
`pixi run index` once `src/ogstores/index.py` is implemented.
