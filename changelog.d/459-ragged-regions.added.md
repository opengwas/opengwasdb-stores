- **A Ragged Store keeps the regions its Release Bundle retains (issue #459).**
  A bundle that carries `sidecars/sparse_regions.tsv` now has that table passed to
  `build-ragged-ssf`, which validates it and keeps it in the Store as
  `regions.tsv`; completion and the Observed-View Check read it from the Store and
  take no flag of their own. A bundle without the table, such as the eQTLGen
  cis-only releases, plans and builds exactly as before, and a published Store
  gains `regions.tsv` only when it is rebuilt. A build that needs the table is
  refused before anything is written on an opengwasdb whose `build-ragged-ssf`
  takes no `--regions`, naming the Store and opengwas/opengwasdb#430.
