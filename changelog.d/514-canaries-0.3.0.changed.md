- **OGS-00007, OGS-00001 and OGS-00003 are rebuilt in format 0.3.0 (#514).**
  Forced releases on the owner's approval, at opengwasdb 0.17.0. Each Store
  validates, reads without pysam, and has its 0.2.x Store archived under its
  `replaced/`.
  - OGS-00007 (39 s) holds 159,816 variants and 189,448 associations, down
    from 171,006 and 202,803. The engine now refuses unharmonised GWAS-SSF
    indels (opengwasdb ADR 0079 §7, `indel_ssf_unharmonised`), which removed
    13,355 rows on 11,190 indel variants.
  - OGS-00001 (249 s) builds and completes its own Store. The Observed-Only
    Store has 86,376 variants, each carrying its hg19 `source_alid`. The
    completed Store has 118,103 variants (31,727 imputed, none missing), and
    the Observed-View Check passed. It holds fewer variants than OGS-00002's
    completion (123,895) because of ADR 0079's reference-first indels.
    OGS-00002's Store is deleted, and its records are archived.
  - OGS-00003 (1,244 s) gains a Rho Matrix and an `hm3` subset (1,195,570
    variants), with its 21,327,043 variants unchanged. The Store is 2.82 GB.
