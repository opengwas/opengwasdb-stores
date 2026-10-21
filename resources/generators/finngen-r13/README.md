# FinnGen R13 on the shared Manifest Generation pipeline

FinnGen is a Source Collection (ADR 0036) fed to the one pipeline in
`src/manifest_generation/` through connector files. Its four connectors are:

| Connector | Where |
|---|---|
| Acquisition | `src/manifest_generation/providers/finngen/acquire.py` |
| Metadata adapter | `src/manifest_generation/providers/finngen/source_metadata_adapter.py` |
| Column Map | none: `opengwasdb.finngen-r13` reads the provider's own tabular files |
| Release config | `release-r13-full.yaml` in this directory |

Both connectors write the shared formats specified in
`docs/spec/source-connectors.md`, from the pinned public endpoint manifest
`finngen_R13_manifest.tsv` (2,754 endpoints: three inverse-rank-normalised
quantitative endpoints and every case-control endpoint). The resulting
connector files and the frozen Source Inventory live with the data they
describe, in `/data/opengwasdb/raw/finngen-r13/connectors/2026-10-21/` and
`resources/inventories/`.

The Source Metadata Table also carries the Store's Analysis identity, in the
two optional columns `analysis_id` (`finngen-r13-<phenocode>`: a bare phenocode
is not unique across sources) and `analysis_group_id` (the provider manifest's
`category`). Nothing the resolver reads moves with them: the frozen inventory,
the resolver manifest and the Resolution Records stay keyed by
`source_analysis_id`, and `analyses.tsv` and its sidecars report the declared
identity, which is what OGS-00017's committed bundle already uses (#288).

## Running it

```sh
# 1. The Source Metadata Table: what the collection intends (2,754 endpoints).
pixi run -e dev python3 -m manifest_generation.providers.finngen.source_metadata_adapter \
  --manifest /data/opengwasdb/raw/finngen-r13/releases/r13-full/manifest/finngen_R13_manifest.tsv \
  --out /data/opengwasdb/raw/finngen-r13/connectors/2026-10-21/finngen-r13.source-metadata.tsv

# 2. The Acquisition Manifest: what acquisition produced. Resumable; existing
#    files are checksummed and kept. `acquire-finngen-r13-full.sh` wraps this
#    with a retry loop for unattended use.
pixi run -e dev python3 -m manifest_generation.providers.finngen.acquire \
  --manifest /data/opengwasdb/raw/finngen-r13/releases/r13-full/manifest/finngen_R13_manifest.tsv \
  --source-dir /data/opengwasdb/raw/finngen-r13/releases/r13-full/source \
  --out /data/opengwasdb/raw/finngen-r13/connectors/2026-10-21/finngen-r13-full.acquisition-manifest.tsv \
  --workers 8

# 3. Freeze, preflight, resolve, render, emit -- one command, any release config.
pixi run -e dev generate-candidate OGS-00017 \
  --config resources/generators/finngen-r13/release-r13-full.yaml --cores 32

# 4. Check the candidate's bundle contract.
pixi run bundle-check
pixi run index
```

`OGS-00017` is accepted, and the emitter refuses to replace an accepted bundle.
A regeneration is a **comparison** run: emit into a temporary registry
(`--registry-root`) and diff against `stores/OGS-00017`, exactly as #287 did
for OGS-00011. Do not emit over `stores/OGS-00017`.

### Resolution gap (blocking, #207 / #281 in opengwasdb)

The pinned `opengwasdb` (`4e92256`) cannot resolve this collection yet. Its
one-pass resolver reads only a reader that streams source metrics
(`MetricsReader.stream_metric_chunks`), and `FinnGenR13Reader` offers
`stream_associations`/`stream_variants`/`extract_at_sites` only, so every
endpoint comes back as a controlled failure:

```text
TypeError: FinnGenR13Reader cannot stream source metrics; the one-pass
resolver reads tabular sources only (issue #207)
```

ADR 0036 is explicit that a Source Collection whose format OpenGWASDB cannot
yet resolve waits rather than entering a bundle on declared values, so the
pipeline's `render` stage excludes all 2,754 endpoints as `resolution_failed`
and the emitted candidate has no members. The connectors, the freeze and the
preflight are unaffected and were verified against the committed bundle; only
Analysis Resolution is missing. The routes forward are OpenGWASDB's: give
`FinnGenR13Reader` the `MetricsReader` contract, or land the Column Map reader
of opengwasdb#281 (whose `ColumnMapReader` already streams metrics and whose
built-in `finngen-r13` map reproduces this reader's rules) together with the
Release Bundle's Column Map (#292).

### The path to acceptance criterion 1

This is the standing route, recorded so nobody re-derives it:

1. the `opengwasdb` pin moves past dev's head (after #222), so the resolver
   in the environment can read source metrics through a Column Map;
