- **OGS-00011's Release Erratum names three Analyses whose stored statistics
  must not be used (issues #403, #412).** GCST002047 writes odds ratios in
  `beta` (its rs7903146 row gives beta 1.001, SE 0.011 and p 0.9588, where the
  Store holds z -91.0); GCST004461 writes log-odds in `odds_ratio` and
  `hm_odds_ratio` (its rs569167217 row gives 0.1508, SE 0.1528 and p 0.3237,
  where the Store holds z 12.38); GCST90165257 writes a REGENIE log-scale
  effect in `odds_ratio` (28,878,664 of 57,644,247 rows are negative, and the
  effect-consistency check measures a median |Δlog10 p| of 12.9). Their stored
  statistics, and the cells Reference Completion imputed from them, are wrong.
  opengwas/opengwasdb#345 (ADR 0069) now refuses such sources at resolution,
  and #478 tracks the rebuild. Release Status is unchanged.
