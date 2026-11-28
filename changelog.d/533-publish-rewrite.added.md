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
  source release, the deviations and the carried warnings. This is the route for OGS-00011 (#499)
  and the converted Dense Stores (#525).
- **`publish-rewrite` accepts two kinds of warning (#533).** A warning is a conversion's
  deviation only when its message carries the conversion's note, the text the engine's
  `Rule.finding` appends to a finding it lowers to a recorded deviation (`[a deviation recorded by
  ogdb rewrite-release --convert-legacy, which converted this release from format_version <v>
  (release <id>); a rebuild from source fixes it]`); a rule's finding that no deviation records can
  be a plain warning (`variant_table.lifted_contig`'s "N not judged" is one on every release),
  so the rule alone no longer proves it, and `lifted_contig` is a deviation rule (opengwasdb#504).
  A release converted before #504 therefore refuses its plain `lifted_contig` warning. The other
  kind is a warning the replaced Validation Record already carried unchanged: its message must
  equal one of the replaced record's `warnings` (once per recorded copy), and a new warning, or
  one with a different Analysis or count, is still refused, as is every error. The accepted
  warnings are named in `rewrite.carried_warnings`, beside `deviations`; this lets OGS-00011's
  rewrite (#499) publish with its native `eaf_orientation.unverified` warning (#520 comment
  7185997253, item 4). The post steps added to a rewritten release (#528's route) accept its
  `rewrite.carried_warnings` the same way. `publish-rewrite` also records the rewritten
  release's `observed.completion_state` from `opengwasdb info`, not from the replaced record
  (a 0.2.x record written before #307 carries none, which `index` refuses); a carried state
  that disagrees with the measured one is refused, naming both.
