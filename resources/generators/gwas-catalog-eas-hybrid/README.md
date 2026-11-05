# gwas-catalog-eas-hybrid

Manifest Generation inputs for the EBI GWAS Catalog `hybrid__East-Asian` pool:
the `gwas-catalog-ssf` Source Collection's East Asian Hybrid Store Release,
OGS-00032. It runs through the one shared pipeline (ADR 0036) exactly as
OGS-00011 does, and [`../gwas-catalog-eur-hybrid/README.md`](../gwas-catalog-eur-hybrid/README.md)
describes that machinery; this file records only what is this pool's.
[`../gwas-catalog-afr-hybrid/`](../gwas-catalog-afr-hybrid/) is its African
twin, acquired and frozen the same day.

```text
release-eas-hybrid-full.yaml   OGS-00032's release config (docs/spec/release-config.md)
score-declarations.tsv         derived INFO score declarations (derive_score_declarations.py, #176)
effect-placeholder-exclusions.tsv  reviewed effect-placeholder listing (#285, #441)
```

## What differs from OGS-00011

| | OGS-00011 (EUR) | OGS-00032 (EAS) |
|---|---|---|
| Selection | `store_key: hybrid__European` | `store_key: hybrid__East-Asian` |
| Dense Component axis | gnomAD v4.1 `EUR-variants.txt.gz` | gnomAD v4.1 `EAS-variants.txt.gz` |
| `selection_superpopulation` | `EUR` | `EAS` |
| Reviewed tables | score declarations, MAF-floor exemptions, effect-placeholder exclusions | score declarations and 107 effect-placeholder exclusions; no MAF-floor exemptions (the scan below found none) |
| `runtime.cores` | 64 | 48 |

Every other policy choice is OGS-00011's: the 0.005 MAF floor with the
sequencing exemption, the 0.6 INFO floor, the Store Composition Rule,
Source-AF-only effect-scale estimation, and the ancestry gates, under which an
Analysis the gates leave unassigned is excluded.

## Acquisition (2026-11-05)

The pool is 852 Analyses across 139 publications. EBI's `harmonised_list.txt`
lists 709 of them. None of the other 143 has a GRCh38 raw file (20 publish a
GRCh37 one, 123 no raw metadata sidecar), so no raw pass was run: this hg38
Store takes harmonised files only, as OGS-00011 does.

HTTPS to `ftp.ebi.ac.uk` timed out from the host that day while plain HTTP
answered, so both passes read `http://ftp.ebi.ac.uk/...` and record those URLs;
each file's sha256 is recorded as always.

```sh
B=http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics
R=/data/opengwasdb/raw/ebi-gwas-catalog
# base pass: 706 ok, 3 connection resets
pixi run python3 -m manifest_generation.providers.gwas_catalog.download_ebi_gwas_catalog_eur_hybrid \
  --store-key hybrid__East-Asian --candidates $R/connectors/2026-10-24/store-candidates-analyses.tsv \
  --base-url $B --harmonised-index $B/harmonised_list.txt --workers 3 \
  --manifest $R/eas-hybrid-download-manifest.tsv
# retry_transient pass over the three: 3 ok
pixi run python3 -m manifest_generation.providers.gwas_catalog.download_ebi_gwas_catalog_eur_hybrid \
  --store-key hybrid__East-Asian --candidates $R/connectors/2026-10-24/store-candidates-analyses.tsv \
  --base-url $B --harmonised-index $B/harmonised_list.txt --workers 2 \
  --accessions-file $R/eas-hybrid-transient-failures.txt \
  --manifest $R/eas-hybrid-download-retry-transient-manifest.tsv
```

## Connectors and the frozen inventory

The connector files are kept, read-only, in
`/data/opengwasdb/raw/ebi-gwas-catalog/connectors/2026-11-05/`, beside a copy
of the candidate table (sha256 `aef4dbe2...`, the one OGS-00011's snapshots
froze against):

