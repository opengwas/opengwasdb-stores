- **`ogstores.bundle.manifest_summary(bundle)` summarises a Release Manifest over
  its built rows (opengwas/og-dev-hub#16).** It returns the built and excluded
  row counts, the median sample size over the built rows that have one (with how
  many did, and how many values the quantity rule refuses, counted and never
  dropped), and the built rows per Assigned Ancestry with an empty value as
  `unassigned`. The Dev Hub's Stores view calls it rather than reading
  `analyses.tsv` itself.
