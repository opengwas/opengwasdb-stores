# A rewritten Store Release is published through the registry's publication transaction

`opengwasdb rewrite-release` (opengwasdb ADR 0105) derives a new format-0.3.0
Store Release from a published 0.2.x one. It stages the new release beside the
published one, `<id>/store.opengwasdb.0.3.0`, and leaves the published release
as it was. With `--convert-legacy` (opengwas/opengwasdb#492) it converts a 0.2.0
to 0.2.2 Dense release. What a conversion cannot fix it records, by validator
rule name, in the manifest's `provenance.converted_from`, and `validate` reports
exactly those rules as warnings. The registry had no way to publish such a
release: `pixi run release` plans only build, complete and post steps, and
`register` reads the records of a run it made itself (#533).

The owner decided that these releases are published under the registry's rules,
each replacing its 0.2.x release:

- **OGS-00011, 24 Nov 2026.** Its completed 0.2.3 release is rewritten to 0.3.0
  and published, rather than rebuilt a second time
  ([#499](https://github.com/opengwas/opengwasdb-stores/issues/499), opened by
  Gibran Hemani, `explodecomputer`).
- **The seven 0.2.0 Dense Stores, 25 Nov 2026.** OGS-00009 and OGS-00012 to
  OGS-00017 are converted fast, with their deviations recorded, and rebuilt
  from source later ("Owner decisions, 25 Nov",
  [#498](https://github.com/opengwas/opengwasdb-stores/issues/498#issuecomment-7159055204)).
  A converted release is a Staged Release (ADR 0038), published under the
  registry's rules as #499's is. It validates with only its recorded deviations
  as warnings, and anything else in the validator's output is a real failure
  ([#520](https://github.com/opengwas/opengwasdb-stores/issues/520#issuecomment-7163972259),
  25 Nov 2026). The owner approved publishing each converted Store on that
  condition in #520's run on 25 Nov 2026.

## Decision

**One registry entry point publishes a rewritten release that is already on
disk:** `pixi run publish-rewrite <ID> --staged <path> --evidence <prefix>`
(`ogstores.publish_rewrite`).

**It adopts the operator's run rather than running the rewrite.** The rewrite
runs for hours, outside the registry, under `/usr/bin/time -v`. Its evidence is
six files, `<prefix>.cmd`, `.log`, `.time`, `.start`, `.end` and `.exit`. The
entry point records them as the step `rewrite-release`
(`records/rewrite-release.json`): the command, its engine's commit and version
(read from that executable's own install), start, end, exit, peak RSS and the
log. Evidence is read exactly, never repaired. The entry point refuses:

- evidence that does not show this Store's published release rewritten to this
  staged path and exiting 0;
- a timed command that is not the recorded one;
- a staged release whose `store_id`, as `opengwasdb info` reports it, is not the
  bundle's;
- an absent identity, which is never compared as equal.

**A path is not a release.** The published Store directory and its register
record must not have changed since the evidence's `.start`. Otherwise the
release the rewrite read may not be the one it would replace. `info` names no
engine that wrote a release, so the engine's commit comes from the executable's
install. That install's `direct_url.json` must predate `.start`: a reinstall
since would put another engine's commit in the record.

**The validator is the locked engine.** The `validate` that judges the
rewrite runs under `runtime.verify_runtime_for_releases`, as `pixi run
release` does: its executable must be `pixi.lock`'s commit and provide
`validate`. This is checked separately from the rewrite's own engine, which is
history.

**The verdict is a recorded `validate` step.** The rewrite validates its
release before it stages it, but it prints only errors and keeps no verdict. So
the entry point moves the staged release to `store.opengwasdb.partial` and runs
the Build Recipe's planned `validate` step on it (`run.execute_step`), with the
worker count the recipe declares. This validates every rewritten release twice.
`validate` took 172 s for OGS-00015 at 16 workers, and the large Stores are 10
to 20 times bigger. If the engine printed its verdict, the second run would go.

**The deviation rule.** A validation error refuses publication, and so does any
warning that is not a deviation the conversion records. A warning is accepted
only from a `--convert-legacy` rewrite, and only under one of the rules a
conversion records: `variant_table.indel_order`, `variant_table.source_alid` and
`side_tables.se_exceptions_rounded`. A 0.2.3 rewrite (#499) records none, so any
warning refuses it. The registry does not read `provenance.converted_from`.
ADR 0023 lets it look at a Store only through `opengwasdb`'s command line, and
no command prints the block. It does not need to: on a 0.3.0 release each of
those rules is an error unless the release records it, and the validator
already fails a record that lists a rule without the evidence for it (opengwasdb
`Rule.finding`, `manifest.converted_from`). So a warning under one of those
rules is the record's. The list is a copy of the engine's
`legacy_conversion.DEVIATION_RULES`. A rule added there is refused here until it
is listed, which is the safe direction for a copy to drift.

**Publication is the existing transaction.** Before anything is moved,
`records/` is copied to a forced run's snapshot. The copy is
`run.snapshot_records`, the snapshot-only step `run.prepare_release_run` now
calls too. `prepare_release_run` also discards an interrupted completion's
checkpoint and `work/complete.started.json`, which a rewrite must not do. So an
unfinished completion, a leftover `.partial` or `.backup`, and a leftover
snapshot are all refused before anything is written, each naming its remedy.
The snapshot is named first, so a `.partial` left beside it cannot hide it.
Once everything is verified, the entry point writes `publication.json` through
`register.begin_publication` and hands it to `register.complete_publication`.
The old Store, its records and its Validation Record go under
`replaced/<UTC>/`, and crash recovery is the marker's, as for `pixi run release
--config force=1`. No other swap or archive code exists.

**Until the marker exists, everything is undone.** Any failure before the
marker is written puts `records/` back from the snapshot and the staged release
back at its path: a refusal, an error assembling the record, a failed write,
Ctrl-C or SIGTERM. The published Store and `validation.yaml` are never touched
before the marker. A refused validation's record is kept in `work/`, because a
re-run would cost hours. It is copied byte for byte after the staged release is moved back and before `records/` is restored, and no failure to keep it stops either. After
the marker, nothing is undone: the next call finishes it.

The Validation Record, register record and marker come from `register`'s
builders: `build_validation_record`, `build_register_record`,
`build_publication_marker` and `engines_of`. `register_release` uses the same
builders. The rewrite's step record is a `run.StepResult` with the evidence's
fields added.

**What it records.** The register record is the publishing phase's:
`completion-register` for a release that completed its own Store (ADR 0037),
`register` otherwise. Its `inputs` keep the replaced release's and add the
rewrite, so the amendment check still finds the steps that wrote the data. The
Validation Record:

- reads `status`, `checks.store`, `warnings` and `errors` from this run's
  `validate --format json`, as `register`'s do;
- carries `observed` from the replaced record, because the rewrite answers every
  question its source does (the engine's digest proof refuses it otherwise).
  `format_version` (from `opengwasdb info`), `store_bytes` (a stat) and
  `validate_status` are measured again. `build_elapsed_s` is the schema's sum of
  step times: the replaced release's, plus the rewrite's and this validation's.
  It is `null` when the replaced one is. A conversion masks every association
  whose SE is above 65,504, so the counts the build reported no longer describe
  it: `n_associations` and `n_top_hits` are `null`;
- passes on the `acceptance` block;
- adds a `rewrite` block: the command, the new and the source `release_id` and
  `format_version`, whether it was a conversion, and the deviations the
  validator reported.

It leaves the Release Status as it is, as `register` does.

## Consequences

- #499 and #525 publish through one command, under the release's run lock, and
  are refused for a reason the operator can read.
- A second call after a publication does nothing. A call after a crash finishes
  the marker first.
- A killed run (SIGKILL, or the host going down) before the marker leaves the
  snapshot, and the `.partial` release once it was staged. The next call
  refuses and names the remedy: `pixi run release <ID> --resolve-snapshot
  restore`, then, if it was staged, moving the `.partial` release back to its
  staged path. Ctrl-C and SIGTERM unwind without leaving either.
- A converted release's native warnings refuse it. On the engine's own 0.2.0
  Dense fixtures `eaf_orientation.unverified` is one. If a hosted Store raises
  one, its publication goes back to the owner, as #520 says.
- Each conversion is validated twice until the engine prints the rewrite's
  verdict.
- **What remains unproven, and the engine follow-up that closes it.**
  `opengwasdb info --format json` prints neither `provenance.converted_from`
  nor `provenance.format_rewrite`. So the registry cannot check that the staged
  release names the published `release_id` as its source, nor which engine
  wrote it. The mtime checks above stand in for both.
  - The deviations recorded are the ones that fired as warnings. A deviation
    the conversion records but that finds nothing is not listed:
    `variant_table.indel_order` on a release with no sorted indel.
  - The follow-up is for opengwasdb's `info --format json` to print the
    rewrite's source `release_id`, its engine commit and its `deviations`.
    The registry would then compare them instead of trusting mtimes.
- OGS-00011's rewrite (#499) carries a native `eaf_orientation.unverified`
  warning, which the rewrite preserves, so this route refuses it. Whether to
  publish it is the owner's call when #499 reaches publication.
