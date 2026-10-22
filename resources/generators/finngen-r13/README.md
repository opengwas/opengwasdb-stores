# FinnGen R13 on the shared Manifest Generation pipeline

FinnGen is a Source Collection (ADR 0036) fed to the one pipeline in
`src/manifest_generation/` through connector files. Its four connectors are:

| Connector | Where |
|---|---|
| Acquisition | `src/manifest_generation/providers/finngen/acquire.py` |
| Metadata adapter | `src/manifest_generation/providers/finngen/source_metadata_adapter.py` |
| Column Map | `finngen-r13`, built into OpengwasDB: `column_map.builtin` in the release config, no file of its own |
| Release config | `release-r13-full.yaml` in this directory |

Both writers write the shared formats specified in
`docs/spec/source-connectors.md`, from the pinned public endpoint manifest
`finngen_R13_manifest.tsv` (2,754 endpoints: three inverse-rank-normalised
quantitative endpoints and every case-control endpoint). The connector files
and the frozen Source Inventory live with the data they describe, under
`/data/opengwasdb/raw/finngen-r13/connectors/` and `resources/inventories/`:
the acquisition pass of `2026-10-21`, and the Source Metadata Table re-derived
from it on `2026-10-22` with the Store-identity columns. **A pass directory is
never rewritten.** A changed connector is a new pass directory and a re-freeze
under a new snapshot id, which is why the two connector files here come from
different passes: the acquisition evidence did not change, the adapter's output
did.

The Source Metadata Table also carries the Store's Analysis identity, in the
two optional columns `analysis_id` (`finngen-r13-<phenocode>`: a bare phenocode
is not unique across sources) and `analysis_group_id` (the provider manifest's
`category`). Nothing the resolver reads moves with them: the frozen inventory,
the resolver manifest and the Resolution Records stay keyed by
`source_analysis_id`, and `analyses.tsv` and its sidecars report the declared
identity, which is what OGS-00017's committed bundle already uses (#288).

## Reading the release through the Column Map

