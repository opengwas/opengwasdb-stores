- **The yardstick measures LD: one LD Store and one Genotype Store, in the same run
  and the same result file (issue #504, og-dev-hub#14).** Workload `yardstick-6`
  adds an `ld` section naming OGL-00006 (the EUR LD Store with Partner Rows) and
  OGL-00007 (the EUR Genotype Store) by registry id; each Store's location and
  `far_bp` come from its LD Store Registry record. Through `ogquery.ld.open` it
  times `open`, `matrix` of 10 and 500 variants, `region` 1 and 3 Mb wide,
  `proxies` of 1, 10 and 100 variants and `clump` of opengwasdb#403's 37
  Analyses, first read and warm, with peak RSS and bytes on disk: every file a
  query reads, links followed (a Genotype Store's include its pgen files). The clump
  input is pinned by checksum and checked against its manifest, so a substitute
  is refused. The result gains an optional `ld` key recording what it read and
  the ogquery version; schema 1 and the GWAS section are unchanged, so every
  earlier result stays valid. The engine lock moves to opengwasdb 0.27.0 and
  ogquery 0.5.0, which reads Genotype Stores.
