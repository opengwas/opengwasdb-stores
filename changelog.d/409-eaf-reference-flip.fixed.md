- **`pixi run eaf-reference-flip <store-id>` corrects a completed Store whose
  `eaf_reference` holds the other allele's frequency, in place and once (issue
  #409).** The HGDP+1kGP panel used for completion carries the panel's *other*
  allele frequency, so `_orient_row` wrote `1 - f` into `eaf_reference` and every
  imputed cell served the complement of the effect-allele frequency (OGS-00014:
  r = −0.9964 against observed EAF, where a correct Store reads +0.99). On the
  owner's decision of 2026-11-01 the tool rewrites `data.zarr/eaf_reference`,
  `data.zarr/indexed_subsets/hm3/eaf_reference` and the `eaf` of imputed
  `top_hits/<tier>` rows only, stages every array beside the live one through
  its own `zarr.json`, verifies `new == 1 - old` on raw float32 bits (NaN
  payloads and unflipped rows included), repeats the orientation guard on the
  staged primary (r > 0.9), and only then swaps — the originals go to
  `replaced/<UTC>-eaf-reference-flip/` and
  `records/eaf-reference-flip.json` records both sha256 sets, the counts and
  both r values. It refuses a second run, a Ragged, Zarr v2, Observed-Only or
  non-0.2.0 Store, a Store that does not read inverted, and a runtime that is
  not the pinned one; `--dry-run` stages and verifies in a scratch directory and
  writes nothing. Observed `eaf`, `eaf_baseline`, `z` and `se` are untouched.
