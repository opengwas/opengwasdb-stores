# Source Inventories

A **Source Inventory** is the frozen record of which intended Analyses
acquisition actually produced usable files for, one row per selected Analysis.
It is where Manifest Generation starts: a release selects its membership from a
frozen inventory rather than from a glob over an acquisition mirror, so the
selection it made is checksummable, diffable in a pull request, and unaffected
by the mirror changing underneath it.

`resources/inventories/<snapshot-id>.tsv` holds the rows;
`<snapshot-id>.meta.yaml` holds the provenance that makes the snapshot
reviewable -- the checksum of the TSV itself, the Selection it was frozen for,
the Source Collection's two connector files (frozen beside it as
`<snapshot-id>.source-metadata.tsv` and `<snapshot-id>.acquisition-manifest.tsv`,
with their originals' checksums), and the duplicate-content groups awaiting a
review decision. Snapshots frozen before #283 (`*-2026-09-10`, `*-2026-09-22`)
record the candidate table and acquisition passes instead; they stay as the
history of the bundles built from them.

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
| `missing_file` | A recorded source file is gone (`SOURCE_VERIFICATION_STATUSES`, #289). | no |
| `missing_index` | Its Tabix index is gone. | no |
| `size_mismatch` | Its size is not the recorded one. | no |
| `checksum_mismatch` | Its sha256 is not the recorded one. | no |
| `index_mismatch` | Its index's sha256 or size is not the recorded one. | no |
| `bad_index` | The index does not open or lists no contig. | no |
| `integrity_failed` | The compressed stream does not decompress to EOF. | no |

A Source Collection whose acquisition only *records* each file's identity --
ukb-b's files were copied into its mirror, not downloaded from a server --
re-verifies the mirror against that record instead of reporting a transfer, and
carries the last seven statuses. They are
`source_inventory.SOURCE_VERIFICATION_STATUSES`; none is ready.

Only `ok` and `already_present` are ready. The vocabulary is owned by
`src/manifest_generation/source_inventory.py` (`READY_STATUSES`,
`KNOWN_READINESS_STATUSES`) so that "ready" has exactly one spelling, and an
unknown status is a loud failure rather than an implied "not ready".

Unavailable inputs stay in the inventory as facts. They are **not** release
members: a selection step reads the ready rows and never reclassifies a
non-ready one.

## The GWAS Catalog's acquisition passes

The GWAS Catalog downloader writes one status manifest per pass. A retry pass
re-attempts the transient failures of the first, and its rows are authoritative
for every `analysis_id` they cover -- it is the later observation of the same
upstream file -- but a later pass may never turn a ready Analysis into a
non-ready one. The GWAS Catalog Acquisition Manifest writer
(`providers.gwas_catalog.write_acquisition_manifest`) applies that overlay once
and records each pass's checksum, per-status counts and overrides beside the
manifest it writes; freeze checksums that record into the inventory's
provenance.

## Freezing and preflighting

Both commands take a release config ([`docs/spec/release-config.md`](../../docs/spec/release-config.md)),
which names the Source Collection's connector files, the snapshot id and the
Selection; both are pure Python (no `opengwasdb`, no R) and run in the default
Pixi environment. The candidate entry point runs them as its first two stages.

```sh
pixi run inventory-freeze --config <release config>   # connectors -> frozen inventory
pixi run preflight --config <release config>          # prove it before reading any association row
```

`inventory-freeze` runs the shared freeze (`manifest_generation.freeze`, #283)
and writes the four snapshot files into the config's
`source_collection.inventory.directory`; commit them. It fails on a selected
Analysis with no Acquisition Manifest row, a row no Analysis claims, or a
duplicate Analysis or file -- anything that would leave a row unaccounted. It
never overwrites a snapshot: when one exists, it writes nothing and refuses
one frozen for another Source Collection or Selection.
[`docs/spec/source-connectors.md`](../../docs/spec/source-connectors.md) is the
authority on its inputs and provenance.

`preflight` is the cheap gate before a resolution run (and is distinct from
Store Release Build's preflight, `CONTEXT.md`). In seconds, and without opening
a single association file, it reports:

- total and per-status counts, and the ready count/bytes with the
  quantitative/case-control split;
- the planned method tier per study design, reference-AF fallback policy, and
  every expected exclusion (each non-ready readiness status);
- missing source files, files whose size changed since the freeze, and recorded
  metadata sidecars that are missing, unreadable, or declare a genome assembly
  other than the release's `source_genome_build`; sidecars declaring no build;
  ready rows with no recorded sidecar or no source URL;
- duplicate-content groups for review;
- every declared Reference Resource with its presence, size and whether this
  release requires it -- including resources the registry declares but this
  release does not use, which is how a decision about a reference fallback sees
  the fact that the panel is absent;
- the requested cores (refusing more than the release's cap), the work root's
  usability and free space.

It exits non-zero when the frozen snapshot cannot be trusted: the TSV no longer
matches the checksum its provenance sidecar records, the snapshot is not the one
the config declares or was not frozen by the shared freeze for the config's
Source Collection and Selection, a frozen connector beside it is gone or no
longer has the sha256 its provenance records, the frozen acquisition is not
filtered to the config's region policy (or is filtered, and the config declares
none), a required Reference Resource (or an
ancestry-mixture reference's fine-group map) is absent, a ready source or
recorded metadata file is gone or changed, a study design has no declared method
tier, the requested core count exceeds the cap, or the work root is unusable or
below the declared free-space minimum. A ready row without a source URL is a
warning: the Acquisition Manifest makes it optional (#287).

Its machine-readable report is written under the release's work root
(`<work_root>/<store-id>/preflight/<snapshot-id>.json` from the entry point,
`<work_root>/preflight/<snapshot-id>.json` from `pixi run preflight`) for use as
candidate evidence.

`preflight` compares recorded **sizes**, never checksums: verifying 1.8 TB is not
a preflight, it is the resolution run's job. It also never treats a non-ready row
whose file has since appeared as ready -- that only signals the frozen snapshot is
stale and should be re-frozen deliberately.

## Declared-selection inventories

Not every Source Collection is described by an external manifest. The `ukb-b`
batch (#219, #289) has a fixed, **declared** membership: the registry's own
`ukb-b-restored-2026-10-11.declared-selection.tsv` is the intent, and the
provider JSON sidecar beside each VCF supplies the study metadata. ukb-b's
adapter writes those two inputs' Source Metadata Table and its acquisition
writer derives the Acquisition Manifest from the recorded identity and
integrity receipts, so the **shared** freeze reconciles them like any other
Source Collection (`resources/generators/ukb-b/README.md`). There a glob is
never intention: a declared Analysis the Acquisition Manifest does not account
for fails the freeze by name, and one whose file is gone is a non-ready row
with the reason.
