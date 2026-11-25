- **The Validation Record records each Store's format version
  (opengwas/opengwasdb-stores#512).** `register` fills `observed.format_version`
  from `opengwasdb info --format json` on the published Store, through
  `ogstores.store_info`, when no step reported it; a failed `info` leaves `null`
  and records why in the register record. `backfill-observed` fills it for every
  built or validated Store (0.2.0, 0.2.1 and 0.2.2 across the 18 on disk), and
  the committed records and `stores.tsv` carry the values.
