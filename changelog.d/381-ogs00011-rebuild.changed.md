- **OGS-00011 is rebuilt with opengwasdb's MAF admission fix
  (issue #381, epic #371).** The rebuild runs at opengwasdb `70340e0`, which
  `v0.3.0` contains. Monomorphic and unreadable frequency rows are now dropped
  under the Store's common-variant filter instead of being admitted, so the
  off-panel axis falls from 101,098,177 to 34,653,805 variants, every one
  observed. The Store holds 48,069,741 variants and validates with warnings.
  Disputed rsids are dropped (`on-rsid-conflict: drop`, opengwasdb#306). The
  reference completion the bundle declares (#334) has not run on this build, so
  `STORES.md` shows it as completion pending.
