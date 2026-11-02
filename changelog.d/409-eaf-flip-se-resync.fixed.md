- **`eaf-reference-flip` now re-syncs the top-hit SE it leaves stale (issue
  #409).** An `int8_residual` SE plane decodes against the frequency of its own
  cell, and an imputed cell's frequency is `eaf_reference`; `float32(1) - f` is not
  the exact inverse of the inverted panel's `float32(1) - f`, so after a flip the
  decoded SE of imputed cells with a rare original frequency moved by up to
  about 1.6e-6 relative (`OGS-00014`: 0.04% of the codec's half step) while every
  top-hit tier still held the SE decoded before it. `opengwasdb validate` refused
  such a Store at its `rtol=1e-6` (`top-hit index … contains se value inconsistent
  with decoded se plane`). The tool now writes `top_hits/<tier>/se` again from the
  decoded plane, exactly as the core's top-hit writer derives it, after a flip of
  a residual-SE Store; `--resync-se` runs just that step on a Store flipped
  earlier. It refuses unless the flip's receipt is complete and matched by the
  live arrays, any observed cell would change, or any cell moves by more than
  1e-5 relative; stages whole shards beside the live arrays; swaps with the flip's
  announced, fsynced renames; records `records/eaf-reference-flip-se-resync.json`
  with the originals under `replaced/<UTC>-eaf-reference-flip-se-resync/`; and
  checks afterwards that every top-hit SE equals the decoded plane bit for bit and
  the HM3 subset is untouched. `OGS-00014`'s Release Erratum no longer says SE is
  unchanged.
