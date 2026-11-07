- **OGS-00006 (Chen 2023, European) declares Reference Completion under its own
  id, against the UK Biobank EUR panel (issue #224, ADR 0037).** The Store is
  already published as Observed-Only, and its accepted Release Bundle gains the
  completion declaration, the one amendment ADR 0037's Decision 1 allows:
  `build.yaml` becomes `completion_state: reference_completed` and gains a
  `complete` block running `complete-ragged` against the panel root
  `/data/opengwasdb/reference/ld` for ancestry EUR (`ld/EUR` is `ukb`) at 64
  workers, and the build phase keeps exactly the argv that wrote the Store. The
  Store codes SE as `int8_residual`, so the declaration waited for opengwasdb
  `v0.6.2`, whose `complete-ragged` keeps the source's SE model and passes the
  strict Observed-View Check (opengwas/opengwasdb#301), and for `v0.7.0`, whose
  `complete-ragged` takes `--n-workers`: in one process its 1,348 LD blocks
  would take about five minutes each. The master list shows
  the row as an Observed-Only Store whose completion is pending until that run
  registers.