```sh
C=$R/connectors/2026-11-05
pixi run python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \
  --pass base=$R/eas-hybrid-download-manifest.tsv \
  --pass retry_transient=$R/eas-hybrid-download-retry-transient-manifest.tsv \
  --out $C/gwas-catalog-ssf-eas-hybrid.acquisition-manifest.tsv
pixi run python3 -m manifest_generation.providers.gwas_catalog.source_metadata_adapter \
  --candidates $C/store-candidates-analyses.tsv \
  --acquisition-manifest $C/gwas-catalog-ssf-eas-hybrid.acquisition-manifest.tsv \
  --out $C/gwas-catalog-ssf-eas-hybrid.source-metadata.tsv
```

The Source Metadata Table is this pool's own rather than the shared
`2026-10-24` one, because the adapter fills `source_genome_build` and
`genotyping_technology` only for Analyses in the Acquisition Manifest it is
given (#347), and the `2026-10-24` manifest holds only the European pool. All
709 ready Analyses record `GRCh38`; 4 record no genotyping technology.

`pixi run inventory-freeze --config release-eas-hybrid-full.yaml` froze
`resources/inventories/gwas-catalog-ssf-eas-hybrid-2026-11-05` (852 rows, 709
ready: 502 quantitative, 207 case-control), and `pixi run preflight` passed.

## Reviewed tables

`score-declarations.tsv` is `derive_score_declarations.py --inventory
resources/inventories/gwas-catalog-ssf-eas-hybrid-2026-11-05.tsv`: 128
declarations (97 `INFO`, 31 `info`). Re-derive it, never hand-edit it.

OGS-00011's other two tables come from a full read of every ready file: an
Analysis whose `effect_allele_frequency` is 0.0 on every row
(`maf_filter_exempt_analyses`, #176) and an Analysis with
`+-2.2250738585072014e-308` placeholder effects (`effect_placeholder_exclusions`,
#285).

The 2026-11-05 scan of all 709 ready files found:

- **No MAF-floor exemptions.** No Analysis has `effect_allele_frequency` 0.0 on every row. GCST90432173
  comes closest (8,278,623 of 8,279,013 rows) and is excluded below.
- **107 Analyses with placeholder effects.** All are listed in `effect-placeholder-exclusions.tsv` and
  excluded as `effect_placeholder_rows`, applying OGS-00011's rule as written (owner decision, #441).
  The rows are of two kinds, and each reason says which:
  - **99 Analyses: zero effects.** Every placeholder row has a finite standard error and p >= 0.77.
    These are source betas of exactly 0 that EBI's harmonisation wrote as +-DBL_MIN, not OGS-00011's
    case. The raw upload of GCST90278092 shows it: all 1,225 of its placeholder rows that match a raw
    row by rsid have raw `beta` 0.0 and the same standard error.
  - **8 Analyses: OGS-00011's case or worse.** Their rows have standard error 0, NA or inf, a
    placeholder odds ratio, or p-values down to 0.036. They are GCST90104038, GCST90104897,
    GCST90165265, GCST90432173, GCST90454404, GCST90454406, GCST90454417 and GCST90503328.

The scripts and their per-Analysis outputs are kept in
`/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/scan/` (`scan_listings.py`,
then `se_check.py` and `p_check.py` over the placeholder Analyses). The listing
is an emit-time membership decision and does not bind the resolution receipt.

## Generating the candidate

```sh
pixi run generate-candidate OGS-00032 \
  --config resources/generators/gwas-catalog-eas-hybrid/release-eas-hybrid-full.yaml --cores 48
```

Its work root is `/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/work/OGS-00032/`.
The resolution receipt binds the reviewed tables by absolute path, so `verify`
and `emit` must run from the checkout that resolved:
`/data/opengwasdb/work/gwas-catalog-eas-afr-hybrid/checkout`.
