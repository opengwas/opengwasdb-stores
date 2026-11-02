- **OGS-00014's imputed-cell EAF is corrected in place (issue #409, epic #232).**
  The AMR panel's EAF column was inverted (#348), so `eaf_reference`, its HM3
  copy and the `eaf` of imputed top-hit rows held the other allele's
  frequency. `eaf-reference-flip` flipped 12,668,057 + 1,196,198 + 730,157
  values, guard r -0.996 before and +0.996 after; z, the SE codes and observed EAF
  did not change, but the decoded SE of imputed cells moved by at most 1.6e-6
  relative, so the top-hit SE was re-synced (`--resync-se`). The Store's `release.yaml` carries a Release Erratum.
