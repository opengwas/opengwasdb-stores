# EAF reference flip (#409)

`pixi run eaf-reference-flip <store-id> [--dry-run] [--resync-se] [--workers N]
[--artifact-root P] [--registry-root P]` is the one-off, value-changing in-place correction of a
Dense, format-0.2.0, Zarr-v3, Reference-Completed Store whose `eaf_reference`
holds the other allele's frequency. It is the second in-place Store rewrite this
project allows and the first that changes decoded values: the #326 SE re-encode
(`docs/se-reencode-2026-10-26.md`) was physical-representation only and set no
precedent after release, so this one is a Release Erratum, decided by the owner
on 2026-11-01 (issue #409) for `OGS-00014`.

## What is wrong

Completion writes `eaf_reference` from the LD panel's own EAF through
`panel_a1_eaf` and `_orient_row`. At the time `OGS-00014` was completed, the
HGDP+1kGP panel it used carried the frequency of the panel's *other* allele (the
panels were corrected in place under #348), so every finite value in that plane
was `1 - f`. `StoreQuery` substitutes the plane into imputed cells at read time
(`_apply_eaf_reference`), so until the Store's correction every imputed cell
served the complement of the effect-allele frequency. Observed cells never read
the plane, which is why the observed data read correctly (r = +0.99 against an
independent reference) while the panel-derived values read r = −0.9964 against
the same observations. The Applied section records the correction; after it the
same comparison reads r = +0.992.

## What the tool does

1. **Refusals before any write.** A registered, non-symlinked Store; no
   `publication.json`, `store.opengwasdb.partial` or completion marker; the
   imported *and* PATH `opengwasdb` both the commit `pixi.lock` resolves (the
   same checks `ogstores.se_reencode` makes, called rather than copied); no
   `records/eaf-reference-flip.json`; and a Dense / `0.2.0` /
   Zarr-v3 / `reference_completed` Store Manifest. Ragged, Hybrid, Zarr v2 and
   Observed-Only Stores are refused with the reason, never skipped, and so is a
   top-hit tier that carries no `eaf` or no `imputed` — its imputed hits would
   keep the wrong orientation — and a Store with no `imputed` completion mask.
2. **The orientation guard.** A deterministic sample of variants that carry both
   a finite `eaf_reference` and an observed EAF in at least one Analysis
   (60 bands of 200 rows, seed recorded) is read through the runtime's own published
   `opengwasdb.encoding.DenseEafPlane`, the plane `StoreQuery` reads. The
   Pearson r between that plane and the mean observed EAF must be `< -0.9`
   over at least 500 variants. The same measurement is repeated on the staged
   primary and must read `r > 0.9` before anything is renamed; equality at
   either boundary is not evidence and refuses.
3. **Staging.** Each target array is written to `.<name>.flipping` in its own
   group, with the live `zarr.json` copied verbatim, so codecs, chunking,
   sharding and fill value cannot drift. Slices are shard-aligned, so no shard is
   read-modify-written. `float32(1) - x` is computed in float32 — the same
   operation on the same input is bit-identical, which is what `validate`'s
   exact comparisons require — and only on finite cells; a NaN keeps its payload.
   `top_hits/<tier>/eaf` is flipped only where that tier's `imputed == 1`.
4. **Verification.** `new == 1 - x` on raw float32 bit patterns, NaN positions
   preserved, unflipped rows bit-identical, and the HM3 copy equal to the staged
   primary at its `variant_index` rows. A failed verification removes the staged
   copies and leaves the Store byte-identical; an injected failure proves it.
5. **The swap.** `store/swap.py`'s two-rename pattern at directory level: the
   original array directory moves to
   `<root>/<id>/replaced/<UTC>-eaf-reference-flip/<group>/<array>`, then the
   staged directory moves into its name. Every rename is announced in the
   receipt before it happens (`moving_aside`, `swapping_in`, and `restoring`
   on the way back) and its outcome written after, with every newly created
   directory under `replaced/` and both parents of every rename fsynced — by a
   helper that raises, so a durability failure is never silent. A failure puts
   every original back and claims `restored` only where the rename, its fsync
   and the live path's hashes all prove it; only then are this run's staged
   copies, the receipt and `replaced/` removed. When even that proof fails,
   nothing is deleted: the receipt is marked `failed_restoration` and the run
   describes each array from the filesystem — `original live`,
   `corrected live`, `live missing, original at <path>` — so the evidence
   outlives the failure. A hash that cannot be read during recovery is a
   per-array failure, not an escape from the receipt.
6. **The receipt.** `records/eaf-reference-flip.json`, written `started`
   immediately before the first rename and `complete` after the last: per touched
   array the sha256 of every file before and after, finite/NaN counts, rows
   flipped, the guard before and after with its seed, the `opengwasdb` revision
   and version, and the tool's path and commit — `tools/relocate_store.py`'s
   shape.

`--dry-run` holds the release lock for its whole span, like apply, and performs
the guard, staging, verification and the second guard in a scratch directory
outside the Store, prints the plan and the counts, and writes nothing in the
Store or its records.

An existing receipt always refuses, and the refusal is read from the filesystem
rather than from the receipt's own `state`, which a kill can leave one
transition behind: each live array is hashed against the recorded before- and
corrected-hashes, and each original under `replaced/` against its
before-hashes. The message states what each array actually is — `original live`,
`corrected live`, `live missing, original at <path>` — and suggests clearing the
receipt only when that inspection proves nothing moved; otherwise it says to
restore first. A receipt that records no hashes at all is described by presence
only, never as matching.

Nothing else is touched by the flip itself: not `manifest.json`, `index.sqlite`,
`overview.html`, `analyses.tsv`, not another array, and not
`data.zarr/indexed_subsets/hm3/eaf` (an int8 residual against its own baseline,
unaffected by the flip). The SE of a residual-SE Store is the exception the next
section describes. The array
directory swap changes `data.zarr`'s and its parents' mtimes but not
`store.opengwasdb/`'s, so the release workflow still plans nothing for the
Store. The Store-root files a Release Erratum must report on
(`stores/<id>/release.yaml` notes, the Validation Record, the catalog) are the
operator's step, not this tool's.

## The SE coupling

A Store whose `se` plane is `int8_residual` stores each SE as a code against a
prediction `c0 + c1 * log(2 f (1 - f))` that takes the frequency `f` of the
*same cell*, and an imputed cell's `f` is `eaf_reference`. The flip therefore
changes the decoded SE of imputed cells even though no SE code is rewritten.
`float32(1) - f` is not the exact inverse of the `float32(1) - f` that the
inverted panel had already applied: near 1, float32 resolves about 6e-8 in
absolute terms, so for a rare variant `1 - (1 - f)` differs from `f` in its
last bits. The decoded SE moves by about `|c1| * 3e-8 / f` relative, up to a few
parts in a million at the panel's 1% frequency floor, and only where the
original reference was below 0.5. That is far below the codec's own half step,
and above `validate`'s comparison of a top-hit tier's `se` with the decoded
plane (`rtol=1e-6`, `atol=0`). A tier copies its SE from the decoded plane when it
is built (`opengwasdb.layouts.dense.top_hits`, `_collect_top_hit_se`: the plane
decoded one row chunk at a time and stored as float32), so after a flip every
tier still holds the SE decoded against the old reference, and `validate`
refuses the Store (`top-hit index p_5e_08 contains se value inconsistent with
decoded se plane`). It was missed in the first version of this tool, and
found by the post-flip `validate` of `OGS-00014`.

The follow-on re-syncs `top_hits/<tier>/se` to the decoded plane. It is part of
the same operation: a flip of a residual-SE Store runs it after the swap, and
`--resync-se` runs it alone on a Store this tool has already flipped (the path for
`OGS-00014`, flipped before the follow-on existed). A Store whose SE is not
residual has nothing to re-sync and the step says so.

1. **Refusals.** Everything the flip refuses on, and the flip's receipt must exist,
   read `complete`, and be matched by every live array it names: the follow-on
   re-reads decoded values, so it runs on the corrected Store and no other. A
   tier without `se`, `imputed`, `variant_index` or `analysis_index` is refused,
   not skipped. The follow-on's own receipt refuses a second run.
2. **The decode.** Each tier's SE is gathered with the core's own collector, so the
   value written back is the value a rebuild would write, as float32.
3. **Guards.** A cell is *changed* when its float32 bit pattern differs from the
   decoded one. The step refuses, writing nothing, if any changed cell is not
   imputed (only an imputed cell's SE depends on `eaf_reference`, so anything
   else is a different defect), or if any changed cell moves by more than 1e-5
   relative (the cause would not be the round trip this step exists for).
4. **Staging and swap.** Only tiers with a changed cell are rewritten, each as a
   sibling `.se.resyncing` directory with the live `zarr.json` copied verbatim and
   whole-shard slices, checked to read back as the decoded plane bit for bit,
   with every unchanged row identical to the stored one, then every staged file
   and directory is fsynced (a failure aborts before any rename; the flip's staged
   arrays get the same treatment). The swap is the flip's
   own (`_swap_all`, `_restore`): every rename announced in the receipt before
   it happens, both parents fsynced, a failure restoring each original and
   proving it by hash, `failed_restoration` kept rather than deleted when it
   cannot. Originals go to `replaced/<UTC>-eaf-reference-flip-se-resync/`; the
   receipt is `records/eaf-reference-flip-se-resync.json`: per tier the cells,
   imputed cells, changed rows, maximum relative change and how many cells
   failed `validate`'s rtol before, plus every file's sha256 before and after.
5. **After the swap.** The files must hash to the verified staged copies; then
   every tier's SE is compared with a fresh decode of the plane at `rtol` 0, bit
   for bit, and the HM3 subset's files must hash as they did before. Any failure
   restores the originals.

A flip run that cannot finish the follow-on leaves the flip complete and says to
run `--resync-se`. `--dry-run` of a flip cannot count the cells the follow-on will
move (the Store is not flipped yet) and says the follow-on follows;
`--resync-se --dry-run` on a flipped Store takes the lock, decodes, stages in a
scratch directory, prints the counts per tier and writes nothing. `--workers`
(1 to 16, default 16: #326's cap) is the number of processes the collector
decodes row chunks with, each with Zarr async width and Blosc threads of one, so
the concurrent decodes never exceed the cap. A decode holds a row chunk of every
Analysis at once.

The same coupling would hold for any later rewrite of `eaf_reference` in a
residual-SE Store, not only this flip.

## Measured against the fixture

`tests/eaf-reference-flip` builds a tiny real Store with the pinned runtime
(Zarr v3, sharded, an int8 residual `eaf` plane with a baseline, an `imputed`
mask, an HM3 subset and two top-hit tiers) and reads it back through the same
plane. It builds each fixture variant once per class and copies it per test, and
shares one completed apply across the read-only assertions. Its first four
variants are the issue's literal example: `eaf_reference`
`[0.1, 0.25, NaN, 0.8]` becomes `[0.9, 0.75, NaN, 0.2]`, compared as float32 bit
patterns (`float32(1) - float32(0.8)` is `0x3E4CCCCC`, one ulp below
`float32(0.2)`), and a non-canonical NaN payload (`0x7FC00001`) survives
bit-exact. The guard reads r = −1.0000 before and +1.0000 after over 599
comparable variants.

## Applied

**OGS-00014, 2026-11-01** (owner decision, issue #409), tool at stores `f281909b`,
opengwasdb 0.3.2 (`44f25a96`). Dry run, then apply, both from a clean checkout of that
commit.

| Array | Finite values flipped |
| --- | --- |
| `eaf_reference` (30,380,465 rows; 17,712,408 NaN kept) | 12,668,057 |
| `indexed_subsets/hm3/eaf_reference` (9,270 NaN kept) | 1,196,198 |
| `top_hits/p_5e_04/eaf` (imputed rows only) | 601,392 |
| `top_hits/p_5e_06/eaf` (imputed rows only) | 89,875 |
| `top_hits/p_5e_08/eaf` (imputed rows only) | 38,890 |

- Guard: r = −0.9958 before, +0.9958 after, n = 4,234 comparable variants (60 bands x 200
  rows, seed 20261101).
- Receipt `records/eaf-reference-flip.json`: `complete`, sha256
  `cbf2d3f8e67e1d1d3ce3a72f6245eeddc0041255c5d9f2534c011ba4813c8a8d`. Originals in
  `replaced/20261101T215514Z-eaf-reference-flip/`.
- Read-only checks afterwards: all 84 live files match the receipt's after-hashes;
  `eaf_reference` against mean observed EAF r = +0.992 (n = 3,980); against UKB
  ancestry-mixture South America, oriented to the Store's effect allele, r = +0.985
  (n = 2,217; +0.989 over the 2,112 existing variants); `hm3/eaf_reference` equals the primary at
  all 1,205,468 variants; every imputed top-hit `eaf` equals `eaf_reference` at its variant; observed
  top-hit `eaf` (521,290, 2,649,653 and 43,421,771 rows) is bit-identical to the originals.
- `find -newer` on the Store lists only the five arrays, their parent directories, the receipt
  and `replaced/`. `release-dry OGS-00014` plans no jobs ("Nothing to be done").
- The Store was not re-validated by this step. The Release Erratum is in
  `stores/OGS-00014/release.yaml`.

**OGS-00014 SE re-sync, 2026-11-02** (tool at stores `def9e2c8`, opengwasdb 0.3.2): the
post-flip `validate` had failed all three tiers' SE check, so `eaf-reference-flip OGS-00014
--resync-se --workers 16` rewrote the top-hit SE (26 min; originals in
`replaced/20261102T111229Z-eaf-reference-flip-se-resync/`, receipt
`records/eaf-reference-flip-se-resync.json`, sha256
`f34f054214a0b2d22570af49d979360e4aef0c6ea4cbeac285bab2386e25d8a6`).

| Array | Cells | Re-synced | Failed validate's rtol before |
| --- | --- | --- | --- |
| `top_hits/p_5e_04/se` | 44,023,163 | 126,116 | 3,282 |
| `top_hits/p_5e_06/se` | 2,739,528 | 16,951 | 276 |
| `top_hits/p_5e_08/se` | 560,180 | 7,548 | 98 |

- Maximum relative change 1.48e-6, all imputed cells. The tool's post-swap check (rtol 0, HM3
  files unchanged) passed; read-only afterwards, an independent decode finds 0 mismatches in
  every tier at rtol 1e-6 and at rtol 0, and the files match the receipt's after-hashes.
  `release-dry OGS-00014` plans nothing.
- `opengwasdb validate` 0.3.2 (`44f25a96`), under the release lock, started 2026-11-02
  11:32:35 UTC, elapsed 4 h 29 m 50 s, exit 0: `{"errors": [], "ok": true, "warnings": []}`.
