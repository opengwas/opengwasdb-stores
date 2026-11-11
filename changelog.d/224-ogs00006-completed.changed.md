- **OGS-00006 (Chen 2023, European) is Reference-Completed under its own id,
  against the UK Biobank EUR panel (issue #224, ADR 0037).** The completion
  phase ran on 2026-11-10/11 at opengwasdb `v0.7.4`, whose `complete-ragged`
  merges its checkpoints by streaming them (opengwas/opengwasdb#424), and
  registered: 10,001,552,498 imputed associations across all 1,400 Analyses,
  14,273,581 variants, and an Observed-View Check that reproduced every
  observed association it compared. The bundle carries the Validation Record
  `register` wrote, the release notes record the run, its counts and how to
  read this Store's completion quality, and the master list shows the row as
  `reference_completed`.
