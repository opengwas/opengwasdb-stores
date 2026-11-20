- **Each Analysis's p-value test, and per-Analysis MAF floors (opengwasdb#371).**
  The resolver manifest now carries `p_value_test`, which the engine requires
  from the release that adds ADR 0085. Two reviewed tables feed it:
  - `release_policy.p_value_tests`. An unlisted Analysis is `wald_1df`, unless
    its label names a 2-df test or MR-MEGA, which is refused until listed.
  - `release_policy.maf_threshold_overrides`.

  The GWAS Catalog EUR Hybrid release lists the 8 MR-MEGA Analyses (the
  resolver refuses them; owner ruling 20 Nov 2026: drop), its 35 joint 2-df
  tests, and 6 labelled "(2df)" whose `p_value` is the 1-df p. Five Analyses
  whose low-frequency rows contradict their own p-value get floors of
  0.025–0.1.
