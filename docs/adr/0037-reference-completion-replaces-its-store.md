# Reference completion replaces its Store under the same id

Status: accepted

Supersedes [0007](0007-reference-completion-as-release-lineage.md).

ADR 0007 published a reference-completed Store under a second id, derived from
its Observed-Only parent. For UKB-B that is OGS-00009 (observed, 33 GB) beside
OGS-00010 (completed, 47 GB). Every observed cell is stored twice, and each
completed Store costs a second registry id, a second Release Bundle and a second
Store Release Build run.

Completion cannot be an in-place addition the way the Top-Hit Index, Rho Matrix
and Indexed Variant Subsets are. opengwasdb ADR 0061 allows in-place additions
only when they can be deleted, an unaware reader still reads correctly, and no
query answer changes. Completion fails all three: default queries include
imputed associations (opengwasdb ADR 0012), and the axis becomes source ∪
reference panel (opengwasdb ADR 0022), so every Variant Index moves.

Completion is still a replacement rather than an addition. The completed Store
contains everything its parent held: completion copies each observed cell across
in the source's own encoding (opengwasdb ADR 0038 §4), and the imputed mask
keeps imputed cells apart. `observed_only=True` returns the parent's
associations on every query path. Store Release Build can already replace a
published Store safely: the forced-run transaction of #195 stages the new Store,
sets the old one aside, swaps them, and archives the old records.

## Decision

1. **A reference-completed Store has one id and is published twice.**
   - Store Release Build first builds and publishes the Observed-Only Store as
     usual, so it is queryable while completion runs.
   - Completion then reads that published Store and writes to the id's staged
     path, `store.opengwasdb.partial`. Its Top-Hit, Rho, overview and
     validation steps run there.
   - Publication replaces the Observed-Only Store through the existing
     replacement transaction.
   - The bundle declares both phases, and there is no `derived_from` parent.
   - A Store already built and published as Observed-Only can be completed
     later. Its accepted `observed_only` bundle may gain a `complete` block
     and switch to `completion_state: reference_completed`. This is the only
     amendment an accepted bundle allows. The amended build phase must still
     plan exactly the argv recorded for the published Store, with unchanged
     membership, so completing it schedules no rebuild.

