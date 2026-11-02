- **The release benchmark now measures the seven query shapes on OGS-00012 to
  OGS-00016, and the committed report covers every registered Store (issue
  #407, epic #232).** Each anchor is the Store's first Analysis in
  `analyses.tsv`, except where that Analysis has no row in
  `top_hits/p_5e_08`; the anchor is then the first Analysis that has one, as
  for OGS-00009, OGS-00015 and OGS-00017. OGS-00009's existing anchor changed
  (`ukb-b-13414` to `ukb-b-10003`) for that reason. OGS-00006 is Ragged, so its
  shapes stay `not_applicable`.
