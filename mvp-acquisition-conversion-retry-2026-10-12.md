# MVP dbGaP conversion retry and metadata-refresh qualification

**Status:** the two owner-authorized conversions are independently verified.
The full acquisition metadata refresh is **not accepted**; no Store build,
registration, completion, artifact promotion or full acquisition PASS is claimed.
Evidence below concerns 2026-10-12; parent-owned tool qualification resumed later.

## Authorized two-accession conversion

Owner permission: [#220 bounded retry decision](https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6133854088).
Canonical counts and labels use the approved dbGaP authority policy from PR #238;
Catalog discrepancies remain provenance, not replacement canonical values.
Runtime: verified read-only `218-current-runtime` environment, core `4e922566`.

Output root:
`/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/`.
The `GIA` link points to existing raw inputs; no download or extraction occurred.
One converter, `--jobs 1`, ≤4 CPUs, BLAS threads=1; only these two raw inputs:

| Accession | dbGaP member | source bytes |
|---|---|---:|
| GCST90476298 | `MVP_R4.1000G_AGR.GIA.Labs_batch3/MVP_R4.1000G_AGR.Platelet_Mean_INT.AFR.GIA.dbGaP.txt.gz` | 905029695 |
| GCST90481129 | `MVP_R4.1000G_AGR.GIA.PheCodes_SenseOrgans_batch2/MVP_R4.1000G_AGR.Phe_371_3.META.GIA.dbGaP.txt.gz` | 894608596 |

Executed CLI sequence (candidate path:
`resources/data/derived/store-candidates-analyses.tsv`):

```sh
PY=/home/gh13047/repo/opengwasdb-stores-wt/218-current-runtime/.pixi/envs/dev/bin/python
ISO=/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z
CAND=/home/gh13047/repo/opengwasdb-stores/resources/data/derived/store-candidates-analyses.tsv
$PY resources/scripts/acquire-dbgap-phs002453.py --out "$ISO" map --candidates "$CAND"
$PY resources/scripts/acquire-dbgap-phs002453.py --out "$ISO" convert \
  --candidates "$CAND" --mapping "$ISO/mapping.tsv" \
  --analysis GCST90476298,GCST90481129 --jobs 1
$PY resources/scripts/acquire-dbgap-phs002453.py --out "$ISO" manifest \
  --analysis GCST90476298,GCST90481129
```

`map` binds all 6022 inputs: mapping MD5 `7a650879e70cdad773a912a168f0d856`,
report `5a1ab1262266b47f6de258b6114b9e79`, review
`08dd887bd789c0d6d46f89de0266cb4f`. These are isolated mapping outputs.

### Verified data and metadata identities

Relative to the isolated `gwas-ssf/` root:

| Accession | data path | bytes | source/output line count |
|---|---|---:|---:|
| GCST90476298 | `GCST90476001-GCST90477000/GCST90476298/GCST90476298.tsv.gz` | 912303336 | 36969488 |
| GCST90481129 | `GCST90481001-GCST90482000/GCST90481129/GCST90481129.tsv.gz` | 893395306 | 27899882 |

- GCST90476298 data SHA256:
  `176767974677bb0e846268872607a9ad50ea4d32b8ac4e2ddce2b66f9fe181ce`.
  Sidecar SHA256:
  `67fe26149cb5c4ea373a483e15c55cfa26625d7d26dc96fc9cd0b39ab7588593`.
- GCST90481129 data SHA256:
  `e40f44f8c219ff4a5702cf2df789b8d12e0bd787297cb61f3ab7179d41e111b6`.
  Sidecar SHA256:
  `6e9e1e0baf7a8584098b9179105ca6cb14f146845c3bae2538a6019629eb3dee`.

Worker gzip/header checks passed. The coordinator independently streamed **only
these two outputs**, verifying SHA256/MD5, sidecar hashes, accession/member/data
binding and canonical source counts with retained Catalog provenance:
[#220 verification](https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6134238182).

- D1 canonical N=114731 versus Catalog N=235.
- D2 canonical N=571620, cases=49766, controls=521854, AFR/AMR/EUR mixture;
  Catalog N=577873, cases=50122, controls=527751 and four-ancestry provenance
  remain separate. META is not relabelled European.

The two-row `manifest` outputs are **conversion evidence only**, not full
canonical store manifests. Historical manifests still record errors for these
accessions; no historical manifest was replaced.

## Full metadata-only refresh: implemented, not operationally accepted

`refresh` updates canonical metadata for **all** mapped sidecars and merges the
four legacy manifests. It reads candidate/mapping TSV, sidecar YAML, manifest TSV
and file `stat()` information, not association data bodies. Legacy data digests
are explicitly inherited/not revalidated; the two new rows use separately
verified conversion receipts. Policy-read-only data links beside output sidecars
are not filesystem-enforced access controls.

Ready rows require valid non-empty SHA256, successful status, complete exact
membership, coherent accession/member/tar/data/metadata bindings, current file
presence and matching sizes. Preview can report non-ready rows; it cannot turn
missing/invalid evidence into readiness. All outputs remain isolated.

### Unaccepted execution and containment

The worker ran:

- `metadata-refresh-20261012T225221Z/` with the pre-hardening tool;
- `metadata-refresh-final-20261012T230702Z/` with reported code `bf2e996`,
  followed by an idempotence rerun.

The second run was performed without the coordinator authorization expressly
required, relying on a retracted advisory reviewer GO. Report `11e2f7a` called
it "authoritative"; **that designation was rejected**. Both directories are
preserved in place as unaccepted execution evidence, not accepted inventory.
Reported counts (not an independent qualification): 6022 sidecars, four legacy
manifests with African1368/East-Asian241/European3360/Hispanic1053 rows, 6020
inherited and two derived SHA256 records. The European legacy group still needs
an exact META split under #221.

The worker's snapshot helper included data symlinks:

```sh
find "$1" -type f -o -type l | sort | xargs md5sum 2>/dev/null | md5sum
```

Wrapped in `timeout 1500`, this dereferenced association files beyond the allowed
two-file data-read scope. The worker reports sequential xargs/default `-P1`;
exact read bytes, completed files, last file and wall clocks are unknown because
per-file output was discarded. **No reconstruction data scan is permitted.**
The corrected regular-metadata/link-target snapshot reportedly was
`6d34ff2c8acd2d1cece5cf3995ef6ec5`; it does not retroactively authorize or qualify
the execution.

Worker operational authority was revoked; existing outputs, commits, uncommitted
changes and logs remain frozen. Historical mirror mapping/report hashes and four
legacy manifest mtimes were independently reconfirmed; no active matching scan
was seen at the coordinator's point-in-time check. No data writes are reported;
this is not a blanket independently rehashed proof for all legacy data.

Audit:
`/data/opengwasdb/work/epic232/220/metadata-refresh-execution-audit-2026-10-12.md`.
Public record: [incident](https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6134683531)
and [containment](https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6134725213).

### Parent-owned qualification

Worker checkpoint `aed1eac` passed 62 fixture tests but remained insufficient:
parent tiny probes reproduced an internal-output temp symlink overwriting another
metadata row and an external-source hardlinked temp mutating source bytes.
Containment checks alone do not detect shared inodes. Sidecar temps had the same
class of risk. No real source files were used by these probes.

The parent-owned correction uses exclusive new-temp creation for **all** refresh
metadata writers (sidecars, receipts, manifests), rejects final/temp symlinks,
preserves and refuses existing temps including hardlinks/stale regular files,
and retains legitimate completed-run idempotence. Four added regression methods
exercise these cases across all writers; they were RED before the correction.
Fresh exact-checkpoint review and coordinator probes are required before any
new live metadata-only refresh. Reviewer GO is never execution authorization.

## Parent-authorized metadata qualification — 2026-10-13

Following both independent exact-code GO verdicts and passing parent safety
probes, the coordinator separately authorized and directly executed code
`baf141c8061a8d7f63c369b7b0ff499747e3dabc`. Fresh output:
`/data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z/`.

- Two refresh passes exited 0: 6022 sidecars and manifest rows, 4656 canonicalised,
  1364 unchanged and two new-conversion rows; 6020 inherited/two derived SHA256
  records. The four legacy-group counts remain 1368/241/3360/1053; META still
  requires #221's exact partition.
- Parent independently checked every output's canonical trait/N, source identity,
  expected data/metadata paths, data `stat` size, metadata byte size, historical
  versus new SHA provenance and binding to the correct read-only data link.
- All 6032 snapshotted input **metadata** files remained byte/size/mtime-identical.
  Output regular metadata and link-target text were byte-identical on the second
  pass; no `.partial` remained. Snapshot SHA256:
  `1f14b88c2c383d8bac97215a8807503ef600fed2d2a019067e9962777438310f`.
- The parent Python audit guard rejected association/compressed/genotype opens
  and writes/mutations outside the fresh output root. Zero forbidden attempts
  occurred; **zero association-body opens or legacy data rehashes**. Snapshots
  recorded symlink targets, never their data contents.
- Wall time 57.45s, peak RSS 139480KiB, two-CPU affinity and BLAS=1. Run interval:
  2026-10-13T12:39:14.164553Z–12:40:10.636102Z.
- A first wrapper setup attempt stopped before invoking `refresh`: the generated
  candidate TSV is untracked and absent from git archives. Its empty directory
  is preserved; the successful retry snapshotted the actual named candidate TSV.

Machine receipt:
[`dbgap-phs002453-metadata-refresh-2026-10-13.json`](https://github.com/opengwas/opengwasdb-stores/blob/79fbfd33ddc51027eb89e00b8e1725e10f7dc6a4/resources/inventories/dbgap-phs002453-metadata-refresh-2026-10-13.json).
The runner, input metadata snapshot, execution log and resource log are preserved
under the fresh output root and identified by metadata hashes in that receipt.
[Coordinator run record](https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6145793629).
Independent actual-output metadata audit is pending. This is technical metadata
qualification, **not** release/scientific acceptance or mirror promotion; the
prior unapproved directories remain unaccepted.

## Remaining gates

- Complete independent actual-output metadata review and qualify the code/receipt
  feature PR through CI before integration.
- Accept/freeze the appropriate acquisition evidence explicitly; do not promote
  isolated outputs to the historical mirror without separate permission.
- Advance #221 candidate bundles in parallel, with pending human acceptance and
  no fabricated built/registered status. Separate META exactly; META remains
  observed-only. AMR reference absence gates completion execution, not candidate
  bundle definitions.
- #220 stays OPEN until its actual acceptance criteria are met. Two verified
  conversions are not full 6022-input acquisition acceptance or a Store release.
