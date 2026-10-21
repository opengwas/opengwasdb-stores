# Workflows

Two workflows, deliberately separate, meeting at the accepted Release Bundle.

| | | |
|---|---|---|
| `Snakefile` | Phase A | accepted bundle -> validated Store Release |
| `generate.smk` | Phase B | frozen Source Inventory -> candidate bundle (coarse wiring; the operator entry point is `pixi run generate-candidate`) |

Store Release Build is `Snakefile`, `release.py` and the `ogstores` package
(`src/ogstores`). Manifest Generation is the `manifest_generation` package
(`src/manifest_generation`), which `generate.smk` only wires. Store Release Build imports nothing from Manifest Generation; both
may import the Release Bundle contract (`tests/packaging/test_boundary.py`).

They share no DAG. The reason is not that one graph would be complex: the
accepted bundle is a boundary *only because a human froze it*. Span both
phases with one DAG and Snakemake will correctly, silently, regenerate a
bundle and rebuild a 71 GB Store because a generator config changed upstream,
and the acceptance gate stops existing.

## Phase A Operator Interface

Phase A is driven by `workflow/Snakefile`:

```sh
pixi run release OGS-00003                                 # one registered release, plus any parent it needs
pixi run release OGS-00003 OGS-00004                       # several registered releases; lineage order is resolved
pixi run release OGS-00003 --config artifact_root=/path    # ids first, then config overrides
pixi run release OGS-00005 --config force=1                # replace a published release, archiving the old one
pixi run index                                             # regenerate master list, summaries, by-label/
```

A release target must be an ID currently registered under `stores/`; use
`stores.tsv` to find valid IDs. IDs shown in commands are real targets, not
placeholders. Post-steps follow the selected command's Store-format support:
`rho` is Dense-only, and `overview` is Dense/Hybrid-only because Ragged's
closed Store envelope excludes `overview.html`.

