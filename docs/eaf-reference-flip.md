# EAF reference flip (#409)

`pixi run eaf-reference-flip <store-id> [--dry-run] [--artifact-root P]
[--registry-root P]` is the one-off, value-changing in-place correction of a
Dense, format-0.2.0, Zarr-v3, Reference-Completed Store whose `eaf_reference`
holds the other allele's frequency. It is the second in-place Store rewrite this
project allows and the first that changes decoded values: the #326 SE re-encode
(`docs/se-reencode-2026-10-26.md`) was physical-representation only and set no
precedent after release, so this one is a Release Erratum, decided by the owner
on 2026-11-01 (issue #409) for `OGS-00014`.

## What is wrong

Completion writes `eaf_reference` from the LD panel's own EAF through
`panel_a1_eaf` and `_orient_row`. The HGDP+1kGP panel used for `OGS-00014`
carries the frequency of the panel's *other* allele, so every finite value in
the plane is `1 - f`. `StoreQuery` substitutes that plane into imputed cells at
read time (`_apply_eaf_reference`), so every imputed cell served the complement
of the effect-allele frequency. Observed cells never read the plane, which is
why the observed data reads correctly (r = +0.99 against an independent
reference) while the panel-derived values read r = −0.9964 against the same
observations.

## What the tool does

1. **Refusals before any write.** A registered, non-symlinked Store; no
   `publication.json`, `store.opengwasdb.partial` or completion marker; the
   imported *and* PATH `opengwasdb` both the tracked pin (the same checks
   `ogstores.se_reencode` makes, called rather than copied); no
   `records/eaf-reference-flip.json`; and a Dense / `0.2.0` /
   Zarr-v3 / `reference_completed` Store Manifest. Ragged, Hybrid, Zarr v2 and
   Observed-Only Stores are refused with the reason, never skipped.
2. **The orientation guard.** A deterministic sample of variants that carry both
   a finite `eaf_reference` and an observed EAF in at least one Analysis
   (60 bands of 200 rows, seed recorded) is read through the runtime's own published
   `opengwasdb.encoding.DenseEafPlane`, the plane `StoreQuery` reads. The Pearson r between that
   plane and the mean observed EAF must be ≤ −0.9 over at least 500 variants.
   The same measurement is repeated on the staged primary and must read
   r ≥ +0.9 before anything is renamed.
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
   staged directory moves into its name. A failure between them, or on a later
   array, puts every original back and removes this run's staged copies and
   receipt.
6. **The receipt.** `records/eaf-reference-flip.json`, written `started`
   immediately before the first rename and `complete` after the last: per touched
   array the sha256 of every file before and after, finite/NaN counts, rows
   flipped, the guard before and after with its seed, the `opengwasdb` revision
   and version, and the tool's path and commit — `tools/relocate_store.py`'s
   shape.

`--dry-run` performs the guard, staging, verification and the second guard in a
scratch directory outside the Store, prints the plan and the counts, and writes
nothing in the Store or its records.

Nothing else is touched: not `manifest.json`, `index.sqlite`, `overview.html`,
`analyses.tsv`, not another array, and not `data.zarr/indexed_subsets/hm3/eaf`
(an int8 residual against its own baseline, unaffected by the flip). The array
directory swap changes `data.zarr`'s and its parents' mtimes but not
`store.opengwasdb/`'s, so the release workflow still plans nothing for the
Store. The Store-root files a Release Erratum must report on
(`stores/<id>/release.yaml` notes, the Validation Record, the catalog) are the
operator's step, not this tool's.

## Measured against the fixture

`tests/eaf-reference-flip` builds a tiny real Store with the pinned runtime
(Zarr v3, sharded, an int8 residual `eaf` plane with a baseline, an `imputed`
mask, an HM3 subset and two top-hit tiers) and reads it back through the same
plane. Its first four variants are the issue's literal example: `eaf_reference`
`[0.1, 0.25, NaN, 0.8]` becomes `[0.9, 0.75, NaN, 0.2]`, compared as float32 bit
patterns (`float32(1) - float32(0.8)` is `0x3E4CCCCC`, one ulp below
`float32(0.2)`). The guard reads r = −1.0000 before and +1.0000 after over 599
comparable variants.
