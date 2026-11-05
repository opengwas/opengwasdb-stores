# gwas-catalog-afr-hybrid

Manifest Generation inputs for the EBI GWAS Catalog `hybrid__African` pool:
the `gwas-catalog-ssf` Source Collection's African Hybrid Store Release,
OGS-00033. It runs through the one shared pipeline (ADR 0036) exactly as
OGS-00011 does, and [`../gwas-catalog-eur-hybrid/README.md`](../gwas-catalog-eur-hybrid/README.md)
describes that machinery; this file records only what is this pool's.
[`../gwas-catalog-eas-hybrid/`](../gwas-catalog-eas-hybrid/) is its East Asian
twin, acquired and frozen the same day.

```text
release-afr-hybrid-full.yaml   OGS-00033's release config (docs/spec/release-config.md)
score-declarations.tsv         derived INFO score declarations (derive_score_declarations.py, #176)
effect-placeholder-exclusions.tsv  reviewed effect-placeholder listing (#285, #441)
```

## What differs from OGS-00011

| | OGS-00011 (EUR) | OGS-00033 (AFR) |
|---|---|---|
| Selection | `store_key: hybrid__European` | `store_key: hybrid__African` |
| Dense Component axis | gnomAD v4.1 `EUR-variants.txt.gz` | gnomAD v4.1 `AFR-variants.txt.gz` |
| `selection_superpopulation` | `EUR` | `AFR` |
| Reviewed tables | score declarations, MAF-floor exemptions, effect-placeholder exclusions | score declarations and 47 effect-placeholder exclusions; no MAF-floor exemptions (the scan below found none) |
| `runtime.cores` | 64 | 48 |

Every other policy choice is OGS-00011's: the 0.005 MAF floor with the
sequencing exemption, the 0.6 INFO floor, the Store Composition Rule,
Source-AF-only effect-scale estimation, and the ancestry gates, under which an
Analysis the gates leave unassigned is excluded.

## Acquisition (2026-11-05)

The pool is 550 Analyses across 96 publications. EBI's `harmonised_list.txt`
lists 360 of them. None of the other 190 has a GRCh38 raw file (50 publish a
GRCh37 one, 140 no raw metadata sidecar), so no raw pass was run: this hg38
Store takes harmonised files only, as OGS-00011 does.

HTTPS to `ftp.ebi.ac.uk` timed out from the host that day while plain HTTP
answered, so both passes read `http://ftp.ebi.ac.uk/...` and record those URLs;
each file's sha256 is recorded as always.

```sh
B=http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics
R=/data/opengwasdb/raw/ebi-gwas-catalog
# base pass: 355 ok, 3 connection resets or truncations, 2 listed files served 404
pixi run python3 -m manifest_generation.providers.gwas_catalog.download_ebi_gwas_catalog_eur_hybrid \
  --store-key hybrid__African --candidates $R/connectors/2026-10-24/store-candidates-analyses.tsv \
  --base-url $B --harmonised-index $B/harmonised_list.txt --workers 3 \
  --manifest $R/afr-hybrid-download-manifest.tsv
# retry_transient pass over those five: 3 ok, the 2 still 404 (recorded as absent)
pixi run python3 -m manifest_generation.providers.gwas_catalog.download_ebi_gwas_catalog_eur_hybrid \
  --store-key hybrid__African --candidates $R/connectors/2026-10-24/store-candidates-analyses.tsv \
  --base-url $B --harmonised-index $B/harmonised_list.txt --workers 2 \
  --accessions-file $R/afr-hybrid-transient-failures.txt \
  --manifest $R/afr-hybrid-download-retry-transient-manifest.tsv
```

## Connectors and the frozen inventory

The connector files are kept, read-only, in
`/data/opengwasdb/raw/ebi-gwas-catalog/connectors/2026-11-05/`, beside a copy
of the candidate table (sha256 `aef4dbe2...`, the one OGS-00011's snapshots
froze against):

```sh
C=$R/connectors/2026-11-05
pixi run python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \
  --pass base=$R/afr-hybrid-download-manifest.tsv \
  --pass retry_transient=$R/afr-hybrid-download-retry-transient-manifest.tsv \
  --out $C/gwas-catalog-ssf-afr-hybrid.acquisition-manifest.tsv
pixi run python3 -m manifest_generation.providers.gwas_catalog.source_metadata_adapter \
  --candidates $C/store-candidates-analyses.tsv \
  --acquisition-manifest $C/gwas-catalog-ssf-afr-hybrid.acquisition-manifest.tsv \
  --out $C/gwas-catalog-ssf-afr-hybrid.source-metadata.tsv
```

The Source Metadata Table is this pool's own rather than the shared
`2026-10-24` one, because the adapter fills `source_genome_build` and
`genotyping_technology` only for Analyses in the Acquisition Manifest it is
given (#347), and the `2026-10-24` manifest holds only the European pool. All
358 ready Analyses record `GRCh38` and a genotyping technology.

`pixi run inventory-freeze --config release-afr-hybrid-full.yaml` froze
`resources/inventories/gwas-catalog-ssf-afr-hybrid-2026-11-05` (550 rows, 358
ready: 224 quantitative, 134 case-control), and `pixi run preflight` passed.

## Reviewed tables

`score-declarations.tsv` is `derive_score_declarations.py --inventory
resources/inventories/gwas-catalog-ssf-afr-hybrid-2026-11-05.tsv`: 6
declarations (2 `INFO`, 4 `info`). Re-derive it, never hand-edit it.

OGS-00011's other two tables come from a full read of every ready file: an
Analysis whose `effect_allele_frequency` is 0.0 on every row
(`maf_filter_exempt_analyses`, #176) and an Analysis with
`+-2.2250738585072014e-308` placeholder effects (`effect_placeholder_exclusions`,
#285).

The 2026-11-05 scan of all 358 ready files found:

- **No MAF-floor exemptions.** No Analysis has `effect_allele_frequency` 0.0 on every row.
- **47 Analyses with placeholder effects.** All are listed in `effect-placeholder-exclusions.tsv` and
  excluded as `effect_placeholder_rows`, applying OGS-00011's rule as written (owner decision, #441).
  None is OGS-00011's case. Every placeholder row has a finite standard error and p >= 0.77: a source
  beta of exactly 0 that EBI's harmonisation wrote as +-DBL_MIN. The raw upload of GCST90278092 (an EAS
  Analysis) shows it: all 1,225 of its placeholder rows that match a raw row by rsid have raw `beta` 0.0
  and the same standard error.

The scripts and their per-Analysis outputs are kept in
`/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/scan/` (`scan_listings.py`,
then `se_check.py` and `p_check.py` over the placeholder Analyses). The listing
is an emit-time membership decision and does not bind the resolution receipt.

## Generating the candidate

```sh
pixi run generate-candidate OGS-00033 \
  --config resources/generators/gwas-catalog-afr-hybrid/release-afr-hybrid-full.yaml --cores 48
```

Its work root is `/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/work/OGS-00033/`.
The resolution receipt binds the reviewed tables by absolute path, so `verify`
and `emit` must run from the checkout that resolved:
`/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/checkout`.
