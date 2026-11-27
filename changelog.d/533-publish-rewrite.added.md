- **`pixi run publish-rewrite` publishes a Store Release that `opengwasdb
  rewrite-release` staged beside the published one (#533, ADR 0043).** It
  records the operator's run of the rewrite as the `rewrite-release` step: the
  command, the engine's commit, start, end, exit, peak RSS and the log, from the
  run's `.cmd`, `.log`, `.time`, `.start`, `.end` and `.exit` files. It then runs
  the recipe's `validate` step on the staged release and publishes it through
  the existing transaction, archiving the 0.2.x release under `replaced/<UTC>/`.
  It refuses a release that is not 0.3.0, one with a validation error, and any
  warning that is not a deviation a `--convert-legacy` conversion records. It
  also refuses a published Store or engine install changed after the rewrite
  started, a validating runtime that is not the locked engine, and an
  unfinished completion or a killed run's leftovers, naming the remedy. Until
  the publication marker is written, any failure leaves the published Store,
  `records/` and `validation.yaml` as they were. A second run does nothing, and an interrupted one is finished by the
  publication marker. The Validation Record's new `rewrite` block names the
  source release and the deviations. This is the route for OGS-00011 (#499)
  and the converted Dense Stores (#525).
