# Fixtures for the clumps pipeline smoke test

- `test-panel/` is the engine's **Test Panel** (a real slice of HGDP+1kGP v2 EUR: 1,061
  variants on chr21 and chr22, 771 samples; its own README has the source and terms),
  copied unchanged from `opengwasdb`'s `tests/ld/data/test-panel/` at commit `c8ebe4ca`
  (`panel.pgen`, `panel.pvar`, `panel.psam`, `panel.keep`, `pcs.eigenvec`). The test builds
  its LD Store from it with `opengwasdb ld build` (about 2 s), so no built LD Store is
  committed. To refresh: copy those five files from the engine at the pinned commit.
- `rows.tsv` is its 911 SNV rows with the panel's simulated GWAS z (`gwas/z.tsv`, same
  commit), as `chrom pos ref alt z`. Regenerate by joining `panel.pvar` (SNV rows) to
  `gwas/z.tsv` by row order and dropping rows with no z.

No single-chromosome panel is committed: the Test Panel is one pgen over both chromosomes and
splitting it needs `plink2`, which this repository's environments do not carry.
