- **The Locus Lead prototype report (issue #444, opengwas/opengwasdb#62).**
  `docs/locus-leads-prototype.html` shows what the prototype tickets
  opengwas/opengwasdb#402–#406 and #409 found when they held genetic-distance
  pruning against plink2 clumping: per lead no (D, W) agrees with r² 0.01
  clumping, while by per-Analysis counts a bp-only window does as well as any
  cM rule, so `n_loci_*` takes bp-only W 10 Mb for EUR and EAS and none for AFR
  and AMR. `tools/locus_leads_prototype_extract.py` writes the only file the
  page reads, `docs/data/locus-leads-prototype.json`, from the engine's
  committed outputs at one commit with each output's sha256, and refuses to
  write when two outputs that share a number disagree. `tools/build-site.sh`
  renders and installs the page.
