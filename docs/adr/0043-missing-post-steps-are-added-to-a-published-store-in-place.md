# Missing post steps are added to a published Store in place, with no rebuild

A Build Recipe that gains `post.rho` or `post.clumps`, or a standalone
`post.top_hits`, after its Store was published declares contents the published
Store does not have. `pixi run release <ID>` had no way to add them. With
`--config force=1` it rebuilds the Store, which for the large Dense Stores
(50-130 GB) costs hours. Without it, Snakemake found nothing to do: the Store's
`register` and `validate` records exist, so it never reached the missing `rho`
and `clump` records (#528). ADR 0037, as clarified by #305, already says these
steps are re-runnable in-place additions (opengwasdb ADR 0061). Adding one
changes no Store-writing step and no membership, so it is no rebuild.

The owner decided that Clumps are added this way:

- **opengwas/opengwasdb#420, 25 Nov 2026**
  ([comment 7148370637](https://github.com/opengwas/opengwasdb/issues/420#issuecomment-7148370637),
  Gibran Hemani, `explodecomputer`): every registered Store is rebuilt or
  converted at format 0.3.0 first (#498). Clumps and `n_loci_5e8` are then
  added in place to those 0.3.0 Stores. The run itself needs the owner's
  approval of command, host and budget.
- **Stream D of [#520](https://github.com/opengwas/opengwasdb-stores/issues/520)**
  (owner, 25 Nov 2026): every priority Store gets Top Hits, Rho (Dense and
  Hybrid) and Clumps, through #522 item 3, this decision.

One further ruling is the orchestrator's, not the owner's. It was made in
#528's review, in #520's orchestrated run, on 25 Nov 2026: **the declared
overview is regenerated.** When `overview` is declared and any step is added,
the plan runs it after the added steps.

## Decision

**One `pixi run release <ID>` adds them.** There is no new command and no new
option. The entry point (`ogstores.release`) asks `ogstores.post_additions`
about each release it is named, unforced, before Snakemake runs.

The evidence for a step is **current** when its record succeeded. For an
addable step (`top-hits`, `rho`, `clump`) or `validate`, it must also have been
planned as the recipe plans it now, at the root it was written under. A
`clump` record from another LD Store mapping is not current.

A release is this mode's when its last phase has published and its Store path
exists. That phase is the build phase's `records/register.json`, or the
completion phase's `records/completion-register.json` for a release that
completed its own Store. The phase must also:

- lack current evidence for at least one addable step or for `overview`;
- or, once this mode has published the release, lack current evidence for any
  of those or `validate`;
- and lack nothing else.

Its plan, in plan order, is:

1. the addable steps that lack current evidence (`completion-rho`,
   `completion-clump` in a completion phase);
2. the declared `overview` (`completion-overview`), when anything is added or
   its record is missing. The engine's overview reports the Rho Matrix and
   the Clumps;
3. the phase's `validate`, then its register.

`pixi run release-dry <ID>` prints that plan and writes nothing. A later step
with current evidence, such as an Indexed Variant Subset, is not rerun.
Snakemake is not run for the release.

**Top Hits are added only where a standalone step builds them.** A Dense build
and every completion build the Top-Hit Index inline, so they plan no
`top-hits` step, and none is ever added there. A Ragged build phase's
`top-hits` is added as `rho` is.

`all` names no release for this mode. Adding Rho to a 130 GB Store is named,
never implied. `all` and a direct `snakemake` run stay correct after an
addition (below).

**The Store-writing steps stay held to their records.** Before anything is
written, the bundle must still plan the Store it published. The baseline is
the one ADR 0037 uses:

- the recorded `planned_argv` of each published phase's `variant-reference`,
  `build` and `complete`, with #341's and #428's exceptions;
- the build phase's membership.

`amendment.published_store_changes` is the one walk of the published phases.
`amendment.bundle_changes` holds a bundle that completes its own Store to it,
and this mode holds every release to it. A change is refused, named, and
points to `--config force=1`. #527 adds the rewrite identity check,
`_rewrite_changes`, to that walk: a published `rewrite-release` record must
exist and name this Store.

**Only a 0.3.0 Store.** A published Store whose `opengwasdb info` format is not
0.3.0 is refused before staging. Clumps exist only from 0.3.0 (opengwasdb ADR
0106), and a 0.2.x Store is rewritten first (ADR 0042).

**Staged by reflink, never by copy, never through a symlink.** `run`'s
no-follow guard runs first. A release directory, Store, staged Store or
`.backup` that is a symlink is refused before anything is copied. The Store
is then copied to `store.opengwasdb.partial` with `cp -a --reflink=always`, so
the copy shares every block. Before the copy, a probe file is reflinked beside
the Store. A filesystem that cannot do that is refused, naming the filesystem
(`stat -f`), and there is no fallback to a full copy. The production root,
`/data/opengwasdb`, is XFS with reflink.

**The steps run on the copy.** Each runs through `run.execute_step`, which
stages its argv onto `.partial`, and its record is held to its planned argv
(`register.check_argv_drift`). The validation is the phase's planned
`validate`, with its `--ld-store` pairs, so `clumps.reclump` runs. A failed
step or a validation error is refused, naming its errors.

A Store that `publish_rewrite` published carries a `rewrite` block. It is held
to ADR 0042's deviation rule again (`publish_rewrite.judge_findings`): a
`--convert-legacy` release may warn only under a deviation its conversion
records, and a 0.2.3 rewrite may not warn. A native warning refuses it.

**One publication lifecycle, shared with `publish_rewrite`.**
`ogstores.staged_publication` owns it, and both modes use it:

1. copy `records/` to a forced run's snapshot (`run.snapshot_records`);
2. stage the candidate at `.partial`;
3. run the mode's work;
4. write `publication.json` (`register.begin_publication`);
5. finish it (`register.complete_publication`).

The old Store, its records and its Validation Record go under
`replaced/<UTC>/`. Each mode keeps its own staging (a rename for a rewrite, a
reflink here) and its verdict policy. The record envelope is shared too
(`staged_publication.republication`): the new Validation Record, the phase's
register record and the marker, from `register`'s builders.

**Until the marker exists, everything is undone.** Any failure before it (a
refusal, a failed step, a validation error, Ctrl-C, SIGTERM) is rolled back:

1. the staging is undone: the staged copy is removed, and a symlink is
   unlinked, never followed;
2. the failed step's record is copied to
   `work/post-additions-refused-<UTC>-<step>.json`;
3. `records/` is restored from the snapshot.

The snapshot's path is known before it is copied, so a snapshot an
interruption left just after its rename is restored too. Ctrl-C and SIGTERM
are ignored while the rollback runs, as `release._run` shields settlement. An
interruption reports the state it left:

- before the marker, it was rolled back;
- from the marker until the marker is removed, the publication is pending,
  and the next `pixi run release <ID>` finishes it;
- once the marker is removed, it is published.

The entry point exits 130 in every case.

**Clearing Snakemake's metadata is part of the transaction.** The entry point
finishes the publication as it finishes every publication
(`release.finish_publication`): `complete_publication`, then Snakemake's
metadata for every record the addition's register lists, in one
`--cleanup-metadata` call, then the marker, last. A step now follows an added
one, so its recorded inputs changed, and Snakemake's `input` trigger would
otherwise reschedule it. Recovery runs the same function, so an interruption
anywhere in it, part-way through the cleanup included, is finished by the next
run, cleanup and all.

**The Validation Record keeps the release's lineage.** It is the published
record, with only what this run measured changed:

- `status`, `checks.store`, `warnings` and `errors` are this `validate`'s. A
  resolver-engine mismatch is a warning, as `register` adds one;
- `build_environment` still names the engine that wrote the Store. Its
  `engines` list drops the steps this run reran, then adds them;
- `observed` is carried. `format_version` (`info`), `store_bytes` (a stat) and
  `validate_status` are measured again. `has_rho` is set when `rho` was added,
  and `has_clumps`, `n_loci_5e8` and `n_unclumped_5e8` when `clump` was.
  `build_elapsed_s` replaces the rerun steps' times with this run's;
- `acceptance` and every other block, including a rewrite's `rewrite`, are
  carried;
- a `post_additions` list gains one entry: the steps added, the records this
  run wrote, the `release_id` and the archive.

The register record is the phase's own. Its `inputs` keep the published
records, except those this run reran, then add this run's. It carries a
`rewrite` block, if there is one, and the same `post_additions` list.

**Its records decide; Snakemake's mtimes do not override them.** Every
unforced release a run requests is assessed, `all`'s included. Once a release
is this mode's (it lacks an addable step, or this mode published it,
`post_additions` in its register record), `assess` refuses it, naming why,
whenever its records no longer describe the Store its bundle plans: a
Store-writing argv or membership that changed, or a register record that did
not succeed. That holds whatever Snakemake would schedule, so drift is never
reported as up to date.

**The Snakefile settles only what an addition disturbs.**
`post_additions.current` holds when this mode published the release, every
step of its phase has current evidence, and the bundle still plans its Store.
For such a release the Snakefile makes `ancient` only the edges between post
steps and into the register (`settled`): the predecessor record of a post
step whose predecessor is a post step, and the last step's record into the
register. An addition leaves a later step's record older than the added
record now before it, such as an Indexed Variant Subset, or the Observed-View
Check of a completed Store. Without this, `pixi run release all` and a direct
`snakemake` would reschedule it, and the entry point would refuse the run as a
rebuild.

The settling stops short of two things:

- A build's, a completion's and a manifest's inputs. A newer build manifest
  still schedules the build, which an unforced run refuses, as before.
- Any run that forces the release (`--forceall`, `--forcetargets`, a rule, or
  a `--forcerun` path under the release, which is how `--config force=1`
  reaches Snakemake). Such a run schedules the whole chain, as before.

A named release this mode published with nothing missing is handed to
Snakemake like any other, and the settled edges leave it nothing to do. A
release this mode never published is decided as before.

**How it composes with completing a Store under its own id.** Completion comes
first: completing rewrites the Store, so post steps added before it would be
lost. A release whose bundle completes its own Store and whose completion phase
has not published is not this mode's. That is the own-id completion of ADR
0037 (and #527 for a 0.3.0 parent), and the post steps the recipe declares run
in its completion phase. Once the completion phase has published, this mode
adds later post steps to that phase. The build phase's records then describe
the replaced Observed-Only Store, and its missing post steps are not added:
the Store they would describe is gone.

```architecture
adds:
  - paths: [src/ogstores/post_additions.py, src/ogstores/staged_publication.py]
    component: release_orchestration
```

## Considered options

- **A sibling entry point, `pixi run add-post-steps <ID>`.** Rejected. The
  issue asks for one `pixi run release <ID>`, and the release lock, recovery,
  snapshot refusal and runtime guard are the entry point's already.
- **Let Snakemake run the missing steps.** Rejected. Snakemake does not
  schedule a missing record whose consumers exist (the behaviour this decision
  replaces). Forced to, it would rerun every later step, including an Indexed
  Variant Subset, which `build-indexed-subset` refuses to rebuild without
  `--overwrite` (refused by ADR 0034).
- **Settle every published release in the Snakefile.** Rejected. A release
  this mode never published is decided by mtimes, as before, so nothing that
  refuses today is let through.
- **A full copy where reflink is unsupported.** Rejected, as above.
- **`register_release` over the old and new records.** Rejected. For a
  rewritten Store it would read the 0.2.x build's report as the Store's format
  and counts, and drop the `rewrite` block and the `rewrite-release` input.

## Consequences

- The 15 priority Stores (stream D) gain Rho and Clumps with one command each,
  after their recipes gain `post.rho`, `post.clumps` and `post.ld_stores`. Each
  run still needs the owner's approval of command, host and budget
  (opengwas/opengwasdb#420): it replaces a published Store.
- A killed run (SIGKILL, host crash) before the marker leaves the snapshot and
  the `.partial` copy. The next run refuses and names the remedy:
  `--resolve-snapshot restore`, then deleting the `.partial` copy.
- Each addition's archive under `replaced/<UTC>/` holds the previous Store,
  renamed, not copied. It shares the unchanged blocks with the published copy.
  It costs the blocks the added steps rewrote, and its own inodes and
  metadata, and nothing deletes it.

## Amendment, 26 Nov 2026: a completed Hybrid's additions begin with a recount

`complete-hybrid` left a Hybrid's `n_hits_5e8`, `n_hits_5e6` and `n_hits_5e4`
stale, and the engine now fails the release for it (`analyses.top_hit_counts`,
opengwasdb#509). The owner ruled (item 10,
<https://github.com/opengwas/opengwasdb-stores/issues/520#issuecomment-7202276131>)
that the recount runs inside this route: reflink copy, recount, then the
missing post steps, validate and publish, the old Store archived.

When this route adds anything to a completed Hybrid, its plan begins with
`opengwasdb recount-top-hits <Store>` (`recount-top-hits`, or
`completion-recount-top-hits` in a completion phase), before the addable steps
and the overview. It is not an addition and does not make a release this
mode's: like the overview, it accompanies additions, so the principle that this
route adds only missing steps stands. The trigger is every such run, not the
engine that wrote the Store, because the command is idempotent (it writes only
a table whose counts change) and takes seconds. `plan()` never holds the step:
a fresh or forced build's `complete-hybrid` recounts itself. A completed
Hybrid that needs only a recount, with nothing to add, is not taken by this
route; that is a follow-up if one turns up. The step's record, elapsed time
and the register's `inputs` are like any step this run ran.
