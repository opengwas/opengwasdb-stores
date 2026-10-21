# opengwasdb-stores

Registry and orchestration metadata for building many OpenGWASDB store releases.

This repository records what stores should exist, what source inputs define them,
how releases were produced, and what should be built next. It does not implement
OpenGWASDB source readers, normalisation, storage layouts, or query engines.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development workflow and
[`docs/spec/store-release-workflow.md`](docs/spec/store-release-workflow.md) for
the proposed fixed-input Store Release pipeline.

## Software Environment

All Python, R, Quarto, and native tooling (PLINK2, bcftools) is managed by
[Pixi](https://pixi.sh) from the root `pixi.toml`/`pixi.lock` (issue #41). A
fresh checkout needs only Pixi installed, plus access to any declared external
data. The sole execution interface is `pixi run <task>`:

```sh
pixi run env-check      # report/validate resolved tool + package versions
pixi run test           # every lightweight test suite (Python + R)
pixi run test-python    # Python suites only
pixi run test-r         # R suites only
pixi run materialise-gwas-ssf-ragged OGS-00006 \
  --qc-panel=resources/reference-resources/qc-panel-hg38/qc_panel.tsv
pixi run --environment docs docs   # render and assemble the docs/ site
pixi run --environment ld-panel ld-acquire -- --help
pixi run --environment ld-panel ld-materialize -- --help
```

For a one-off script not wired to a named task, run it inside the environment
with `pixi run python <script>.py` or `pixi run Rscript <script>.R` rather
than invoking a bare interpreter or a sibling checkout's virtualenv.

## Model

```text
Source Collection
  -> Manifest Generation           (src/manifest_generation/)
  -> accepted Release Bundle       stores/OGS-00042/
  -> Store Release Build           (src/ogstores/, workflow/)
```

Two workflows, meeting at the accepted Release Bundle and sharing no DAG.
Store Release Build is specified in
[`docs/spec/store-release-workflow.md`](docs/spec/store-release-workflow.md).
Manifest Generation is the `manifest_generation` package under
`src/manifest_generation/` (#249): the shared candidate workflow and emitter at
its root, and each Source Provider's connector code (acquisition, metadata
adapters, provider-specific generators) under `providers/`. Each generator's
config and reviewed inputs stay under `resources/generators/<generator>/`, and
each release records the commands it ran in `release.yaml:generator.commands`.
The legacy R generators also remain under `resources/generators/` until they
retire (ADR 0036). What is still open is a Manifest Generation Snakemake
DAG (`workflow/generate.smk`), whose four boundary rules are fixed so Store
Release Build is not built against a moving target.

The single rule everything else follows from (ADR 0023):

> The only thing this repository computes on the path from an accepted Release
> Bundle to a built Store Release is an `opengwasdb` command line.

It does not read, rewrite, project, or validate a row of association or
Analysis data, and it does not inspect a built Store's internals. Where an
`opengwasdb` command cannot consume the canonical bundle, the fix is to
improve that command's interface upstream rather than to add a projection
layer here.

## Repository Layout

```text
stores/<store-id>/       Release Bundles plus generated summary.yaml
stores.tsv  STORES.md    generated master list and human view
src/                     all importable Python
  ogstores/                Store Release Build and the Release Bundle contract;
                           the readiness and indexed-subset audit CLIs
  manifest_generation/     Manifest Generation: the shared pipeline, the emitter,
                           the bundle-check CLI
    providers/               per-Source-Provider connectors (gwas_catalog, ukb_b,
                             dbgap, finngen, eqtlgen, opengwas), in any language
  curation/                Canonical Trait Mapping Table curation
workflow/                Snakemake wiring: Snakefile and release.py (Store Release
                         Build), generate.smk (Manifest Generation)
resources/               inputs only
  reference-resources/     LD panels, reference AF, trait mappings, QC panels
  annotations/             curated metadata that evolves after release
  inventories/             frozen Source Inventories
  generators/              release configs, reviewed tables, generation receipts;
                           the legacy R generators until stage 2 of epic #282
tools/                   repository tooling: ld-panel/, benchmarks/,
                         reference-resources/, the env check, the site build, spikes
tests/                   the suites, and run_all_tests.py behind `pixi run test`
docs/                    adr/ decisions, spec/ specifications, reports/ report
                         sources, and the rendered site
CONTEXT.md               project language
```

Each kind of thing has one home (ADR 0038). `stores/` is what this repository
produces. `src/` holds all its importable code: Store Release Build
(`src/ogstores`) and Manifest Generation (`src/manifest_generation`) meet only
at the Release Bundle. `workflow/` wires them, `resources/` holds only their
inputs, and `tools/` holds the tooling that is neither.
`tests/packaging/test_layout.py` keeps it that way. `families/` and
`.rebuild133/` are not part of the layout; they retire through #252.
`CONTRIBUTING.md` documents each directory.

## Store Release identity

Every Store Release has one globally unique opaque identifier, `OGS-` plus
five digits, allocated sequentially (ADR 0022). It names the registry
directory, the artifact directory, and the `release_id` in the built Store's
manifest. There is no Store Family tier: the `OGS-` id is the only identifier,
and access posture survives as descriptive `release.yaml` metadata (ADR 0028).

Identifiers are opaque so that metadata cannot be stuffed into them and go
stale, and so the generated master list is the only way to find a release --
which makes that list load-bearing rather than documentation. Human legibility
comes from each release's `label` and from generated `by-label/` symlinks;
nothing resolves a release by label.

## Release Bundles

```text
stores/<store-id>/
  release.yaml      identity, label, status, lineage, provenance
  build.yaml        the recipe: an opengwasdb subcommand and its flags
  analyses.tsv      membership; opengwasdb owns the schema
  validation.yaml   evidence, written back by the run
```

`build.yaml` names an `opengwasdb` CLI subcommand, never a Python entry point,
and its `options` keys are `opengwasdb` flag names passed through verbatim --
the registry never acquires a mirrored copy of a parameter schema that would
drift.

The repository stores metadata and small reports only. Store artifacts, source
data, large logs, and large benchmark outputs live outside it.

## External Artifacts

A Store Release's artifact path is a pure function of its identifier, so
resolving a parent Store from `derived_from` needs no registry lookup:

```text
<artifact-root>/<store-id>/
  source/                   acquired or filtered source files
  work/                     checkpoints, scratch, logs
  records/<step>.json       one per executed step
  store.opengwasdb          the Store Release
  store.opengwasdb.partial  transient staged destination
  publication.json          transient: a publication in progress
  replaced/<UTC>/           a release a forced run replaced, kept until a person deletes it
<artifact-root>/by-label/   generated symlinks
```

The canonical artifact root is `/data/opengwasdb/stores`, resolving built
releases to `/data/opengwasdb/stores/<OGS-ID>/store.opengwasdb` with sibling
`.partial`, `records/`, `logs/`, and artifact-side `by-label/` symlinks under
`/data/opengwasdb/stores/by-label/`.

The root is deployment configuration, not part of a Release Bundle. It
resolves, highest precedence first, from a workflow `--config artifact_root=`,
the `OPENGWASDB_ARTIFACT_ROOT` environment variable, the repository's
`ogstores.yaml`, then the built-in default above. One immutable bundle can
therefore be built on CI, a laptop, or the production host without editing it.

## Pipeline Execution and Preflight

- **Workflow tests (`tests/workflow/`) are fixture-scale**: they validate the Snakemake DAG, command-line assembly, staged `.partial` transactions, publication gating, and crash recovery using synthetic test bundles.
- **Production builds require source/reference preflight**: real releases cannot build without their configured inputs (e.g. GWAS Catalog files, LD reference panels, frequency tables) present on disk.
- **The all-seven release pipeline command**:
  ```sh
  pixi run release OGS-00001 OGS-00002 OGS-00003 OGS-00004 OGS-00005 OGS-00006 OGS-00007
  ```
  This command is recorded for production operations but **must not be run** until configured source and reference inputs exist on the host filesystem.

## Worked example

```text
stores/OGS-00003/                  finngen-r13 / r13-pilot-20, dense observed-only
resources/generators/finngen-r13/  its Manifest Generation inputs, and the legacy R generator
```

`stores/README.md` lists all seven migrated Trial Store Releases. The bundles
left under `families/` are being triaged and retired through #252.
