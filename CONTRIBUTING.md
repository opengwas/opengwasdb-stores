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

## Worktrees

A worktree is a second working directory backed by the **same** `.git`
database, so several branches can be checked out at once without stashing.
Agents use them heavily — one per task — and they accumulate silently: this
repository reached 54 before anyone counted.

Two facts make them safe to be relaxed about:

- **Removing a worktree does not delete its branch or its commits.** Those live
  in the shared `.git`. A worktree is a *view*, and removing one throws away
  only the view.
- **The same branch cannot be checked out in two worktrees.** Git refuses. This
  is the rule that actually bites.

### The rule

> **The branch a human is reading must never be checked out in an agent's
> worktree.**

A feature branch lives in the primary checkout, where it can be opened in an
editor and watched while work continues. If an agent checks it out in a
worktree, the human cannot have it — which is the failure this rule exists to
prevent.

So:

```text
primary checkout          feat/<name>            read by a human, never by an agent
  worktree per task       ticket/<n>-<slug>      one agent, one ticket
                              |
                              +--> merged into feat/<name> by an orchestrator
```

Each ticket gets its own worktree and its own branch. An orchestrator merges
each one into the feature branch when it is green. Where tickets declare
blocking edges, those edges are the merge order.

### Naming

```text
feat/<name>          a feature branch, in the primary checkout
ticket/<n>-<slug>    one ticket's work, in its own worktree
```

### Cleaning up

```sh
git worktree list                 # every view, and what it has checked out
git worktree remove <path>        # remove one; its branch survives
git worktree prune                # forget views whose directory is gone
```

Before removing in bulk, check for work that exists nowhere else — uncommitted
files, and commits not reachable from any `origin` ref:

```sh
git -C <path> status --porcelain
git -C <path> rev-list --count HEAD --not --remotes=origin
```

Commit or tag anything that turns up. A branch merged into a pushed branch is
already safe, and a branch that reached a GitHub pull request is safe forever —
GitHub keeps `refs/pull/<n>/head` after the branch is deleted.

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
  families.yaml        Store Family records
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
Simultaneously the output of Phase B and the fixed input of Phase A. Also holds
candidate releases (`status: candidate`), so what is under consideration and
what has been built appear in one list.

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
`bundle.check()`, so Phase B depends on it too. `workflow/` is also Snakemake's
own namespace (`Snakefile`, `rules/`, `scripts/`, `envs/`).

### `resources/families.yaml`

One entry per Store Family: label, provider, access posture, default licence,
Source Reader Capability, Source Collection, query promise, build priority.

A Store Family is a product identity, not a format (ADR 0024). Three families
here share one Source Collection and one Source Format -- all EBI GWAS Catalog
GWAS-SSF -- and promise `full-gwas`, `signals_only` and `cis_and_signals`. The
format is what a builder sees; the family is what a query user sees. A family
is also the unit of continuity across releases, which is why the promise cannot
be a field restated on each one.

`source_reader_capability` is the only field that becomes argv. There is no
separate `source_format`, and no `source-collections/` directory: the Source
Collection is a grouping string. A real Source Inventory, when acquisition
produces one, returns as `resources/inventories/<id>.tsv` -- rows, not a
metadata tier.

### `resources/reference-resources/`

Auxiliary build-time inputs that are **not** the Source Collection of any
family (ADR 0011): LD reference panels, reference allele-frequency panels,
ancestry-mixture references, QC panels, the Canonical Trait Mapping Table, and
the SomaScan target tables. Each carries a `resource.yaml` declaring kind,
ancestry, genome build, variant ID convention, and location.

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

### `families/`

Not part of the layout. What remains is the thirteen Release Bundles that have
not yet migrated to `stores/`, awaiting triage -- each is either given a Store
Release id or marked `superseded`/`withdrawn` and retired. The directory goes
when it empties.

## Metadata and scripts

- YAML holds nested configuration and metadata; TSV holds repeated tabular rows
  (ADR 0013).
- Keep Release Bundles self-contained. A Store Release's artifact path is a
  pure function of its identifier, `<artifact-root>/<store-id>/` (ADRs 0014 and
  0022; 0018's family-first layout is superseded).
- The artifact root is deployment configuration, not a bundle field. Resolve it
  with `paths.artifact_root()`: a workflow config override, then
  `OPENGWASDB_ARTIFACT_ROOT`, then the tracked `ogstores.yaml`, then the
  built-in default. Never commit an artifact root into a Release Bundle, so one
  immutable bundle can be built on CI, a laptop, or the production host.
- A committed `analyses.tsv` is an exact release selection. Do not silently add
  every file found in a directory at build time.
- Record source file names and checksums. Fail if a selected file is missing or
  its identity no longer matches. For the legacy monolithic BESD Store Releases
  (`OGS-00001` and `OGS-00002`), source identity is currently recorded only as an
  unverified path prefix on a single deployment host (`source_snapshot.besd_prefix`
  in `OGS-00001`; `OGS-00002` carries only `source_snapshot_id` and inherits lineage
  via `derived_from`), with no checksum, file list, or size recorded or checked.
  This is a known integrity gap tracked by open issue #134, not an equivalent
  alternative to checksums.
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
pixi run bundle-check
pixi run test
pixi run test-python
pixi run test-r
pixi run --environment docs docs-smoke
```

`bundle.check(bundle, registry_root=...)` is the executable Release Bundle
contract. It returns a list of every registry-side error it can find (an empty
list means valid) and never raises for invalid bundle content. It reads only
files inside Release Bundles, plus a parent bundle when resolving
`derived_from`; it never opens a Store or inspects a Release Artifact. The
`bundle-check` task discovers every bundle under `stores/` and fails if any
error is returned, and CI runs that task explicitly. In particular, it rejects
a Build Recipe `artifacts` block because issue #126 moved the artifact root to
deployment configuration.

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
