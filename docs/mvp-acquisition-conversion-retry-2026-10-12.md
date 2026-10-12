# MVP dbGaP conversion retry — 2 missing accessions (2026-10-12)

Bounded conversion retry authorised for ticket #220 (owner authorisation
2026-10-12). Baseline merged `dev` = `1db347d`; adopted core environment
`218-current-runtime` (`opengwasdb` `4e92256`). This checkpoint converts and
freezes the two previously unmatched accessions in an **isolated** output root.
The historical mirror, its mapping files and its manifests were not written.

## Isolated output root

`/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/`
(`GIA` is a symlink to the read-only raw mirror `GIA`; nothing under the mirror
was modified).

## Inputs (named, already extracted; no download)

| Accession | dbGaP member | bytes |
|---|---|---|
| `GCST90476298` | `MVP_R4.1000G_AGR.GIA.Labs_batch3/MVP_R4.1000G_AGR.Platelet_Mean_INT.AFR.GIA.dbGaP.txt.gz` | 905,029,695 |
| `GCST90481129` | `MVP_R4.1000G_AGR.GIA.PheCodes_SenseOrgans_batch2/MVP_R4.1000G_AGR.Phe_371_3.META.GIA.dbGaP.txt.gz` | 894,608,596 |

## Commands (one converter, `--jobs 1`, BLAS threads 1)

```sh
PY=/home/gh13047/repo/opengwasdb-stores-wt/218-current-runtime/.pixi/envs/dev/bin/python
ISO=/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z
CAND=/home/gh13047/repo/opengwasdb-stores/resources/data/derived/store-candidates-analyses.tsv

$PY resources/scripts/acquire-dbgap-phs002453.py --out "$ISO" map --candidates "$CAND"
$PY resources/scripts/acquire-dbgap-phs002453.py --out "$ISO" convert --candidates "$CAND" \
    --mapping "$ISO/mapping.tsv" --analysis GCST90476298,GCST90481129 --jobs 1
```

`map` reproduced the reconciled mapping exactly (`mapping.tsv`
`7a650879e70cdad773a912a168f0d856`, `mapping-report.tsv`
`5a1ab1262266b47f6de258b6114b9e79`, `mapping-review.tsv`
`08dd887bd789c0d6d46f89de0266cb4f`; 6,022/6,022 bound).

## Frozen conversion evidence

| Accession | data file | bytes | sha256 | lines (source = output) |
|---|---|---:|---|---|
| `GCST90476298` | `gwas-ssf/GCST90476001-GCST90477000/GCST90476298/GCST90476298.tsv.gz` | 912,303,336 | `176767974677bb0e846268872607a9ad50ea4d32b8ac4e2ddce2b66f9fe181ce` | 36,969,488 |
| `GCST90481129` | `gwas-ssf/GCST90481001-GCST90482000/GCST90481129/GCST90481129.tsv.gz` | 893,395,306 | `e40f44f8c219ff4a5702cf2df789b8d12e0bd787297cb61f3ab7179d41e111b6` | 27,899,882 |

Sidecar sha256: `GCST90476298` `67fe26149cb5c4ea373a483e15c55cfa26625d7d26dc96fc9cd0b39ab7588593`;
`GCST90481129` `6e9e1e0baf7a8584098b9179105ca6cb14f146845c3bae2538a6019629eb3dee`.
`gzip -t` passed on both; headers match the expected GWAS-SSF shapes
(quantitative 15 columns; binary 21 columns). Output line count equals the
source member line count for both (no row loss).

## Canonical metadata and retained Catalog provenance

Per the owner decision (dbGaP authoritative), each sidecar carries the deposit
value as canonical and the Catalog counterpart under `catalog_metadata`:

