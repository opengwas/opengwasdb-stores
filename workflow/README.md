# Production Store Release workflow (issue #98)

One command builds one Store Release from its fixed input:

```bash
pixi run release --configfile families/<family>/releases/<release>/build.yaml
```

`workflow/Snakefile` is dependency wiring only. It reads the release's
`build.yaml`, asks `workflow/model.py` for the release's paths, and declares
which phase depends on which. It contains no Store-Family name, no source column
name, and no manifest translation. Every rule shells out to `workflow/phase.py`,
which owns everything semantic.

## The DAG

| Phase | What it does | OpenGWASDB call |
|---|---|---|
| `validate_fixed_inputs` | Validates `build.yaml`, `analyses.tsv`, the selected source files and their checksums, and every declared Reference Resource (`resources/lib/release_plan.py`). Emits `sidecars/input-validation.json`. | — |
| `resolve_analysis_metadata` | **Passthrough** (issue #98): freezes the selection into `work/analyses.resolved.tsv` and writes the shared builder manifest (issue #96) into `work/builder-manifest.tsv`. Emits `sidecars/metadata-resolution.tsv`. | — |
| `build_observed_store` | Runs the plan's `build.command` with its opaque `build.arguments`. Emits `sidecars/build-report.tsv`. | `opengwasdb <build.command>` |
| `regenerate_observed_overview` | Regenerates `overview.html` from the persisted Store, after any mutation. | `opengwasdb regenerate-overview` |
| `validate_observed_release` | Validates the Store and merges the CLI result into the release's `validation.yaml`. | `opengwasdb validate` |

The rho and Reference-Completion branches (issue #100) are not wired. A release
that enables either is refused by `workflow/model.py` rather than built without
them, and the passthrough resolve is replaced by real ancestry and effect-scale
resolution in issue #99.

## Completion records, not Store mtimes

A built Store is a mutated directory, so its modification time is evidence of
nothing. Each phase writes `work/completions/<phase>.json` only after its output
passes that phase's read-back checks, and Snakemake tracks the record. A partial
Store has no record and cannot masquerade as a completed phase.

Each record binds the phase name and Store Release identity, the sha256 of
`build.yaml`, `analyses.tsv`, every selected source file and every declared
Reference Resource descriptor, the pinned `opengwasdb` revision and the
effective arguments, the output locations, the completion time, and the
read-back result. Changing any bound input invalidates that phase and its
dependents on the next run.

## Resumption contract

- **The build replaces the Store atomically.** It builds into the
  `store.opengwasdb.partial` sibling and moves it into place only after the
  envelope reads back. An interrupted or failed build therefore never leaves a
  half-written Store at the release's Store path.
- **A failed phase writes no record.** The next invocation re-runs it and
  everything downstream, whatever the Store's mtime says.
- **An already-complete phase is not re-run.** A fully built release reruns as
  `Nothing to be done`.

## Layout

Paths come from the release's `build.yaml` (`artifacts.artifact_root`,
`artifacts.release_subdir`, optional `artifacts.work_dir`/`artifacts.store_uri`),
following ADR 0018's `<artifact-root>/<store-family-id>/releases/<family-release-id>/`.
Work files and the built Store are artifacts, not tracked metadata (ADR 0015).
The only things written back into the release bundle are the small
`sidecars/` reports and `validation.yaml`.

## Environment

`pixi run release` runs in the `workflow` environment (`snakemake-minimal` plus
`opengwasdb`). It is separate from `workflow-prototype`, which stays a throwaway
model of the design (`resources/prototypes/snakemake-release-pipeline/`).

## Fixture

`tests/release-workflow/fixtures/r13-fixture/` is a three-Analysis,
eight-variant FinnGen R13-shaped release, checked in so the whole DAG can be run
in seconds. Regenerate it with
`python3 tests/release-workflow/fixtures/generate_r13_fixture.py`. The suite that
drives the Snakefile against it is `tests/release-workflow/run_tests.py`
(`pixi run test`).