Every Analysis reads through OpengwasDB's generic tabular reader,
`opengwasdb.column-map`, and the built-in `finngen-r13` map: the release config
declares that capability in `build.options`, and binds the map by name and the
pinned package's checksum in `column_map.builtin`, so the bundle carries no map
file — the bytes are the pinned `opengwasdb` package's own. Manifest Generation
passes the resolver `--column-map finngen-r13`, and every Resolution Record's
`resolution_config.column_map` records `{name, sha256}`; Store Release Build
passes the same map to `build-dense-vcf` (ADR 0063, #292).

The reason is that only this reader streams source metrics, which is what the
one-pass resolver reads. The dedicated `opengwasdb.finngen-r13` reader offers
`stream_associations`/`stream_variants`/`extract_at_sites` only, so every
endpoint came back as a controlled failure (opengwasdb#207):

```text
TypeError: FinnGenR13Reader cannot stream source metrics; the one-pass
resolver reads tabular sources only (issue #207)
```

That is what #288's stage-1 run recorded — all 2,754 Analyses excluded as
`resolution_failed` — and it is why the built-in map, which reproduces that
reader's rules for this format, replaced the dedicated reader here. Both the
pin's built-in map and the resolver's `--column-map` landed before this
release config did (#292), and the probe below proves the path end to end.

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

# 3. The 10-Analysis probe: the same recipe on a bounded Selection.
pixi run -e dev generate-candidate OGS-99288 \
  --config resources/generators/finngen-r13/release-r13-probe-10.yaml --cores 10

# 4. Freeze, preflight, resolve, render, emit -- one command, any release config.
pixi run -e dev generate-candidate OGS-00017 \
  --config resources/generators/finngen-r13/release-r13-full.yaml --cores 32

# 5. Check the candidate's bundle contract.
pixi run bundle-check
pixi run index
```

`OGS-00017` is accepted, and the emitter refuses to replace an accepted bundle.
A regeneration is a **comparison** run: emit into a temporary registry
(`--registry-root`) and diff against `stores/OGS-00017`, exactly as #287 did
for OGS-00011. Do not emit over `stores/OGS-00017`. The full run resolves
2,754 endpoints — 2.16 TB of source files — so it is scheduled around the
host's backup window.

## The 10-Analysis probe (#288)

`release-r13-probe-10.yaml` carries the release's connectors, defaults, method
tiers, Reference Resources and Build Recipe, with ten Analyses: seven
case-control endpoints spanning 132 to 203,299 cases, and all three
inverse-rank-normalised quantitative endpoints. Run at the pin the release
names (`opengwasdb 0.3.0`, `d405de0`), all ten resolved:

| Analysis | Design | Assigned ancestry | Measured EUR share | Finland (fine group) |
|---|---|---|---:|---:|
| `AB1_ACTINOMYCOSIS` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8411 |
| `ALCODELIRIUM` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8406 |
| `CD2_INSITU_BREAST` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8435 |
| `K11_IBD_STRICT` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8406 |
| `M13_ARTHROPATHIES` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8406 |
| `RX_STATIN` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8406 |
| `T2D` | case-control | EUR (`af_assigned`) | 0.9814 | 0.8409 |
| `BMI_IRN` | quantitative | EUR (`af_assigned`) | 0.9818 | 0.8360 |
| `HEIGHT_IRN` | quantitative | EUR (`af_assigned`) | 0.9818 | 0.8362 |
| `WEIGHT_IRN` | quantitative | EUR (`af_assigned`) | 0.9818 | 0.8367 |

Each record's fit is comfortable: a 50,000-site overlap, an NNLS residual of
0.041 against the declared `residual_max` of 0.06, a runner-up margin of 0.967
against `delta` 0.20, and an `eaf_orientation` of `passed` (r ≈ 0.967). The
Finnish fine group takes 0.84 of the mixture, so the measured ancestry is not
merely "European" — it is the expected Finnish sub-population.

**SD is not estimated, by declaration.** The quantitative tier declares
`declared_standardised` (the IRN endpoints are standardised over the whole
cohort by construction), so the resolver records `phenotype_sd.status =
skipped`, reason `scale_already_declared`; every case-control endpoint records
`skipped`/`non_quantitative_effect_scale`. No row carries an `original_sd`, and
`sidecars/sd_estimation.tsv` says the same for all ten.

**The differences from `stores/OGS-00017`.** Of the 35 columns the probe's
`analyses.tsv` shares with the accepted bundle, 28 are byte-identical on all
ten rows. The seven that differ, every one explained:

- `assigned_ancestry` and `ancestry_assignment_method` (10/10): the accepted
  bundle is `unassigned`, because nothing resolved when it was made; the probe
  measures EUR. This is the difference the ticket expects.
- `source_file` (10/10): the accepted bundle names the retired
  `/data/opengwasdb/finngen-r13/` tree; the frozen inventory names the
  read-only mirror under `/data/opengwasdb/raw/finngen-r13/`.
- `source_reader_capability` (10/10): `opengwasdb.finngen-r13` became
  `opengwasdb.column-map`.
- `inclusion_reason` (10/10): the accepted bundle's collection-wide phrase
  ("all endpoints in the frozen FinnGen R13 public summary-statistics
  manifest") against the pipeline's per-row reason, `selected_ready_source`.
  `exclude_from_build` is blank on both sides: the probe includes all ten.
- `source_label` and `analysis_label` (1/10, `ALCODELIRIUM`): the adapter trims
  the manifest's trailing non-breaking space, as the GWAS Catalog adapter
  trims padding; the R-era bundle kept it.

The probe adds `analysis_index`, `downloaded_file`, the four
`imputation_score_*` columns, `info_score_threshold` and `maf_threshold`, and
has no `source_category` (its content is `analysis_group_id` now). The
committed bundle also carries completion columns (`completed_against`,
`completion_*`) and `n_hits_*`, which a candidate does not: they are build
evidence.

## What the full comparison run establishes

The comparison runs in `/data/opengwasdb/work/288-MVXoxt` (before the identity
work) and `/data/opengwasdb/work/288-r3-*` (the frozen `2026-10-22` snapshot)
reconcile the collection's files completely; with the Column Map above, the
Analyses resolve too, as the probe shows for ten of them.

- **Files.** 2,754 Analyses and 2,754 files in the release's `source/`
  directory: 0 unmatched by name, 0 size mismatches and 0 checksum mismatches.
  The 19 endpoints acquisition reused from the earlier `raw/finngen-r13-10/`
  capture are symlinks, and three endpoints were re-hashed by hand, one of them
  through a symlink; the frozen Source Inventory's `sha256` equals the committed
  bundle's `checksum` on all 2,754 rows.
- **Membership.** 2,751 case-control and 3 quantitative endpoints, all ready.
  Stage 1's candidate excluded all 2,754 as `resolution_failed`; the probe's
  candidate includes all ten of its Analyses, so the full run's membership is
  expected to be all 2,754.
- **`analyses.tsv`.** With the reader still un-resolvable, 30 of the 35
  columns the candidate shares with the committed bundle were byte-identical
  over the whole table, including `analysis_id` and `analysis_group_id` on all
  2,754 rows, `checksum`, `size_bytes`, `n_cases`, `n_controls`, `sample_size`,
  both method tiers, `source_genome_build`, the licence and the consortium.
  The differences the probe now shows at full scale join them: `source_file`
  (2,754), the reader capability (2,754), the now-measured ancestry (2,754),
  `inclusion_reason` (2,754) and `ALCODELIRIUM`'s trimmed label (1).
- **Ancestry.** Measured, not declared: every probe row carries an Assigned
  Ancestry of EUR from the source allele frequencies, so the committed bundle's
  `unassigned` is the one thing the full run is expected to change on every
  row.

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
(`docs/spec/release-config.md`).

The R generator those configs drove — `resources/generators/finngen-r13/
generate.R` with `lib/source-formats/finngen-r13-dense/generate.R`,
`lib/finngen_r13_pilot.R` and `lib/metadata_resolvers/finngen_manifest.R` — and
its R suites are **deleted**. It was the collection's only working path until
the Column Map resolved it; #288's acceptance criterion 1 is that deletion, so
those receipts now name history rather than a runnable generator. What remains
of that era is one behaviour the pipeline still needs: `acquire.py`'s
`--release-dir` compatibility mode, which fills a working bundle's checksums
and download evidence, and `tests/finngen-r13-pilot/test_acquire.py` still
holds it to the recorded digests.

`assess.py` remains: it turns OGS-00003's pilot build evidence
(`sidecars/build_report.tsv`, `sidecars/sd_estimation.tsv`, `validation.yaml`)
into the scale-up recommendation the full release rests on. It reads a built
pilot's Release Artifacts, not a manifest.