2. #292 part 2 lands the Release Bundle's Column Map declaration;
3. this release's rows then declare `source_reader_capability =
   opengwasdb.column-map` with the built-in `finngen-r13` map, in
   `build.options` and the resolver manifest's per-Analysis capability;
4. re-run the OGS-00017 comparison, and reconcile membership, ancestry and
   every `analyses.tsv` column;
5. only then delete the R generator, which is criterion 1 itself.

None of that is built here: steps 1-3 are other tickets' work, and the R
generator stays the only working FinnGen path until step 4 passes.

### What the comparison run establishes

The comparison run in `/data/opengwasdb/work/288-MVXoxt` (and, after the
identity work, `/data/opengwasdb/work/288-r2-*`) reconciles the collection
completely; only Analysis Resolution is missing.

- **Files.** 2,754 Analyses and 2,754 files in the release's `source/`
  directory: 0 unmatched by name, 0 size mismatches and 0 checksum mismatches.
  The 19 endpoints acquisition reused from the earlier `raw/finngen-r13-10/`
  capture are symlinks, and three endpoints were re-hashed by hand, one of them
  through a symlink; the frozen Source Inventory's `sha256` equals the committed
  bundle's `checksum` on all 2,754 rows.
- **Membership.** 2,751 case-control and 3 quantitative endpoints, all ready.
  The emitted candidate has 2,754 rows and excludes every one of them as
  `resolution_failed`, so its membership is not a FinnGen decision.
- **`analyses.tsv`.** 30 of the 35 columns it shares with the committed bundle
  are byte-identical, including `analysis_id` and `analysis_group_id` on all
  2,754 rows, `checksum`, `size_bytes`, `n_cases`, `n_controls`,
  `sample_size`, both method tiers, `source_genome_build`, the licence, the
  consortium, `source_reader_capability` and the Finnish Source Ancestry Label.
  The differences: `source_file` (2,754) because the committed bundle names the
  retired `/data/opengwasdb/finngen-r13/` tree; `source_label` and
  `analysis_label` (1, `ALCODELIRIUM`) because the adapter trims the manifest's
  trailing non-breaking space as the GWAS Catalog adapter does; and
  `inclusion_reason`/`exclude_from_build` (2,754) because of the gap above. The
  pipeline's schema adds `analysis_index`, `downloaded_file`, the four
  `imputation_score_*` columns and `maf_threshold`, and drops the committed
  `source_category`, whose content `analysis_group_id` now carries.
- **Ancestry.** Unmeasured: every record is a controlled failure, so no row
  carries an Assigned Ancestry and the measured-EUR difference the ticket
  expects is not observable yet. The committed bundle is `unassigned` and empty
  here too.

Every endpoint is resolved, without exception: the pipeline reads no declared
ancestry or effect scale. The quantitative IRN endpoints' method tier declares
`declared_standardised` (they are standardised over the whole cohort by
construction); every other endpoint is `log_or`/`binary_trait` with its counts.

## A later round in a new format

**A later FinnGen round in a new format needs only a new Column Map and a
release config.** If a round publishes GWAS-SSF, a bespoke tabular layout, or
anything else opengwasdb's generic tabular reader can describe, add

1. that round's **Column Map** (the per-Source-Collection declaration of which
   provider column supplies each field, and the collection's conventions), and
2. a **release config** naming that round's connector files, Selection, method
   tiers and Reference Resources,

and nothing else: the acquisition writer, both connector files, the freeze,
preflight, resolution, the Quality Checks, rendering and the emitter are the
pipeline's. A later round that keeps this provider's format may not even need
a new Column Map: a new endpoint manifest, a new Selection, and a re-freeze
give a new Store Release.

## History: the R-era configs

`config-full.yaml`, `config-pilot-20.yaml` and `generation-r13-pilot-20.yaml`
are kept unchanged because the committed receipts of OGS-00017 and OGS-00003
cite them; `config-full.yaml`'s `selection: {mode: full}` and `output` block
are the retired grammar and are refused by the release config loader
(`docs/spec/release-config.md`). The R generator they configure
(`resources/generators/lib/source-formats/finngen-r13-dense/generate.R`, with
`lib/finngen_r13_pilot.R` and `lib/metadata_resolvers/finngen_manifest.R`) and
its R suites are **kept**: #288 first deleted them, then restored them, because
it is still the only path that resolves FinnGen. Its deletion lands with
acceptance criterion 1 (see "The path to acceptance criterion 1"), not before.

`assess.py` remains: it turns OGS-00003's pilot build evidence
(`sidecars/build_report.tsv`, `sidecars/sd_estimation.tsv`, `validation.yaml`)
into the scale-up recommendation the full release rests on. It reads a built
pilot's Release Artifacts, not a manifest.
