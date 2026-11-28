- **A Build Recipe can declare `post.clumps: true` (#526, epic #520).** The
  release then plans `opengwasdb clump <store> --ld-store ANCESTRY=PATH …` after
  `top-hits` and `rho`, in the build phase and the completion phase, on every
  layout. The LD Store mapping is the recipe's explicit `post.ld_stores` and is
  never defaulted: a recipe that declares `post.clumps` without it is refused at
  planning and by the bundle check. The planned `validate` gets the same
  `--ld-store` pairs, so `clumps.reclump` runs. `register` records
  `observed.has_clumps` (null when the recipe plans no clump step) and the
  engine's own `n_loci_5e8` and `n_unclumped_5e8`, and `stores.tsv` has a
  `has_clumps` column.
