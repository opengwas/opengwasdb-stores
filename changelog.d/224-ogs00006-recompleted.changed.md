- **OGS-00006 (Chen 2023, European) is Reference-Completed inside the regions its
  bundle retains (issue #224, opengwas/opengwasdb#430).** A forced release at
  opengwasdb `v0.9.0` rebuilt the Observed-Only Store with its `regions.tsv`
  (format 0.2.1), then completed it only inside its 3,627 significant regions:
  1,038,186 imputed cells in 1,101 Analyses, the other 299 left observed-only.
  The Observed-View Check reproduced every observed association it compared and
  found no added cell outside a region. A Release Erratum records the first
  completion of 2026-11-11 (10,001,552,498 imputed cells): the engine made any LD
  block holding at least four observations a region, and the genome-wide QC panel
  put about six in almost every block, so it filled about 885 of the 1,348 blocks
  for every Analysis. Its Store is archived, not deleted.