| Accession | canonical `trait_description` / `samples.sample_size` | `catalog_metadata` retained |
|---|---|---|
| `GCST90476298` | `platelet count (mean, inv-norm transformed)` / `114731` (African) | trait as-is; `sample_size: 235` |
| `GCST90481129` | `Inflammation of eyelids` / `571620`, `case_control_study: true` | `Inflammation of eyelids (PheCode 371.3)`; `sample_size: 577873`; `n_cases: 50122`; `n_controls: 527751`; `ancestry_fraction: 0.703775743113106` |

`GCST90481129` also retains the deposit description "AFR, AMR, and EUR MVP
participants" (3-ancestry) while the Catalog 4-ancestry aggregate is provenance.
Provenance fields (`source.repository/study/tar/member/tar_md5/url`) are present
for both.

## Manifest refresh — pure-metadata merge (implemented; mirror untouched)

The existing CLI `manifest --analysis GCST90476298,GCST90481129` regenerates each
per-`store_key` manifest from the files under the isolated `gwas-ssf`, so it
emits only the selected rows (2-row files), not the canonical full store
manifest. The historical mirror manifests are:

| Manifest | rows | `GCST90476298` / `GCST90481129` |
|---|---:|---|
| `dense-pmid-39024449-African-download-manifest.tsv` | 1,368 | `error` (`not converted`) |
| `dense-pmid-39024449-European-download-manifest.tsv` | 3,360 | `error` (`not converted`) |

A canonical refresh must replace exactly the two `error` rows with the new `ok`
rows and preserve the other 4,726 rows (whose sha256 are already recorded). The
CLI has no merge mode, and regenerating would require hashing every converted
file (multi-TB — forbidden). The isolated partial files
(`manifests/dense-pmid-39024449-{African,European}-download-manifest.tsv`) are
kept only as evidence of the refreshed rows and are **not** canonical manifests.

### Proposed bounded tooling fix (for approval)

Add a pure-metadata `manifest` merge mode, e.g.
`manifest --merge-existing <existing-manifest.tsv>`: read the existing canonical
manifest, compute the sha256 of only the refreshed accessions' isolated data
files, replace exactly those rows (by `analysis_id`), keep every other row
byte-for-byte, and write the result to the isolated `--manifest-dir`. Tests:
refreshed row replaces the matching `error` row; unrelated rows preserved;
missing existing manifest / unknown accession fails loudly; a refreshed row
whose `data_file` is absent is not marked `ok`; no full re-hash of preserved
rows (assert the sha256 helper is called only for refreshed IDs). This keeps the
canonical manifest reproducible without reprocessing the 6,020 existing files.

### Authoritative isolated run (post-hardening)

> Note: this run was produced before the final guard set (valid-sha256 for ready
> rows, guarded TSV temp, new-sidecar binding). It is superseded and will be
> repeated with the corrected tool after review; only the re-run counts.

`refresh` reads mapping/manifests/sidecar YAML only — never a `.tsv.gz` — and
carries each existing data identity (`data_file_md5sum`, `data_file_name`,
`source`) forward under a `metadata_refresh` block (`reused_data_md5sum: true`,
`source_sidecar`), so no new validation is claimed. Data is materialised as
read-only symlinks inside the isolated root.

Run into `/data/opengwasdb/work/epic232/220/metadata-refresh-final-20261012T230702Z/`
(full mode, exit 0):

| Result | Value |
|---|---|
| Output sidecars | 6,022 (4,656 canonicalised, 1,364 unchanged, 2 new-conversion) |
| Merged manifest rows | 6,022, all `ok`, across the 4 store manifests |
| `sha256_provenance` | 2 `derived`, 6,020 `inherited` |
| Read-only data symlinks | 6,022 |
| Association files written / `.partial` left | 0 / 0 |

Spot checks: `GCST90480207` canonical trait becomes `Raynauds syndrome` with
`catalog_metadata` retaining `Raynaud's syndrome (PheCode 443.1)` and
`data_file_md5sum` `9976e21f…` unchanged; its merged manifest row keeps
`sha256 17a108b6…` / `data_bytes 553320445` carried forward. The two new rows are
`status=ok` with the freshly derived sha256 `17676797…` / `e40f44f8…`. A second
run produced a byte-identical metadata tree (metadata-only snapshot `6d34ff2c…`,
regular files hashed and symlink targets recorded, never the data contents); the
mirror mapping (`a9b83d6c…`/`f0b00a0b…`), manifests (mtime 2026-10-01) and
sidecar/data mtimes are unchanged.

