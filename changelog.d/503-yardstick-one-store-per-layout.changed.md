- **The yardstick measures one Store per GWAS Store Layout, across every query type and shape (workload `yardstick-4`).**
  OGS-00009 (Dense), OGS-00011 (Hybrid) and OGS-00027 (Ragged) replace OGS-00006.
  No served Store holds a Rho Matrix, so the rho measures are `not_applicable`
  until one does. New measures: the metadata queries (`analyses_table`,
  `variants_table`, `resolve`), a 100 kb region, five `lookup` shapes over 100
  fixed, scattered variants and Analyses, the full Analysis with
  `observed_only=True`, `top_hits` (recording whether the index or a scan answered), the Ragged cis query, and `rho`, `rho_row`
  and `rho_matrix`. A query a layout refuses is now recorded `not_applicable`,
  not `unavailable`. `tools/yardstick_inputs.py` picks the inputs read-only
  through the facade, and the workload records the rows each returned. The run
  budget is 1800 s (was 600 s) and the cadence is the finish of every epic
  that changes opengwasdb, or ogquery once it exists (opengwas/opengwasdb#487,
  #506). The result schema
  only gains keys. `CONTEXT.md` defines **Yardstick**.
