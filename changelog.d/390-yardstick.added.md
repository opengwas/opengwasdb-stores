- **The yardstick is one fixed benchmark, run at the end of every epic, so that a
  performance trend compares like with like (issue #390, epic #385).** `pixi run
  yardstick` times fixed reads on published Stores — open, PheWAS by rsid and by
  ALID, position and region queries, a full analysis, the `hm3` subset, bytes on
  disk and peak RSS — read-only and in a fresh process per Store, and writes a
  versioned JSON result under `docs/_yardstick/`. A measure it cannot take is
  recorded `unavailable` with its error, never as a zero.
