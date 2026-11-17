- **The release benchmark report records a Store's seven query shapes
  `not_applicable`, naming both numbers, when the Store holds fewer Analyses
  than the core harness's `RANDOM_AXIS_SIZE`, instead of a `ValueError` shape
  error (issue #414).** The count comes from the Store's own `analyses.tsv`,
  because a bundle lists Analyses its build can exclude, and the size is read
  from the core module rather than restated. Two anchors move to the first
  Analysis each Store carries with a `top_hits/p_5e_08` hit: OGS-00011 to
  `GCST000998`, and OGS-00004 to `GCST005076`, its bundle's `GCST003566` being
  excluded from the build. A captured harness tail now names the checkout and
  artifact root by marker, so a tracked artifact carries no host path from a
  traceback.
