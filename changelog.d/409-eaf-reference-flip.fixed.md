- **`pixi run eaf-reference-flip <store-id>` corrects a completed Store whose
  `eaf_reference` holds the other allele's frequency, in place and once (issue
  #409).** The HGDP+1kGP panel that completed `OGS-00014` carried the panel's
  *other* allele frequency until it was repaired in place under #348, so
  `_orient_row` wrote `1 - f` into `eaf_reference` and, until this correction,
  every imputed cell served the complement of the effect-allele frequency (r =
  −0.9964 against observed EAF before, +0.992 after). On the
  owner's decision of 2026-11-01 the tool rewrites `data.zarr/eaf_reference`,
  `data.zarr/indexed_subsets/hm3/eaf_reference` and the `eaf` of imputed
  `top_hits/<tier>` rows only, stages every array beside the live one through
  its own `zarr.json`, verifies `new == 1 - old` on raw float32 bits (NaN
  payloads and unflipped rows included), repeats the orientation guard on the
  staged primary (r > 0.9), and only then swaps — the originals go to
  `replaced/<UTC>-eaf-reference-flip/` and
  `records/eaf-reference-flip.json` records both sha256 sets, the counts and
  both r values, with each rename announced in the receipt before it happens
  (`moving_aside`, `swapping_in`, `restoring`), both parents fsynced by a helper
  that raises, and `failed_restoration` left in place rather than deleted when a
  recovery cannot be proven. An existing receipt's refusal describes each array
  from the filesystem (`original live`, `corrected live`, `live missing,
  original at <path>`) and suggests clearing it only when nothing moved. It refuses a second
  run, a Ragged, Zarr v2, Observed-Only or non-0.2.0 Store, a top-hit tier
  without `eaf` or `imputed`, a Store without its completion mask, a Store that
  does not read inverted (`r < -0.9` before, `r > 0.9` after), and a runtime
  that is not the pinned one; `--dry-run` holds the release lock, stages and
  verifies in a scratch directory and writes nothing. Observed `eaf`,
  `eaf_baseline`, `z` and `se` are untouched.
