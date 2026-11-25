- **OGS-00001 completes its own Store, OGS-00002 is superseded by it, and OGS-00003 plans a Rho Matrix (#514).**
  OGS-00001's Build Recipe gains `completion_state: reference_completed` and a `complete-ragged` block
  (HGDP+1kGP hg38 panel, ancestry EUR, copied from OGS-00002), so one id builds and completes its Store
  (ADR 0037); its build phase plans the argv its published Store recorded, with unchanged membership.
  OGS-00002 moves to `superseded` with `superseded_by: OGS-00001` and stays registered, since an id is never
  reused (ADR 0022); the catalog excludes it as a Trial release (ADR 0039). OGS-00003 declares
  `post.rho: true`. Each amendment is a dated note in the bundle's `release.yaml`.
