- **`pixi run audit-store` audits a built Hybrid Store Release read-only, with
  three commands (issues #372, #373, #374, epic #371; `docs/spec/store-audit.md`).**
  `reconcile` checks the shared variant axis, its Dense Component and the
  off-panel remainder against what the Store records. `attribute` attributes the
  off-panel union to the Analyses and publications that observe it. `trace`
  follows a seeded sample back to the raw source rows and replays the builder's
  admission rule on them. On OGS-00011 it found every one of the 101,098,177
  off-panel variants observed, with no duplicate ALIDs, and 83% of them coming
  from five publications. It traced the cause to a hole in
  `admit_rows` (opengwasdb#331): placeholder `0.0`/`1.0` and unreadable
  frequencies were kept as `maf_missing` under a `FILTERED` policy, rather than
  judged and dropped.
