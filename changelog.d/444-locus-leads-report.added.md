- **The Locus Lead prototype report (issue #444, opengwas/opengwasdb#62).**
  `docs/locus-leads-prototype.html` shows what the prototype tickets
  opengwas/opengwasdb#402–#406 and #409 found when they held genetic-distance
  pruning against plink2 clumping, and why the owner then dropped Locus
  Leads: no rule agrees with r² 0.01 clumping lead by lead, the bp-only count
  saturates below a genome-wide ceiling, the rules that keep up split loci
  that clumping keeps whole, and exact clumping is affordable at build time.
  `n_loci_*` will come from LD clumping at Store build time. A figure sets
  each Analysis's Locus Leads under four rules against its clump count and
  against its own count on HapMap3 variants, and three region figures show,
  lead by lead, where each rule merges or splits what plink2 clumps.
  `tools/locus_leads_prototype_extract.py` writes the only file the page
  reads, `docs/data/locus-leads-prototype.json`, from the engine's committed
  outputs at one commit with each output's sha256, and the chromosome lengths
  behind the ceiling from the EUR panel's variant tables with theirs, and
  writes the region figures' rows to `docs/data/locus-leads-regions.tsv`. It
  refuses to write, naming the field and both values, when a later ticket's
  copy of #403's numbers differs from #403's in any field, when #403's sweep
  and #409's profile give an Analysis different index variants, when a
  statistic recomputed from the per-Analysis counts differs from the committed
  one, when the regions file read other inputs or gives an Analysis other
  whole-genome counts, or when an output or a key is missing; `tests/locus-leads-extract/` tests the
  refusals on synthetic outputs.
  `tools/build-site.sh` renders and installs the page.
