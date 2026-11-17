- **The dbGaP MVP EUR completion reference is `ukb-hg38-eur-ld`, and
  `gpm-hg38-eur-ld` is retired (issue #335).** `gpm-hg38-eur-ld` declared the
  UKB EUR LD panel at the generation host's
  `/local-scratch/projects/genotype-phenotype-map/data/ld_reference_panel_hg38`,
  a path absent here; the same panel is declared at its production path through
  the `EUR -> ukb` alias as `ukb-hg38-eur-ld` (#312), which OGS-00012 already
  completed against (#227). Both dbGaP MVP configs now name it, and the retired
  declaration is deleted rather than kept as a stub; git history keeps it, and
  the provenance documents that name it stay as history.
