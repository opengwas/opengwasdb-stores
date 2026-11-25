- **The LD Store Registry records the other six LD Stores (#538, epic #496):**
  HGDP+1kGP AFR, AMR, EAS and SAS (OGL-00002 to OGL-00005, `retiring`), the
  EUR Partner Rows overlay (OGL-00006, `reference`) and Pan-UKB EUR
  (OGL-00012, `built`), each written by `python3 -m ld_registry write` and
  shown in `ld-resources.tsv`. The writer now records every link in a Store
  and its resolved target (`measured.links`), refuses a dangling link and a
  rewrite whose links changed; the index fails when a link's target
  lies under no record, or under a `retired` one, unless the record lists it
  in `unrecorded_targets` with its reason. An imported matrix's description is
  recorded as its documented fields and rendered into `source`, never as raw
  JSON, and its licence must be the record's. Every record keeps the engine's
  `variants_dropped`.
