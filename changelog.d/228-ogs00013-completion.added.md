- **OGS-00013 (MVP AFR) is Reference-Completed under its own id, against
  the HGDP+1kGP AFR panel (issue #228, ADR 0037).** The Store is already
  published as Observed-Only, and its accepted Release Bundle gains the
  completion declaration, the one amendment ADR 0037's Decision 1 allows:
  `build.yaml` becomes `completion_state: reference_completed`
  and gains a `complete` block running `complete-dense` against
  `hgdp1kgp-hg38-ld` at `/data/opengwasdb/reference/hgdp1kgp-hg38/panel` for
  ancestry AFR at 32 workers, and the build phase keeps exactly the argv that
  wrote the Store. All 1,368 Analyses carry Assigned Ancestry AFR, so completion
  imputes every one. The declaration was held from 2026-10-22 while #348 found
  variant identities duplicated within the panel's blocks; the panels were
  repaired in place on 2026-11-01/02, and the released AFR receipt is
  `repairs/20261101T225229Z-348-AFR.json`. The completion ran and registered on
  2026-11-03, so the Store is validated and Observed-View-Checked, and the master
  list shows it as `reference_completed`.
