# Store Audit

The specification of the read-only Store Audit command (stores #372, epic #371).
It follows [ADR 0023](../adr/0023-the-registry-store-seam-is-a-command-line.md)
(the seam is a command line) and leaves source reading, Store access and format
semantics with `opengwasdb` (ADRs 0012 and 0017). The words are
[CONTEXT.md](../../CONTEXT.md)'s. This file is the contract: the command, what
it reads, what its report says, and how a reader is meant to act on it.

## The command

```text
python3 -m ogstores.audit reconcile <store_path> --output <evidence_json>
pixi run audit-store reconcile /data/opengwasdb/stores/OGS-00011/store.opengwasdb \
  --output /data/opengwasdb/work/epic371/orchestrate/opengwasdb-stores-371/evidence/372/reconciliation.json
```

| Option | Meaning |
|---|---|
| `--reference PATH` | reconcile the off-panel partition against this construction variant reference instead of the one the Store records |
| `--skip-reference` | do not reconcile against a reference at all; recorded as `skipped`, never as a pass. Refused together with `--reference` |
| `--no-identities` | skip the variant-table pass (duplicate ALIDs, multiallelics, reference membership). Recorded as `scanned: false` |
| `--band-variants N` | axis entries one band of the occupancy scan spans (default 4,000,000). The answer does not depend on it |
| `--verify auto\|sha256\|metadata` | how the Store is proved unchanged: `sha256` every file, `size+mtime_ns` only, or `auto` (sha256 up to 512 MiB) |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | reconciled; every disagreement is a `finding` |
| 1 | reconciled, and the Store's artifact and its own record disagree (`error`) |
| 2 | the Store cannot be audited at all: no manifest, not a hybrid Store, a required artifact absent, a missing reference, a bad invocation |

A `2` is never a report about the Store, and a `1` is never a pass. The command
exits `2` rather than writing a report that reconciles something other than the
Store the caller named, and refuses an `--output` inside the Store.

## Scope

One slice, and deliberately not more: this command reconciles the **axis**, not
its causes. It reads the Store Release's own artifact and its own record --
`manifest.json`, `variants.tsv.gz` and its ALID search index, `data.zarr`'s
`ragged/offsets`, `ragged/variant_index` lengths and `ragged/by_variant/offsets`,
`dense/dense_to_shared.npy`, `analyses.tsv`, and `records/build.json` beside it
-- and the construction reference the record names. It never reads a Build
Recipe, so a Store whose recipe has moved on is still audited as built.

What it does not measure is in the report's `scope.not_measured`, with the
ticket that owns each: per-Analysis and per-publication attribution, support
across Analyses, source frequency evidence, and rsid groups. Their absence is
stated, not left to be inferred from a report that has no field for them.

## What it reconciles

| Fact | Read from | Reconciled against |
|---|---|---|
| shared-axis length | `VariantAxis` over `variants.tsv.gz` | recorded `provenance.n_variants`, table row count |
| Dense-axis length | `dense/dense_to_shared.npy`, the Dense Component's own axis | recorded `provenance.hybrid.n_panel` |
| off-panel length | axis minus the distinct mapped rows | recorded `provenance.hybrid.n_off_panel` |
| observed occupancy | `ragged/by_variant/offsets` in bands: `counts = diff(offsets[lo:hi+1])` | the Analysis-major `ragged/offsets`, the recorded `n_overflow_associations` |
| variant-major index span | `ByVariantReader` (spans the axis, not stale against the component) | recorded `provenance.ragged.by_variant` |
| Analyses | `ragged/offsets` entries minus one | `analyses.tsv` rows, recorded `n_analyses` |
| canonical identity | every `variants.tsv.gz` row, plus the sorted ALID search index | the axis: one row per canonical ALID |
| construction reference | `read_variant_reference` on the recorded path | the axis, and the Dense Component's row set |

Canonical identity is counted twice on purpose. The table streaming pass counts
duplicates only among rows sharing a position -- the axis is position-ordered --
while the sorted ALID search index counts them exactly, with no ordering
assumption, by counting adjacent equal entries. The rows whose ALID is wider
than the index's fixed 32-byte key are counted from the table instead, because
the index leaves them out ([`opengwasdb` issue #127](https://github.com/explodecomputer/opengwasdb)).
Two counts that agree are the evidence that either is right.

The construction reference is recorded by identity (path, size, mtime, sha256,
ALID count) because a panel that moves cannot be reconciled with afterwards.
`role` states which reference it is: the build's own construction panel, not the
LD panel a Reference Completion uses (ADR 0037).

## The report

`schema` is `ogstores.audit.reconcile/1`; `audit_version` is separate and
changes only if the report's meaning does. Adding a field does not bump either.

| Field | Contents |
|---|---|
| `argv`, `started_at`, `finished_at`, `wall_seconds`, `runtime` | what ran, when, and against which `opengwasdb`/Python/NumPy; `audit_version` beside them |
| `store` | path, `manifest_sha256`, id, layout, format version, completion state, `opened_mode: "r"`, `build_revision` (the `opengwasdb` rev from `records/build.json`), `recorded` (every claim reconciled above), `analyses_tsv_rows` |
| `reference` | identity of the construction reference, its ALID count, the axis rows in it and absent from it, and whether the Dense Component's row set is exactly that set |
| `axis` | `n_axis`, `n_dense`, `n_off_panel`, the mapping's own checks (`distinct_entries`, `duplicate_entries`, `strictly_increasing`, `out_of_range`), `reconciled` |
| `occupancy` | `axis`, `dense` and `off_panel`: associations, observed and unobserved variants, single-association variants, the largest, and for off-panel the first 20 unobserved axis indices; plus a support histogram and the band scan's own checks |
| `components` | Analyses, association totals, index lengths, and `index_validated_by` -- the engine interface that validated the index, not this module |
| `identities` | rows, distinct ALIDs, duplicate groups and extra rows (with `duplicate_alid_source`), the search index's agreement with the table, non-canonical rows, multiallelic sites and rows, missing rsids, and the ALID/column cross-check sample |
| `inconsistencies` | one entry per disagreement: `kind`, `severity` (`error` or `finding`), `detail`, and the counts that justify it |
| `ok`, `error_count`, `finding_count` | the exit code's own summary |
| `unchanged` | the read-only proof: `method`, `files`, and `changed`/`added`/`removed` |
| `resources` | band size, band count, peak RSS (which includes the reference's ALID set), so a rerun can be planned |
| `scope` | what was measured, and `not_measured` with the ticket that owns each |

`occupancy.dense` is not a finding waiting to happen: a Dense-axis entry carries
no Ragged Overflow association by the disjoint partition, because its
associations are in the Dense Component. Only the off-panel axis's unobserved
entries raise `unobserved_axis_entries`, and unobserved entries are a `finding`,
not an `error` -- the epic asks how many there are, not whether they are legal.

## Read-only

Two properties, both checked rather than promised:

1. The release is opened with `opengwasdb.store.open.open_store` and its arrays
   with `arrays(mode="r")`; every array is loaded `mmap_mode="r"`. Nothing is
   written but `--output`, which may not be inside the Store.
2. Every file's size and mtime -- and its sha256 unless the Store exceeds the
   hashing limit -- is recorded before and after the audit and compared. A
   difference is an `error` (`store_modified`) and exits `1`; the report says
   which method it used, so "unchanged" is a claim with a measurement behind it.

Large artifacts stay outside Git, under the deployment's evidence directory
named in the ticket. Small reviewed reports may be committed.

## Measured on OGS-00011

`OGS-00011` is the Store epic #371 is about: a rebuilt Observed-Only Hybrid
Store Release with 3,169 Analyses, built by `opengwasdb` revision
`496bfe9cc9bbc85c60718d62aa4c6d43d83af810` (recorded in `records/build.json`).
The audit took 334.7 s wall and 7.3 GiB peak RSS (`resources.max_rss_kb`
7,489,460) on a 224-core host, read 22,699 files without changing one
(`unchanged.method: size+mtime_ns`), and reconciled:

- 114,514,113 shared-axis variants = 13,415,936 Dense-axis + 101,098,177
  off-panel, all three against the recorded counts;
- 2,908,357,117 Overflow associations, the recorded total, with 0 on the
  Dense-axis side;
- **101,098,177 off-panel entries observed and 0 unobserved** -- every off-panel
  axis entry carries at least one surviving observed association;
- 0 duplicate ALIDs over 114,514,113 distinct canonical identities, 2,526,751
  multiallelic sites covering 6,002,327 rows, and 1,273,627 rows with no rsid;
- 29,789,554 of the 101,098,177 off-panel entries (29.5%) carry exactly one
  association, the single-observer tail the epic's later tickets attribute;
- a construction reference of 13,415,936 ALIDs (`sha256
  7c7ab7bd…`) whose members are exactly the Dense Component's rows: 0 missing
  from the axis, 0 Dense-axis rows absent from it.

The number to carry forward is the occupancy: the off-panel axis is not a tail
of unused pre-filter identities. Something admitted 101,098,177 distinct
variants and observed every one of them, which points the epic's remaining
hypotheses at admission and at source frequencies, not at axis bookkeeping.
