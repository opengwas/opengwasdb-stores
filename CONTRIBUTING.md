# Contributing to opengwasdb-stores

This repository is the registry and orchestration layer for OpenGWASDB Store
Releases. It records what should be built and the evidence that it was built
correctly; reusable source readers, Store builders, and query behavior belong in
the sibling `opengwasdb` repository.

`CONTEXT.md` is the authority on project vocabulary. Use its terms in code,
configuration, documentation, issue comments, and commit messages.

## The principle everything else serves

**A wrong answer that looks like a right answer is the worst outcome this
project can produce.**

Store construction can fail silently: a source column can be interpreted as the
wrong allele, a file can be omitted, or a partial Store can look complete. Three
habits follow:

1. Fail loudly rather than silently skipping, coercing, or substituting data.
2. Keep absence distinct from zero and from a reference-derived value.
3. Add a validation rule whenever a new silent-failure class is discovered.

## Branches

```text
feature branch  ->  dev  ->  main
```

- `main` is the stable repository state.
- `dev` is the integration branch. All work lands here before `main`.
- Feature branches start from `dev` and return to it, one issue or coherent
  change at a time.

Do not mix unrelated clean-up into a feature branch. Preserve a contributor's
existing working-tree changes and resolve overlapping edits explicitly.

## Repository boundaries

The repository tracks small, reviewable definitions and evidence:

- Source Collection, Store Family, and Reference Resource metadata;
- accepted Release Manifest bundles (`release.yaml`, `analyses.tsv`,
  `build.yaml`, and `validation.yaml`);
- Store-specific input-generation helpers and shared orchestration;
- small validation and build reports.

Raw summary statistics, built Stores, transient work directories, and large logs
live outside Git and are referenced by explicit paths or URIs. Shared source
reading, normalization, Store construction, and validation behavior belongs in
`opengwasdb` (ADRs 0012 and 0017).

Store-specific helpers may acquire and select data, but the shared Store Release
workflow begins at the fixed-input boundary documented in
[`docs/spec/store-release-workflow.md`](docs/spec/store-release-workflow.md).

## Repository layout

```text
stores/            accepted Release Bundles, one per Store Release
workflow/          Phase A: accepted bundle -> validated Store Release
src/ogstores/      the Python package both phases use
resources/         everything on the input side
  families/            Store Family identity and priority
  source-collections/  upstream summary-statistics inventories
  reference-resources/ auxiliary build-time inputs
  annotations/         curated metadata that outlives a release
  generators/          Phase B: sources -> candidate bundle
  scripts/             standalone toolchains and repo tooling
docs/              adr/ decisions, spec/ specifications, rendered site
tests/
```

`resources/` is not a miscellany. The split is output versus input: `stores/`
holds what this repository produces, `workflow/` and `src/` are the machinery
that produces it, and everything under `resources/` is an input to that, or
something that makes an input.

### `stores/`

One directory per Store Release, named by its opaque `OGS-` identifier
(ADR 0022): `release.yaml`, `build.yaml`, `analyses.tsv`, `validation.yaml`.
Simultaneously the output of Phase B and the fixed input of Phase A.

A bundle is immutable once accepted. A material change to membership or to the
Build Recipe is a **new** Store Release (ADR 0004), not an edit. Correcting
Analytical Metadata after publication is a Release Erratum.

### `workflow/`

Phase A. `Snakefile` scans `stores/` and wires dependencies; nothing else.
Per ADR 0023 it contains no Store Family name, no source column name, no
manifest translation, and no layout branch -- each rule asks `ogstores.plan`
for a Step and hands it to `ogstores.run`.

`generate.smk` (Phase B) will live here too. The two share no DAG: the accepted
bundle is a boundary only because a human froze it, and one graph spanning both
would silently regenerate a bundle and rebuild a Store when a generator config
changed.

### `src/ogstores/`

`bundle.py` (load and check a bundle), `plan.py` (Bundle to argv -- the seam),
`paths.py`, `run.py`, `index.py`. Top-level rather than inside `workflow/`
because it is not Phase A's: a generator validates what it emits with
`bundle.check()`, so Phase B depends on it too.

### `resources/families/`

Store Family identity -- intended biological scope, query promise, access
posture, release cadence, build priority. A lookup table keyed by Store Family
ID, because ADR 0022 made family a field on a Store Release rather than a path
level. A Store Family is built from exactly one Source Collection (ADR 0010).

### `resources/source-collections/`