2. **An automated observed-view check gates the swap.** It runs after
   validation and before publication. Any failure refuses the publication and
   leaves the Observed-Only Store in place. Under ADR 0023 this repository
   never inspects a Store's internals, so the check is an `opengwasdb` command.
   Store Release Build plans it as a step and records its verdict.
   - **Exhaustive part:** the published Store's ALIDs all map into the staged
     Store's axis with their order preserved, and both list the same Analyses
     in the same order.
   - **Sampled part:** whole chunk-rows across all Analyses, because a Dense
     read costs a full chunk however few cells it uses. The sample has a seeded
     random stratum (1,000 chunk-rows by default) plus deliberate strata at
     chromosome starts and ends, the final partial chunk-row, and the
     completion writer's band boundaries. In every sampled band:
     - each observed cell has identical decoded Z, SE and EAF and is not marked
       imputed;
     - no previously missing cell is observed;
     - no inserted panel row carries an observed cell.
   - **Record:** the verdict carries the seed, the sample and the counts, and
     is kept as the step's record.
   - **Scope:** the check covers Dense Stores, Hybrid Stores, whose
     Dense Component it checks as above and whose Overflow it checks too
     (Clarified by #333 below), and Ragged Stores, which it checks Analysis
     by Analysis (Clarified by #336 below).

3. **The replaced Observed-Only Store is deleted, not archived.** Unlike a
   forced re-run, the swap deletes the replaced Store's arrays once the new
   Store is in place. The replaced release's records and Validation Record are
   still archived under `replaced/<stamp>/`, so the change is dated and its
   evidence kept.

## Evidence: OGS-00009 → OGS-00010

This check was run by hand against the existing two-id pair on 2026-10-21, with
opengwasdb 0.3.0 (seed 20261021). It took 684 s, a median of 0.61 s per band.
The prototype is `tools/benchmarks/observed-view-check.py`, and its summary
is `docs/benchmark-output/ogs00009_ogs00010_observed_view_check.json`. The
implementation is specified in #302.

- **Exhaustive axis map:** all 9,847,701 parent variants were found in the
  13,549,988-variant child, in order. The child has 3,702,287 inserted panel
  rows. Mapping the full axis took about 4 s.
- **Sample:** 1,077 of the parent's 9,848 chunk-rows. 1,000 were random
  (10.2% of the grid). 79 came from the edge strata: 24 chromosome edges, the
  final partial chunk-row and 54 writer-band boundaries; 2 of these were also
  drawn at random. Together they cover 1,076,701 parent variants × 2,024
  Analyses: 2,179,242,824 cells, of which 1,250,967,477 are observed in the
  parent.
- **Result:** no differences.
  - 0 mismatches in Z, SE or EAF; the largest absolute difference was 0.0.
  - 0 observed cells were marked imputed or missing in the child.
  - 0 parent-missing cells were observed in the child.
  - 0 observed cells on the 530,140 inserted rows in sampled spans.
  - 330,861,006 parent-missing cells were imputed in the child, as expected.
- **Power:** a defect in 30 or more of the 9,848 chunk-rows would have
  appeared in the random stratum with at least 95% probability. A defect
  confined to fewer chunk-rows than that cannot be excluded by sampling.
- **Negative control:** with the row map shifted by one, about 99% of cells
  mismatched on Z, SE and EAF, and 0.2–0.3M cells were reported in each
  direction between observed and imputed or missing.

## Considered options

- **Two ids, with the parent's Store deleted once the child validates.**
  Rejected. It still costs a second id and bundle for every completed Store,
  and a `superseded` parent bundle can never be rebuilt (`BUILDABLE_STATUSES`).
- **Complete before publishing anything.** This would build the Observed-Only
  Store unpublished in the Work Root and publish once. Rejected: nothing is
  queryable during a long completion. Supporting it as well would mean a second
  path beside this one.
- **Store completion as extra arrays beside the observed planes.** Rejected:
  every query would merge two components. opengwasdb removed Ragged Overflow
  from Dense completion for exactly this cost (ADR 0022).
- **Archive the replaced Store as a forced re-run does.** Rejected: the archive
  is the duplicate this decision removes, and the completed Store contains its
  data.
- **Keep both Stores published** (ADR 0007, the status quo). Rejected: 33 GB
  for UKB-B alone, for data the completed Store already serves.

## Consequences

- **An id's default answers change once.** Before completion they are
  observed-only; afterwards they include imputed associations, and Variant
  Indices are renumbered. The observed associations themselves do not change
  (Evidence), so anything computed on the interim Store can be reproduced with
  `observed_only=True`, matched by ALID (opengwasdb ADR 0061). The archived
  records date the switch.
- **The completed release needs its own release id.** opengwasdb ADR 0011
  already keeps the Store identity and gives the completed release a new
  release identity. The planner currently passes the store id as
  `--release-id` for every step, so the completion phase must pass a distinct
  one. The registry id names the Store.
- **The swap is unsafe for a process holding the Store open.** A long-running
  reader keeps cached array metadata. After the swap it would read the new
  chunks through the old shapes and return wrong values without raising an
  error. Forced re-runs already carry this risk; completion makes replacement
  routine rather than exceptional.
- **opengwasdb needs one new command: the observed-view check** (opengwas/opengwasdb#289).
  - Staging and publication belong to Store Release Build, so no "publish or
    keep in work" flag is needed. Builds already write to
    `store.opengwasdb.partial`, and `register` publishes.
  - `on_panel` already marks panel rows.
  - Two follow-ups are optional:
    - persist which rows came from the source (completion builds this map as
      `out_to_src`, then discards it). Re-deriving the Observed-Only Store
      would then be exact rather than inferred from "rows with an observed
      cell". That inference held for all 1,076,701 sampled rows;
    - let completion take a completed Store as its source and drop the imputed
      cells. Re-imputing against a new panel would then not need a rebuild.
- **Re-completing against a new panel** rebuilds the Observed-Only Store from
  the bundle, which stays buildable, then completes again. With the second
  follow-up it would instead complete straight from the current Store.
- **Store Release Build changes:**
  - **Bundle contract:** `reference_completed` no longer requires
    `derived_from`, and `build.yaml` carries both the `build` and `complete`
    blocks.
  - **Planner:** two phases under one id, with the completion's source being
    the id's own published Store and per-phase step records.
  - **Steps:** the observed-view check runs before `register` in the
    completion phase, and the replacement deletes the old Store's arrays.
  - **Master list:** it reports the completion state of the Store actually
    published.
- **Existing two-id pairs are not migrated by this decision.** OGS-00009 and
  OGS-00010 are both still `candidate`, so they can be folded into one id
  without touching an accepted release.
- **Bundle immutability gains one exception.** CONTEXT.md and ADRs 0014 and
  0031 say an accepted Release Bundle is immutable. Under Decision 1, an
  accepted `observed_only` bundle may gain a `complete` block. Any other change
  is still refused. That is what lets Stores built before this decision be
  completed later under their own ids.
- **CONTEXT.md needs updating if this is accepted.** Store Release currently
  reads "versioned, immutable", and Release Lineage lists reference-completed
  releases. Both need to say that completion replaces an id's Store once, as a
  new release of the same Store. The "immutable once accepted" wording for
  Release Bundles needs the one exception above.

## Clarified by #305

The amendment rule of Decision 1 is enforced before any job runs, as follows.

- **Scope.** Only a release whose bundle now completes its own Store, and
  whose build phase has published (`records/register.json`), is checked.
  Observed-only and two-id releases get no check, as before.
- **Baseline.** The published release's records, never its Store, and only
  what made that Store:
  - the recorded argv of the build phase's Store-writing steps
    (`variant-reference` if planned, and `build`);
  - the membership the Store was built from, its derived build manifest
    and the Analyses it excludes;
  - once the completion phase has published, the recorded argv of its
    `complete`.

  Post steps of either phase (`top-hits`, `rho`, Indexed Variant Subsets,
  overview, validation) are re-runnable in-place additions (ADR 0034,
  opengwasdb ADR 0061). A new declaration, or a planner that has moved on
  since, is not a change to the Store. So "plan exactly the argv recorded
  for the published Store" means the argv that wrote it.
- **Left to review.** `release.yaml`'s prose and provenance. No code
  enforces accepted-bundle immutability for those fields, for any bundle.

## Clarified by #333

A Hybrid release completes its own Store too. The planner admits it as it
does a Dense one, with `complete-hybrid`. (A Ragged release was still refused
until #336, below.)

- **What the check covers in a Hybrid Store.** Completion changes both
  components. An Overflow association at a variant the LD panel adds to the
  Dense axis moves into the Dense Component as an observed cell (a crossover),
  and the Overflow is rebuilt without it. So an inserted panel row may carry
  an observed cell after all, but only a crossover. opengwasdb#295 checks:
  - the Dense and shared axes, exhaustively;
  - every crossover against its source value;
  - the Dense Component by the sample of Decision 2, with crossovers set aside;
  - a seeded sample of whole Overflow Analyses, association by association;
  - every Analysis's Overflow count, less its crossovers.
- **One step for both layouts.** The check's step and argv are the same as a
  Dense release's. `--n-random` draws 1,000 chunk-rows of the Dense Component
  and 1,000 Overflow Analyses from the same seed. The command reads the layout
  from the two Stores.
- **The gate on the runtime is the pin.** A core whose check compares only
  Dense pairs offers the same command, so probing for it cannot refuse it. The
  release entry point refuses any core but the pinned revision, which has
  #295.
- **No resume.** `complete-dense` keeps a checkpoint and resumes after an
  interruption; `complete-hybrid` does not, so an interrupted Hybrid
  completion starts again from the published Observed-Only Store.

## Clarified by #336

A Ragged release completes its own Store too, with `complete-ragged`. With
it, every layout can.

- **What the check covers in a Ragged Store.** There is no grid of
  chunk-rows, and completion rebuilds the variant axis, so opengwasdb#296
  matches by ALID:
  - every source variant in the completed axis, and the same Analyses in
    both, exhaustively;
  - a seeded sample of whole Analyses (`--n-random` of them, from the same
    seed). In each, every observed association keeps its decoded Z, SE and
    EAF and is not marked imputed, no association the source lacks is
    observed, and every imputed association lies in a region completion
    retained.
- **The check needs the panel.** Retained regions are not stored as
  coordinates; the check rebuilds them from the LD panel and ancestry
  completion used, and refuses a Ragged pair without the panel. So for a
  Ragged release the planner passes the `complete` block's `ld-panel` and
  `ancestry` to the check step, and refuses a `complete` block that leaves
  either out. Dense and Hybrid steps are unchanged.
- **SE is compared exactly.** `complete-ragged` refits and re-quantises SE
  stored as `int8_residual`, which on real data moves observed SE slightly
  (opengwas/opengwasdb#301). Until that is resolved, a Ragged Store whose SE
  is stored that way, as OGS-00006's is, fails the check.
- **No resume and no extra steps.** `complete-ragged` keeps no checkpoint, so
  an interrupted completion starts again from the published Observed-Only
  Store. It builds its Top-Hit Index inline and writes the format-0.2.0
  `by_variant` index, which validation audits; a Ragged Store has no overview
  and no Indexed Variant Subsets. Its completion phase is `complete`,
  validation and the check.
