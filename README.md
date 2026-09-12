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
  -> Manifest Generator            (Phase B, not yet designed)
  -> accepted Release Bundle       stores/OGS-00042/
  -> Store Release                 (Phase A)
```

Two workflows, meeting at the accepted Release Bundle and sharing no DAG.
Phase A is specified in
[`docs/spec/store-release-workflow.md`](docs/spec/store-release-workflow.md);
Phase B has four fixed boundary rules and is otherwise open.

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
stores/<store-id>/       accepted Release Bundles, one per Store Release
stores.tsv  STORES.md    generated master list
families/<family-id>/    Store Family identity, priority, query promise
source-collections/      upstream summary-statistics inventories
reference-resources/     LD panels, reference AF, trait mappings
generators/              Phase B: bundle producers
src/ogstores/            bundle.py  plan.py  paths.py  run.py  index.py
workflow/                Snakefile (Phase A), generate.smk (Phase B)
annotations/             curated metadata that evolves after release
docs/adr/  docs/spec/    decisions and specifications
CONTEXT.md               project language
```

## Store Release identity

Every Store Release has one globally unique opaque identifier, `OGS-` plus
five digits, allocated sequentially (ADR 0022). It names the registry
directory, the artifact directory, and the `release_id` in the built Store's
manifest. Store Family is a field on the release, not a path level.

Identifiers are opaque so that metadata cannot be stuffed into them and go
stale, and so the generated master list is the only way to find a release --
which makes that list load-bearing rather than documentation. Human legibility
comes from each release's `label` and from generated `by-label/` symlinks;
nothing resolves a release by label.

## Release Bundles

```text
stores/<store-id>/
  release.yaml      identity, label, family, status, lineage, provenance
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
  source/  work/  records/  store.opengwasdb
```

For this server the artifact root is `/data/opengwasdb`.

## Worked example

```text
stores/OGS-00003/          finngen-r13 / r13-pilot-20, dense observed-only
generators/finngen-r13/    the family's Phase B entry point
source-collections/finngen-r13/
```

`stores/README.md` lists all seven migrated Trial Store Releases.
