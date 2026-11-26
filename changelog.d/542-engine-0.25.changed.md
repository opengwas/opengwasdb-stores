- **The engine moves to opengwasdb 0.25.1** (`1a116541`). The Pan-UKB import
  recipe can now re-plan a recorded import with `ld-stores rebuild … --plan-only`
  (opengwasdb#502), which the previous engine refused by name. A Pan-UKB import
  now drops frequencies that round to 0 or 1 in the store's float32
  (opengwasdb#505, the port of ogld#102 the engine's move from ogld had missed).