One directory per homogeneous upstream summary-statistics inventory.
`source.yaml` declares the provider, Source Format, Source Reader Capability,
access posture, and default licence; an optional `inventory.tsv` is the
discovered snapshot of what is available upstream.

A Source Collection has exactly one Source Format and one Source Reader
Capability (ADR 0009). Declarative records only -- code that *reads* the data
belongs in `generators/`, and code that interprets the statistics belongs in
`opengwasdb`.

### `resources/reference-resources/`

Auxiliary build-time inputs that are **not** the Source Collection of any
family (ADR 0011): LD reference panels, reference allele-frequency panels,
ancestry-mixture references, QC panels, and the Canonical Trait Mapping Table.
Each carries a `resource.yaml` declaring kind, ancestry, genome build, variant
ID convention, and location.

Small tables may be tracked here. Large panels live under the artifact root and
are referenced by path -- this repository is not an artifact store (ADR 0015).

### `resources/annotations/`

Curated metadata that may change *after* a Store Release is published without
changing the analytical asset -- Trait Annotations principally.

The distinction is deliberate and load-bearing. Re-curating a Trait Annotation
does not create a new Store Release; correcting Analytical Metadata, which
changes how the statistics are interpreted, does.

### `resources/generators/`

Phase B. `<family-id>/` holds a family's entry point, config, and README;
`lib/` holds shared helpers; `lib/source-formats/` holds code scoped to a
Source Format, so two families sharing a Source Collection share selection code
without a configuration system by accident.

A generator's only output is a bundle directory. It never builds a Store, and
it invokes statistics rather than implementing them: if two Store Families
computing something differently would be a bug, it belongs in `opengwasdb`.

### `resources/scripts/`

Standalone toolchains and repository tooling that belong to neither phase: LD
panel construction (`ld-panel/`), documentation rendering (`*.qmd`,
`build-site.sh`), repository checks (`run_all_tests.py`, `env_check.py`), and
one-off analyses.

This is the one directory at risk of becoming a junk drawer. The test: if
something here starts being run as a step in producing a Store Release, it
belongs in `generators/` or `workflow/` instead.

## Metadata and scripts

- YAML holds nested configuration and metadata; TSV holds repeated tabular rows
  (ADR 0013).
- Keep Release Bundles self-contained. A Store Release's artifact path is a
  pure function of its identifier, `<artifact-root>/<store-id>/` (ADRs 0014 and
  0022; 0018's family-first layout is superseded).
- A committed `analyses.tsv` is an exact release selection. Do not silently add
  every file found in a directory at build time.
- Record source file names and checksums. Fail if a selected file is missing or
  its identity no longer matches.
- Do not copy OpenGWASDB builder logic into this repository. Invoke a documented
  OpenGWASDB CLI subcommand with explicit arguments; `build.options` keys are
  flag names passed through verbatim, never interpreted here (ADR 0023).
- Comments should explain why a choice exists and cite the issue or ADR that
  constrained it.

## Decisions and documentation

Anything that constrains future work gets a numbered ADR in `docs/adr/`.
Supersede an earlier ADR explicitly rather than quietly contradicting it. A
workflow or file contract also needs a specification under `docs/spec/` in the
same commit as the implementation that adopts it.

Documentation is part of a change:

- update `CONTEXT.md` when the domain language changes;
- update `docs/release-metadata-schema.md` when bundle fields change;
- update the Store Release workflow specification when its boundary or DAG
  changes;
- regenerate reports rather than hand-editing measured values;
- update the sibling `opengwasdb` repository or open an issue there when a
  change requires a new reader, builder, manifest field, or validation behavior.

## Checks

Use the Pixi environments pinned by this repository:

```bash
pixi run env-check
pixi run test
pixi run test-python
pixi run test-r
pixi run --environment docs docs-smoke
```

Run the checks relevant to a change. A behavior change to a script needs a test
that reproduces the failure it prevents. Assert that fixtures exercise the
intended source format, ancestry path, or Store layout before relying on their
result.

For a Release Bundle or generated report, also run the repository's applicable
schema and validation commands and inspect the resulting evidence. A successful
process exit is not proof that a scientifically valid Store was produced.

## Commits and pull requests

A commit message explains why the change exists, records relevant measurements
or validation, and references its issue. If a change alters a documented
contract, update the specification or ADR in the same commit.

When reporting work, state what was completed, what was deliberately excluded,
which checks ran, and any external data that was unavailable. Do not describe a
partial build or unvalidated report as complete.
