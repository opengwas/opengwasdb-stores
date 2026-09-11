# Production Store Release workflow (issues #98, #100)

One command builds one Store Release from its fixed input:

```bash
pixi run release --configfile families/<family>/releases/<release>/build.yaml
```

`workflow/Snakefile` is dependency wiring only. It reads the release's
`build.yaml`, asks `workflow/model.py` for the release's paths and which optional
branches it enables, and declares which phase depends on which. It contains no
Store-Family name, no source column name, and no manifest translation. Every rule
shells out to `workflow/phase.py`, which owns everything semantic.

## The DAG

| Phase | What it does | OpenGWASDB call |
|---|---|---|
| `validate_fixed_inputs` | Validates `build.yaml`, `analyses.tsv`, the selected source files and their checksums, and every declared Reference Resource (`resources/lib/release_plan.py`). Refuses rho on a non-Dense layout here, before any Store work. Emits `sidecars/input-validation.json`. | — |
| `resolve_analysis_metadata` | **Real resolution** (issue #99): computes Assigned Ancestry, the Ancestry Assignment Method and per-super-population proportions (AF mixture fit against the declared `ancestry_mixture` resource), and effect-scale / phenotype SD (source-AF estimate, or declared-standardised verification). Writes `work/analyses.resolved.tsv` and the shared builder manifest (issue #96) into `work/builder-manifest.tsv`, and emits `sidecars/metadata-resolution.tsv`. Never writes the committed `analyses.tsv`. | — |
| `build_observed_store` | Runs the plan's `build.command` with its opaque `build.arguments`. Emits `sidecars/build-report.tsv`. | `opengwasdb <build.command>` |
| `build_observed_rho` | **Only when `rho.enabled`.** Mutates the built Store in place, adding `data.zarr/rho`. Emits `sidecars/rho-report.json`. | `opengwasdb build-dense-rho` |
| `regenerate_observed_overview` | Regenerates `overview.html` from the persisted Store. Depends on `build_observed_rho` when rho is enabled, so the page can never be written before the mutation that adds its Rho tab. | `opengwasdb regenerate-overview` |
| `validate_observed_release` | Validates the Store and merges the CLI result into the release's `validation.yaml`. Lands the release's lifecycle status: `validated`, or `built` when the effect-scale check failed (issue #99). | `opengwasdb validate` |
| `register_completed_release` | **Only when `reference_completion.enabled`.** Registers the lineage-linked child Store Release (ADR 0007) in its own Release Bundle, a sibling of the observed bundle. | — |
| `complete_store` | Builds the child Store from the observed Store into a `.partial` sibling, then moves it into place. The observed Store is only read. Emits `sidecars/completion-report.tsv` in the child bundle. | `opengwasdb <reference_completion.command>` |
| `build_completed_rho` | **Only when `rho.enabled`.** The child's in-place Rho Matrix. | `opengwasdb build-dense-rho` |
| `regenerate_completed_overview` | The child's `overview.html`, after its last mutation. | `opengwasdb regenerate-overview` |
| `validate_completed_release` | Validates the child Store and writes the child bundle's `validation.yaml`. The workflow's final target when completion is enabled. | `opengwasdb validate` |

## Effect-scale failure: report by default, block on request

A failed effect-scale check is evidence, not a workflow failure. It is recorded
in `validation.yaml` (`checks.effect_scale` / `checks.sd_estimation`) and in the
`warnings` list, and the release lands as `built` rather than `validated` -- the
FinnGen R13 pilot's `HEIGHT_IRN` genuinely fails effect-scale validation in an
otherwise clean release, and rescaling it silently would be worse than
recording it. A family that wants the failure to stop the build sets
`effect_scale_validation.block_on_failure: yes` in `build.yaml`.

## The two optional branches

Both branches are configured entirely in the observed release's `build.yaml`
(ADR 0022), as an `enabled` flag plus an **opaque** CLI flag mapping -- the same
passthrough as `build.arguments`, so a newly required builder flag needs no
change here:

```yaml
rho:
  enabled: true
  arguments:
    z-thresh: 100
    min-nulls: 1

reference_completion:
  enabled: true
  family_release_id: dense-observed-v1-completed
  command: complete-dense
  arguments:
    ld-panel: /data/opengwasdb/reference/hgdp1kgp-hg38/panel
    ancestry: EUR
    release-id: dense-observed-v1-completed
```

**Rho is Dense-only.** There is no Hybrid or Ragged rho implementation, so a
`build.yaml` that enables rho on a non-Dense layout is refused by
`resources/lib/release_plan.py` at `validate_fixed_inputs`, with a message naming
the layout, before anything expensive runs. Rho is an in-place Store mutation,
and `regenerate-overview` reads only what is already persisted, so overview
regeneration is ordered strictly after it.

**Reference Completion is a child, never a parent mutation.** The branch
registers a *distinct* Store Release whose lineage names the observed parent
(ADR 0007) and builds a separate Store under
`<artifact-root>/<family>/releases/<child-release-id>/`. The child's Release
Bundle is the observed bundle's sibling
(`families/<family>/releases/<child-release-id>/`), matching the checked-in
trial releases; the registration writes its `release.yaml`, and
`validate_completed_release` writes its `validation.yaml`. The observed Store is
opened read-only, so the completion branch cannot touch it. The child then runs
the same tail as the observed release -- rho, overview regeneration, validation,
in that order.

## Completion records, not Store mtimes

A built Store is a mutated directory, so its modification time is evidence of
nothing. Each phase writes `work/completions/<phase>.json` only after its output
passes that phase's read-back checks, and Snakemake tracks the record. A partial
Store has no record and cannot masquerade as a completed phase. The observed
release and its completion child each have their own `work/completions/`.

Each record binds the phase name and Store Release identity, the sha256 of
`build.yaml`, `analyses.tsv`, every selected source file and every declared
Reference Resource descriptor (and any Reference Resource file tracked in this
repository, so swapping the panel invalidates the phases that used it), the
pinned `opengwasdb` revision and the effective arguments, the output locations,
the completion time, and the read-back result. Changing any bound input
invalidates that phase and its dependents on the next run.

## Resumption contract

- **Every new-output phase replaces its Store atomically.** The build and the
  completion both write into a `store.opengwasdb.partial` sibling and move it
  into place only after the envelope reads back. An interrupted or failed phase
  therefore never leaves a half-written Store at the release's Store path.
- **A failed phase writes no record.** The next invocation re-runs it and
  everything downstream, whatever the Store's mtime says.
- **An interrupted completion resumes.** `opengwasdb complete-dense` writes a
  per-block checkpoint directory before it finishes. `complete_store` records
  the exact command it issued next to that checkpoint, and on the next
  invocation runs `opengwasdb complete-dense-resume` when it is unchanged --
  discarding a checkpoint from a different configuration rather than resuming
  against the wrong reference panel.
- **An already-complete phase is not re-run.** A fully built release reruns as
  `Nothing to be done`.

## Layout

Paths come from the release's `build.yaml` (`artifacts.artifact_root`,
`artifacts.release_subdir`, optional `artifacts.work_dir`/`artifacts.store_uri`),
following ADR 0018's `<artifact-root>/<store-family-id>/releases/<family-release-id>/`.
Work files and the built Store are artifacts, not tracked metadata (ADR 0015).
A Reference-Completion child uses ADR 0018's default layout for its own release
id rather than the observed release's `store_uri`, so the child Store can never
resolve onto the observed Store. The things written back into the repository are
the observed release bundle's small `sidecars/` reports and `validation.yaml`,
the one-line Release Status in its `release.yaml`, and the child's registered
Release Bundle.

## Environment

`pixi run release` runs in the `workflow` environment (`snakemake-minimal` plus
`opengwasdb`). It is separate from `workflow-prototype`, which stays a throwaway
model of the design (`resources/prototypes/snakemake-release-pipeline/`).

## Fixture

`tests/release-workflow/fixtures/r13-fixture/` is a three-Analysis,
eight-variant FinnGen R13-shaped release plus a one-block EUR LD reference panel,
checked in so the whole DAG -- including both optional branches -- can be run in
seconds. Its `BMI_FIXTURE` declares no upstream phenotype SD (so resolution
derives one), `HEIGHT_FIXTURE` is seeded to fail the declared-standardised
effect-scale check (landing the release as `built`), and `RX_STATIN_FIXTURE` is
a binary trait. `tests/release-workflow/fixtures/ancestry-reference/` is the
tiny two-group ancestry-mixture panel resolution fits against. Regenerate both
with `python3 tests/release-workflow/fixtures/generate_r13_fixture.py`. The suite
that drives the Snakefile against them is `tests/release-workflow/run_tests.py`
(`pixi run test`); `tests/metadata-resolution/run_tests.py` tests the resolution
module's gated-out and no-AF branches directly.