The earlier `metadata-refresh-20261012T225221Z` run used the pre-hardening tool
and is a superseded dry run.

The refreshed sidecars and merged manifests are **isolated review artifacts**,
not a mirror promotion; the two new rows reference the isolated retry data paths.

### Hardened gates (reviewed; GO on bf2e996)

* Canonical metadata fields (`trait`, `sample_size`, `study_design`, `data_url`)
  are regenerated from the mapping/candidate for every row, so no stale Catalog
  field survives; data identity (`status`, sizes, `sha256`, assembly) is
  inherited and labelled `sha256_provenance=inherited` (the two new rows are
  `derived` in the receipt).
* Strict digest shapes are enforced (`sha256` 64 hex, `md5` 32 hex); a malformed
  inherited digest fails.
* `--out`/`--source-sidecars`/`--source-manifest-dir` overlap is refused, and
  data is materialised as **read-only symlinks inside the isolated root** so
  each refreshed sidecar sits beside its data (no dead relative paths, no legacy
  sidecar symlinks); an existing non-symlink file is never overwritten.
* Data presence: `--preview` reports absent data as a non-ready row and still
  exits 0; the full manifest marks absent data `error` and exits non-zero.
* Identity binding: duplicate accession/member, a member not shaped
  `MVP_R4.1000G_AGR.*.txt.gz`, a missing source tar, or a
  `candidate_store_key`/`store_key` mismatch all fail; the existing manifest row
  set must equal the candidate store set exactly.
* Case-control META keeps its mixture ancestry (`Multi-ancestry`), not a
  European interpretation.
* Full membership is validated as the exact candidate accession set (no store
  silently omitted); the count is not hard-coded.
* Every write target (sidecar, `.partial`, data symlink, manifest, receipt) must
  resolve inside the isolated root, and `--manifest-dir` must be inside `--out`;
  a symlinked bucket/parent that escapes to the read-only source is refused
  before any write, and the source tree is snapshotted unchanged in regression.
* Source bindings are checked: the sidecar's `source.member`/`source.tar` must
  equal the mapping row, `source.tar_md5`/`data_file_md5sum` must be well-formed
  md5, and the existing manifest's `data_file` must bind the source data path.
* New sidecars and new manifests must be a 1:1 set with `status=ok` and a
  well-formed `sha256`; duplicates, unknown IDs or a missing pairing fail rather
  than inventing derived evidence.
* The full run exits non-zero for any non-`ok` row or identity/size mismatch;
  `--preview` reports non-ready rows. `yaml_bytes` is the actual refreshed
  sidecar size, and `data_bytes` is checked against the data file's `stat`.

## Operational note (I/O)

One metadata-only snapshot helper initially used `md5sum` over the data symlinks,
which follows them and reads the referenced association files; it was stopped by
a timeout and replaced with a metadata-only snapshot (regular files hashed,
symlink targets recorded). The inadvertent read was read-only against the mirror
and wrote nothing; no mirror path changed.

## Guarantees and exclusions

* Zero writes to `/data/opengwasdb/raw/dbgap-phs002453` (mirror mapping hashes
  remain `a9b83d6c57874d00f71e2760c4a9edde` / `f0b00a0b3b2bc1422b10bba0b1b27f9b`,
  no mirror `mapping-review.tsv`; mirror manifests unchanged and still `error`
  for the two).
* The 6,020 existing converted files were not rewritten.
* No Store build, completion, acceptance, publication or identity change. No
  deletion.
* This is **not** acquisition completion: the canonical manifest refresh is
  unexecuted and the two files live only in the isolated root. Issue #220 stays
  OPEN.