`pixi run release` and `pixi run release-dry` run `workflow/release.py`, the
supported entry point (#195). Around one Snakemake run it:
- refuses a release whose Release Status does not allow a build (only
  `accepted`, `built` and `validated` are buildable; #247), before
  anything is written. `all` means every buildable release, and the entry
  point, `--dry-run` included, lists each one it leaves out and why;
- completes any publication a crash interrupted;
- refuses a published release unless it is named with `--config force=1`, and
  refuses a release holding a leftover records snapshot;
- snapshots what a forced run may replace;
- settles the run afterwards.

It accepts only release ids, `--config`, `--cores`, `--dry-run`, `--keep-going`,
`--rerun-incomplete` and `--resolve-snapshot restore|delete`. `--no-hooks`, `--touch` and other unsafe options are
refused before anything is written, and `--dry-run` writes nothing. An
up-to-date release is a no-op.

A forced run copies the release's `records/` to `records.before-force-<UTC>/`.
Publication is one transaction, recorded in `publication.json` until it is
complete. The old Store, those records and a copy of its `validation.yaml` move
to `replaced/<UTC>/`, and nothing deletes them; each archive holds a full Store.
The entry point removes that marker only once Snakemake has finished the
register job, so a crash after publication starts is always finished by the
next run. A crash before it leaves the old release in place, with a snapshot
that blocks the release until an operator runs `pixi run release <ID>
--resolve-snapshot restore` (or `delete`).

Two runs never touch one release at once. Each run holds an exclusive lock
per release (`<artifact-root>/.release-locks/<ID>.lock`) from recovery to
settlement, so a run of a release that is already running is refused with "a
run of `<ID>` is in progress (pid …)". Ctrl-C or SIGTERM stops Snakemake,
restores the records a forced run had begun to replace, and exits 130 or 143.

Running `snakemake --snakefile workflow/Snakefile` directly bypasses all of
this except the Snakefile's own `onstart` refusals, which `--no-hooks` disables.
They refuse an unbuildable bundle as the entry point does, and the
Snakefile's `all` target expands only to buildable bundles.
See the specification's Safety section.

### A reference-completed release is published twice under one id

A Dense release whose bundle declares `completion_state: reference_completed`,
a `build` block and a `complete` block, with no `derived_from`, completes its
own Store (ADR 0037). One `pixi run release <ID>` does both phases, with no
`--config force=1`:

1. **Build phase:** the same steps and records as an observed-only release
   (`build`, the declared post steps, `validate`, `register`). Its `register`
   publishes the Observed-Only Store at `<ID>/store.opengwasdb`, so it is
   queryable while completion runs.
2. **Completion phase:** `complete` reads that published Store and writes
   `<ID>/store.opengwasdb.partial`, the completion's post steps and
   validation run there, `completion-observed-view-check` confirms the staged
   Store keeps every observed association of the published one (a failing
   check publishes nothing and leaves the published Store as it was; read
   `<ID>/work/observed-view-check.json`), and `completion-register` replaces the Observed-Only
   Store with the completed one through the same publication transaction. The
   completed Store keeps the id as its Store identity and has its own release
   identity, `<ID>-completed`.

The completion phase's records are `complete`, `completion-rho`,
`completion-indexed-subset-<name>`, `completion-overview`,
`completion-validate`, `completion-observed-view-check` and
`completion-register`. They never overwrite the
build phase's, and both sets stay in `records/`. The completion phase takes
no records snapshot. Once the completed Store is published it deletes the
Observed-Only Store, which the completed Store contains, so the id keeps one
copy on disk. Its archive, `replaced/<UTC>/` (suffixed `-2`, `-3`, … when that
name is taken), holds no Store: a copy of the replaced release's
`records/register.json` and of the passing
`records/completion-observed-view-check.json` under `records/`, and the
replaced `validation.yaml`. The bundle's `validation.yaml` then describes the
completed Store and names the archive. A crash during the deletion is finished
by the next run. A forced run archives the Store it replaces in both phases.

A failed or interrupted completion leaves the Observed-Only Store published
and the build phase's records untouched. The next `pixi run release <ID>`,
with no `--config force=1`, resumes it from the missing completion-phase step
(#305). An interrupted `complete` is resumed from its checkpoint,
`<ID>/.store.opengwasdb.partial.checkpoint`, with
`opengwasdb complete-dense-resume`, so hours of imputation are not repeated.
The workflow records the planned argv a completion starts from, in
`<ID>/work/complete.started.json`, and resumes only when that is the argv
planned now. If the bundle's `complete` block has changed since, or the
checkpoint was not started by this workflow, the run refuses, naming why,
and keeps the checkpoint. Only `--config force=1` discards a checkpoint; it
then rebuilds both phases.

The preflight lets an unforced run through only when every job it schedules
for a published release is a completion-phase job, and the release's records
show its Observed-Only Store published and not yet completed
(`records/register.json` without `records/completion-register.json`). A run
that would rebuild any part of the build phase is refused as before.

**A Store published as Observed-Only can be completed later.** Its accepted
`observed_only` bundle may gain a `complete` block and switch to
`completion_state: reference_completed`. That is the only change an accepted
bundle allows (ADR 0037). Then `pixi run release <ID>` runs the completion
phase alone. For a bundle that completes its own Store, the entry point
refuses, before any job runs and naming what changed, a bundle that no longer
plans the Store it published:
- a different argv for a step that wrote the Store (`variant-reference`,
  `build`), or such a step added or dropped;
- a different membership (the derived build manifest, and the Analyses it
  excludes);
- once the completion phase has published, a different `complete`.

A post step is a re-runnable in-place addition, so a new post-step
declaration passes this check, and so does a post step the planner now plans
differently. A new declaration's job is still a build-phase job over a
published Store, though, so the #195 guard refuses it unless the run is
forced. Observed-only and two-id releases are not checked. A direct
`snakemake` run refuses a changed bundle only when it schedules a job
(`onstart`): one with nothing to schedule writes nothing, and has none of the
entry point's recovery or settlement either. `release.yaml`'s prose and
provenance are left to review. `--config force=1` still rebuilds a named
release from its bundle as it now stands.

### Production Execution vs. Fixture-Scale Tests

- **Workflow tests (`tests/workflow/`) are fixture-scale**: they exercise DAG wiring, command line composition, transaction staging (`.partial`), publication gating, and step resumption using temporary mock bundles and mock executables without requiring multi-gigabyte production data or reference panels.
- **Production builds require source and reference preflight**: before executing a real release pipeline, the operator must verify that all declared raw sources (e.g. GWAS-SSF, VCF, or BESD prefixes) and reference panels (e.g. LD panels, reference allele frequency tables) exist at their configured paths.
- **The all-seven command**:
  ```sh
  pixi run release OGS-00001 OGS-00002 OGS-00003 OGS-00004 OGS-00005 OGS-00006 OGS-00007
  ```
  **Must NOT be run until configured inputs exist.** Running this without the required source data and reference resources present will fail at preflight or step execution.

See `docs/spec/store-release-workflow.md`.

## Phase B Operator Interface

The candidate workflow (issue #153) is driven by the Manifest
Generation entry point, `python3 -m manifest_generation.generate_candidate`
(`src/manifest_generation/generate_candidate.py`), exposed as one Pixi task. It
runs any candidate-workflow config:

```sh
pixi run generate-candidate OGS-00011 \
  --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
  --cores 64 \
  --resume
```

See `resources/generators/gwas-catalog-eur-hybrid/README.md` for the stage
(`--stage`), resume, finalise and human-review details. `workflow/generate.smk` wires the same stages as an optional coarse DAG
for a future multi-release batch:

```sh
snakemake --snakefile workflow/generate.smk --cores 64 \
  --config store_id=OGS-00011 \
           config=resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
           snapshot_id=gwas-catalog-ssf-eur-hybrid-2026-09-22 \
           work_root=/data/opengwasdb/work/gwas-catalog-eur-hybrid \
           registry_root=stores
```

The per-Analysis pool and checkpoints stay inside the resolver
(`opengwasdb resolve-analyses`), so the DAG never loads a genome-scale reference
once per Analysis.
