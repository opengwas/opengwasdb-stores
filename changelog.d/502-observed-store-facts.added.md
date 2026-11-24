- **The Validation Record records the published Store's size, whether it holds
  the HM3 Indexed Variant Subset and a Rho Matrix, and its top-hit counts
  (issue #502, epic og-dev-hub#14; `docs/release-metadata-schema.md`,
  "validation.yaml").** `register` writes `observed.store_bytes` from a stat
  of the Store it publishes, the same measure as the Store audit's, which is
  not a Store read under ADR 0023. `has_hm3` and `has_rho` are `true` when the
  Build Recipe planned the step and it succeeded. `n_top_hits` is `null` until
  opengwas/opengwasdb#488 reports the counts, which will also complete
  `n_variants` and `n_associations`. `stores.tsv` and the Analysis Catalog's
  `stores` table carry the new fields. `pixi run -e dev backfill-observed`
  writes them into the records registered before this change, touching no
  other key; it runs once, after #498. Its dry run over the registry today
  would fill 18 records and skip 6.
