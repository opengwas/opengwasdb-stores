- **The five shipped HGDP+1kGP LD panels (AFR, EAS, SAS, EUR control, AMR) are
  repaired in place (issue #348).** `tools/ld-panel/repair_panel.py` inverted
  every row's `EAF` to the frequency of its `EA` and dropped the losing row of
  each duplicated ALID (AFR 291,411 rows, EAS 116,794, SAS 141,543, EUR 134,043,
  AMR none). Every block was verified against its pre-repair backup with no
  failure, and each panel's EAF now correlates r > 0.97 with the UKB ancestry
  mixture (it was r < -0.96). Counts, receipts and their sha256 are in the three
  `hgdp1kgp-*` resource declarations. OGS-00014's imputed EAF was already
  corrected in place under #409, so the completions still affected were
  OGS-00002, whose completion had read the unrepaired EUR panel and was later
  re-completed against the repaired one (`348-rebuild-ogs00001-recomplete-ogs00002.fixed.md`),
  and OGS-00022 and OGS-00023, which are registry entries
  only -- their Store files are no longer on disk.
