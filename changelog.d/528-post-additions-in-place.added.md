- **`pixi run release <ID>` adds the post steps a published Store's recipe
  declares and its records lack (`rho`, `clump`, a standalone `top-hits`) in
  place, with no rebuild and no `force` (#528, ADR 0043).** Its plan, which
  `pixi run release-dry <ID>` shows, is those steps, the declared `overview`
  regenerated after them, `validate` and the register. The published Store is
  staged by `cp --reflink=always`, never by a full copy: a filesystem that
  cannot reflink is refused, naming it, and so is a symlinked Store. The steps
  and `validate` run on the copy, which is published through the existing
  transaction, archiving the old Store under `replaced/<UTC>/`. The new
  Validation Record keeps the published one's lineage and adds `has_rho`,
  `has_clumps`, the engine's clump counts and a `post_additions` entry. It
  refuses, writing nothing, a 0.2.x Store, a bundle whose Store-writing steps
  or membership changed, and a killed run's leftovers. A rewritten Store is
  held to ADR 0042's deviation rule again. Any failure before publication
  leaves the published Store, `records/` and `validation.yaml` as they were;
  an interruption says whether it was rolled back, the publication had begun
  (the next run finishes it, clearing Snakemake's metadata before the marker)
  or it had finished. Afterwards a second run, `pixi run release all` and a
  direct `snakemake` find nothing to do, while a newer build manifest and
  `force=1` schedule what they always did, and a changed Store-writing step or
  a failed register record is refused by name, never reported up to date. A
  release still to complete its own Store is completed first. This is stream D's route
  for adding Rho and Clumps to the priority Stores (#520).
- `pixi run publish-rewrite` and the in-place additions share one publication
  lifecycle and record envelope, `ogstores.staged_publication` (#528). An
  interrupted rewrite's rollback now also ignores a repeated Ctrl-C or SIGTERM.
