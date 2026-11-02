- **The five shipped HGDP+1kGP LD panels (AFR, EAS, SAS, EUR control, AMR) are
  repaired in place (issue #348).** `tools/ld-panel/repair_panel.py` inverted
  every row's `EAF` to the frequency of its `EA` and dropped the losing row of
  each duplicated ALID (AFR 291,411 rows, EAS 116,794, SAS 141,543, EUR 134,043,
  AMR none). Every block was verified against its pre-repair backup with no
  failure, and each panel's EAF now correlates r > 0.97 with the UKB ancestry
  mixture (it was r < -0.96). Counts, receipts and their sha256 are in the three
  `hgdp1kgp-*` resource declarations. Stores completed against these panels
  (OGS-00002, OGS-00014) still hold the old values until they are re-completed.
