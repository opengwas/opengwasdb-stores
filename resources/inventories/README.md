# Source Inventories

A **Source Inventory** is the frozen record of which upstream Analyses
acquisition actually produced usable files for, one row per discovered Analysis.
It is where Phase B starts: a generator selects release membership from a
frozen inventory rather than from a glob over an acquisition mirror, so the
selection it made is checksummable, diffable in a pull request, and unaffected
by the mirror changing underneath it.

`resources/inventories/<snapshot-id>.tsv` holds the rows;
`<snapshot-id>.meta.yaml` holds the provenance that makes the snapshot
reviewable — the checksum of the TSV itself, the acquisition manifests it was
merged from (with their checksums and per-status counts), the candidate table
that fixes the selection scope, and the duplicate-content groups awaiting a
review decision.

A Source Inventory is rows, not a metadata tier: the Source Collection is a
string on the release, and this directory holds data files
([ADR 0024](../../docs/adr/0024-one-family-record-no-source-collection-tier.md)).

## Columns

| Column | Description |
|---|---|
| `analysis_id` | Registry Analysis ID (the upstream `GCST` accession). Unique within an inventory. |
| `publication_pmid` | Publication PMID, from acquisition. |
| `trait` | Source trait label, from acquisition. |
| `study_design` | `quantitative` or `case-control`; selects the method tier. |
| `sample_size` | Source total sample size. Provenance, not Analytical Metadata: the resolution stage resolves the release's own `sample_size_kind`/`sample_size`. |
| `readiness_status` | The acquisition outcome (see below). |
| `data_url` / `yaml_url` | Upstream locations, recorded rather than reconstructed. |
| `data_file` / `yaml_file` | The **exact** local mirror paths acquisition wrote. A consumer never rebuilds a filename: harmonised names come in both `<GCST>.h.tsv.gz` and `<PMID>-<GCST>-<EFO>.h.tsv.gz` shapes. |
| `data_bytes` / `yaml_bytes` | Compressed sizes recorded at acquisition. Blank means "acquisition recorded none" — absence, never zero. |
| `sha256` | Checksum of the source file recorded at acquisition. |
| `error` | The acquisition error, when there was one. |

## Readiness vocabulary

| Status | Meaning | Ready |
|---|---|---|
| `ok` | Downloaded, metadata-gated, header-gated and checksummed in this pass. | yes |
| `already_present` | Same gates, but the transfer was skipped because the file was already there. | yes |
| `missing_remote_harmonised_yaml` | No harmonised file exists upstream (catalog coverage gap). | no |
| `header_rejected` | File present, but its header lacks columns the GWAS-SSF reader requires (in this pool, `beta`). | no |
| `metadata_rejected` | Upstream `-meta.yaml` is not GRCh38 or not harmonised. | no |
| `data_failed` | Transfer failed; no data file. | no |
| `yaml_failed` | Metadata transfer failed. | no |
| `dry_run` | Names resolved, nothing downloaded. | no |
| `error` | Unexpected exception during acquisition. | no |

Only `ok` and `already_present` are ready. The vocabulary is owned by
`resources/generators/lib/source_inventory.py` (`READY_STATUSES`,
`KNOWN_READINESS_STATUSES`) so that "ready" has exactly one spelling, and an
unknown status is a loud failure rather than an implied "not ready".

Unavailable inputs stay in the inventory as facts. They are **not** release
members: a selection step reads the ready rows and never reclassifies a
non-ready one.

## Retry overlay

Acquisition writes one status manifest per pass. A retry pass re-attempts the
transient failures of the first, and its rows are authoritative for every
`analysis_id` they cover — it is the later observation of the same upstream
file. `freeze` therefore overlays the retry manifest on the base manifest and
records, in the provenance sidecar, both manifests' checksums and per-status
counts plus the number of rows the overlay replaced. That is where a status
count that differs from the base manifest's is explained rather than silently
replaced.

## Freezing and preflighting

Both commands are driven by the release's generator config, which names the
snapshot it selects from; both are pure-Python (no `opengwasdb`, no R) and run
in the default Pixi environment.

```sh
pixi run inventory-freeze     # acquisition manifests + candidates -> frozen inventory
pixi run preflight            # prove the frozen inventory before reading any association row
```

`inventory-freeze` writes `<snapshot-id>.tsv` and `<snapshot-id>.meta.yaml`, then
commit them. It fails on duplicate `analysis_id`s, an unknown readiness status, a
candidate with no acquisition row, an acquisition row outside the candidate
pool, or a duplicate candidate accession — anything that would leave a row
unaccounted.

`preflight` is the cheap gate before a resolution run (and is distinct from the
Phase A production workflow's Preflight Run, `CONTEXT.md`). In seconds, and
without opening a single GWAS-SSF file, it reports:

- total and per-status counts, and the ready count/bytes with the
  quantitative/case-control split;
- the planned method tier per study design, reference-AF fallback policy, and
  every expected exclusion (each non-ready readiness status);
- missing source files, files whose size changed since the freeze, and metadata
  files that are missing, unreadable, or no longer declare GRCh38 harmonised
  source;
- duplicate-content groups for review;
- every declared Reference Resource with its presence, size and whether this
  release requires it — including resources the registry declares but this
  release does not use, which is how a decision about a reference fallback sees
  the fact that the panel is absent;
- the requested cores (refusing more than the release's cap), the work root's
  usability and free space.

It exits non-zero when the frozen snapshot cannot be trusted: the TSV no longer
matches the checksum its provenance sidecar records, the snapshot is not the one
the config declares, a required Reference Resource (or an ancestry-mixture
reference's fine-group map) is absent, a ready source or metadata file is gone
or changed, a study design has no declared method tier, the requested core count
exceeds the cap, or the work root is unusable or below the declared free-space
minimum.

Its machine-readable report is written under the release's work root
(`<work_root>/preflight/<snapshot-id>.json`) for use as candidate evidence.

`preflight` compares recorded **sizes**, never checksums: verifying 1.8 TB is not
a preflight, it is the resolution run's job. It also never treats a non-ready row
whose file has since appeared as ready — that only signals the frozen snapshot is
stale and should be re-frozen deliberately.
