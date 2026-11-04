- **OGS-00015 (MVP EAS) is Reference-Completed under its own id, against
  the HGDP+1kGP EAS panel (issue #230, ADR 0037).** The Store is already
  published as Observed-Only, and its accepted Release Bundle gains the
  completion declaration, the one amendment ADR 0037's Decision 1 allows:
  `build.yaml` becomes `completion_state: reference_completed`
  and gains a `complete` block running `complete-dense` against
  `hgdp1kgp-hg38-ld` at `/data/opengwasdb/reference/hgdp1kgp-hg38/panel` for
  ancestry EAS at 16 workers, and the build phase keeps exactly the argv that
  wrote the Store. All 241 Analyses carry Assigned Ancestry EAS, so completion
  imputes every one. The `hgdp1kgp-hg38-ld` declaration now records that the AFR
  and EAS panels ship, and the dbGaP MVP generator's README describes where all
  five Stores stand. The declaration was held from 2026-10-22 while #348 found
  variant identities duplicated within the panel's blocks; the panels were
  repaired in place on 2026-11-01/02, and the released EAS receipt is
  `repairs/20261101T225229Z-348-EAS.json`. The completion ran and registered on
  2026-11-02, and the Store is validated and Observed-View-Checked.
