- **Manifest Generation follows opengwasdb#367's contract changes (issue #563).**
  The `complete` options it writes into `build.yaml` now carry `ld-panel-id`, the
  completion's Reference Resource id, so a completion records the panel it read
  rather than a digest-derived id (opengwasdb#327); a release config cannot set it.
  The bundle's `analyses.tsv` gains `p_value_test` (`wald_1df`, `joint_2df` or
  `mr_mega`, from `release_policy.p_value_tests`), which the derived build manifest
  carries so every build's effect-consistency check reads it (opengwasdb#510, ADR
  0085). An Analysis whose Resolution Record is a refusal by that check is excluded
  with the new reason `effect_consistency_refused`, not `resolution_failed`. The
  docs list the five `effect_consistency*` Store columns, the new Admission reasons
  and the new `validate` rules, and the repository builds against
  `opengwasdb@orch/367-silent-errors` until PR opengwas/opengwasdb#515 merges.
