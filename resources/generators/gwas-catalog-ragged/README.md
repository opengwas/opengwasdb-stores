# GWAS Catalog region-filtered ragged releases

Release configs for GWAS Catalog collections that are too large to keep whole
and are filtered at acquisition to a region policy (#300): Chen et al. 2023's
plasma metabolome atlas and Sun et al. 2018's INTERVAL SomaScan pQTLs. They
replace the R `gwas-ssf-ragged` generator and its `metabolome-plasma-2023` and
`pqtl-interval-2018` configs; OGS-00006 and OGS-00007, which those built, keep
their recorded commands as history.

| Release config | Store Release built as | Region policy |
|---|---|---|
| `release-chen-2023-european.yaml` | OGS-00006 (1,400 Analyses) | `signals_only`: significant-trans flanks and suggestive leads |
| `release-sun-2018-pilot-10.yaml` | OGS-00007 (10 Analyses) | `cis_and_signals`: also a 1 Mb flank around each target gene |

Both keep the QC panel (`qc-panel-hg38`), so Analysis Resolution can measure
ancestry and phenotype SD from the filtered rows.

## Connectors

`connectors/` holds the connector files this checkout writes for these
releases; the GWAS Catalog's whole Source Metadata Table is read-only on
`/data/opengwasdb/raw/ebi-gwas-catalog/connectors/2026-10-20/`.

| File | Written by |
|---|---|
| `sun-2018.source-metadata.tsv` | `python3 -m manifest_generation.providers.gwas_catalog.somascan_targets --source-metadata-table /data/opengwasdb/raw/ebi-gwas-catalog/connectors/2026-10-20/gwas-catalog-ssf.source-metadata.tsv --analysis-targets resources/reference-resources/somascan/sun-2018-analysis-targets.tsv --somascan-targets resources/reference-resources/somascan/somascan-targets.tsv --out ...`: the 3,283 Sun Analyses with their target annotation |
| `chen-2023-european.acquisition-manifest.tsv` | `python3 -m manifest_generation.providers.gwas_catalog.legacy_ragged_acquisition OGS-00006 --out ...` |
| `sun-2018-pilot-10.acquisition-manifest.tsv`, with its `.sparse-regions.tsv`, `.filter-summary.tsv` and `.meta.yaml` | `python3 -m manifest_generation.region_filtered_acquisition`, below |
| `sun-2018-pilot-10.originals.acquisition-manifest.tsv` | Sun's 10 originals in `/data/opengwasdb/raw/ebi-sun-pqtl-10/`, which it read and kept |

Chen's Acquisition Manifest lists the files the R generator filtered, as they
are, with lineage from OGS-00006's `filter_summary.tsv`: the originals were
deleted after filtering, so they cannot be acquired afresh. 198 of the files
were re-materialised on 15 Sep 2026; `ogs-00006-rematerialised-2026-09-15.tsv`
is the reviewed list of them, each by its recorded and on-disk sha256, and any
other difference is refused. Reviewed by counting: each file's rows, less the
rows sharing a recorded suggestive lead's position and id beyond the first,
are exactly R's `retained_rows` (225 sibling rows over the 198); the 16 whose
originals survive in `/data/opengwasdb/raw/ebi-chen-10/` were also re-filtered
row for row. Its run record (`.meta.yaml`) checksums the list and OGS-00006's
`sparse_regions.tsv` and `filter_summary.tsv`, which a regenerated candidate
carries unchanged. Sun's files were acquired to its policy by the shared region filter, which
keeps the QC panel R never kept for Sun. The originals in
`/data/opengwasdb/raw/ebi-sun-pqtl-10/` are OGS-00007's, so the filter only
read them, in place, and kept them (`--keep-originals`):
`sun-2018-pilot-10.originals.acquisition-manifest.tsv` records each one there,
with its size and sha256, and the lineage names it.

```sh
pixi run python3 -m manifest_generation.region_filtered_acquisition \
  --config resources/generators/gwas-catalog-ragged/release-sun-2018-pilot-10.yaml \
  --originals resources/generators/gwas-catalog-ragged/connectors/sun-2018-pilot-10.originals.acquisition-manifest.tsv \
  --out-dir /data/opengwasdb/raw/ebi-sun-pqtl-10/filtered-qc-2026-10-21 \
  --out resources/generators/gwas-catalog-ragged/connectors/sun-2018-pilot-10.acquisition-manifest.tsv \
  --workers 4 --keep-originals
```

The directory was then made read-only. Each file keeps exactly the rows R's
filter kept for OGS-00007, plus the QC-panel rows, and its cis, trans and lead
regions equal OGS-00007's `sparse_regions.tsv`.

## Generate a candidate

```sh
pixi run generate-candidate <store-id> \
  --config resources/generators/gwas-catalog-ragged/release-chen-2023-european.yaml --cores 32
```

OGS-00006 and OGS-00007 are `built`, so a regeneration emits into a temporary
`--registry-root` for comparison; a new release of either needs a new Store
Release id.
