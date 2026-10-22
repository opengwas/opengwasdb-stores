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

### Each worktree runs its own environment

The repository's Python code (`ogstores`, `manifest_generation`, `curation`)
installs editable into the pixi environments (`pyproject.toml`, #257), and an
editable install points at the checkout that ran `pixi install`. So run
`pixi install` in a new worktree and use its own environment there: borrowing
another checkout's (`pixi run --manifest-path <other>/pixi.toml -e dev python3
...` from this worktree) imports that checkout's code. The test runner refuses
to run this checkout's tests under an environment installed from elsewhere, or
with nothing installed.

One test is a deliberate exception. The workflow suite's pre-#304 test
(`TestWorkflowCompletingAStoreBuiltBefore304`, #305) builds a Store with the
workflow as it was at an earlier revision. It runs that revision from a `git
archive`, with the archive's own `src/` ahead of the editable install on
`PYTHONPATH`, so the old code, not this checkout's, really runs. It exists
because a Store built by older code must still be completable without a
rebuild. Installing the old revision instead would cost minutes on every CI
run, for no extra fidelity. The exception covers that archived revision
only: this checkout's own code and tests always import from the installed
package.

## Repository boundaries

The repository tracks small, reviewable definitions and evidence:

- Source Collection and Reference Resource metadata;
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
stores/            Release Bundles, one per Store Release
src/               all importable Python
  ogstores/          Store Release Build, and the Release Bundle contract both use
  manifest_generation/  Manifest Generation: sources -> candidate Release Bundle
    providers/         per-Source-Provider connector code
  curation/          Canonical Trait Mapping Table curation
workflow/          Snakemake wiring for both workflows, and the release entry point
resources/         inputs only
  reference-resources/ auxiliary build-time inputs
  annotations/         curated metadata that outlives a release
  inventories/         frozen Source Inventories
  generators/          release configs, reviewed tables and receipts
tools/             repository tooling that is neither a package nor an input
tests/             the suites, and the runner behind `pixi run test`
docs/              adr/ decisions, spec/ specifications, reports/ report sources,
                   rendered site
.github/           CI (workflows/ci.yml)
```

Each kind of thing has one home (ADR 0038). `stores/` holds what this
repository produces. `src/` holds every importable module, `workflow/` wires
them into the two workflows, and `resources/` holds their inputs and nothing
else. Code that is neither a workflow's nor a test goes in `tools/`.
`tests/packaging/test_layout.py` enforces the layout:
- no Python outside `src/`, `tests/`, `tools/` and `workflow/`;
- no package outside `src/` and `tests/`;
- no code under `resources/` but the listed R metadata resolvers.

To place a new file:
1. Something the repository produces goes in `stores/`, or in `docs/` if it
   is a report.
2. Something a workflow reads as data goes in `resources/`.
3. Code a workflow imports or runs goes in `src/`, as a module run with
   `python3 -m`. A provider's acquisition script, in any language, goes in
   its `providers/<provider>/`.
4. A test goes in `tests/`, a report source in `docs/reports/`, and
   everything else in `tools/`.

A move never breaks a path that generation still reads: frozen inventory
provenance, the originals named there, and config inputs.
- Re-freeze under a new snapshot id, or leave the file where it is.
- An input whose content is unchanged may move with its recorded path, and
  the provenance pin that covers it, updated in place under the same snapshot
  id (ADR 0038).
- Any change to an input's content needs a new snapshot id.
- Paths recorded in `stores/` and in `generation-*.yaml` receipts are history
  and are never edited, and neither are the commands in a locked or
  preregistered record.

### `stores/`

One directory per Store Release, named by its opaque `OGS-` identifier
(ADR 0022): `release.yaml`, `build.yaml`, `analyses.tsv`, `validation.yaml`.
Simultaneously the output of Manifest Generation and the fixed input of Store
Release Build. Also holds candidate releases (`status: candidate`), so what is
under consideration and what has been built appear in one list.

A bundle is immutable once accepted. A material change to membership or to the
Build Recipe is a **new** Store Release (ADR 0004), not an edit. Correcting
Analytical Metadata after publication is a Release Erratum.

This is the *only* home for a Release Bundle. The family-first archive that
once sat beside it held bundles in the pre-ADR-0022 schema and is gone (#252):
each of its bundles was either registered here under its own `OGS-` id or
retired, and git history keeps the retired ones. Nothing writes a bundle
anywhere else.

### `src/ogstores/`

Store Release Build: `bundle.py` (load and check a bundle), `plan.py` (Bundle
to argv -- the seam), `paths.py`, `run.py`, `index.py`, and the release entry
point's modules. It is a package of its own rather than part of `workflow/`
because Manifest Generation depends on the Release Bundle contract too: a
generator validates what it emits with `bundle.check()`. It also holds two
read-only command-line views of the registry, run with `python3 -m`:
`release_readiness` (the Store Release readiness view) and
`audit_indexed_subsets` (`pixi run audit-indexed-subsets`).

Store Release Build imports nothing from Manifest Generation. Manifest
Generation imports from `ogstores` the Release Bundle contract (`bundle`) and
the registry layout it shares with Store Release Build: Store id and Artifact
Root paths (`paths`) and the master-list index (`index`).
`tests/packaging/test_boundary.py` enforces both directions.

### `src/manifest_generation/`

Manifest Generation (ADRs 0035 and 0036), installed editable like
`ogstores`. Its root holds the shared pipeline: the candidate workflow
(`candidate_workflow.py`), its entry point (`generate_candidate.py`, run as
`pixi run generate-candidate` or `python3 -m manifest_generation.generate_candidate`),
the release config it runs (`release_config.py`), the shared freeze over a
Source Collection's connector files (`freeze.py`), Source Inventory preflight
(`source_inventory.py`, `inventory.py`),
the Resolution Area where Resolution Records are kept once per Source
Collection (`resolution_area.py`), the one emitter (`emit.py`), Manifest
Generation's rules (`generation_rules.py`) and the helpers they share.
`check_bundles.py` (`pixi run bundle-check`) holds every committed bundle to the
contract and to those rules; it lives here because Store Release Build may not
import them.

A candidate run's working files are in two places outside Git. The Work Root,
`<output.work_root>/<store-id>/`, is one Store Release's: its preflight report,
resolver manifest, log, resolver index and resolution receipt. The Resolution
Area, `<resolution-root>/<source_collection_id>/`, is the Source Collection's:
every Selection resolves into it and reuses its records, and each receipt names
the records its bundle was decided from. The resolution root is deployment
configuration, resolved in the Artifact Root's order: the run's
`--resolution-root`, then `OPENGWASDB_RESOLUTION_ROOT`, then `ogstores.yaml`'s
`resolution_root`, then the built-in default. Store Release Build reads neither
(`tests/packaging/test_boundary.py`).

`providers/<provider>/` holds one Source Provider's connector code, each an
importable subpackage: acquisition and download scripts, metadata adapters,
and generators specific to that provider. Its non-Python scripts sit beside
the Python: GWAS Catalog's studies table (`ebi-studies.r`) and
re-materialisation (`materialise-gwas-ssf-ragged.R`), and OpenGWAS's GWAS-VCF
download. A provider that is inconsistent across its collections gets one
subpackage per Source Collection (ADR 0036): dbGaP is a host rather than one
collection, so the MVP (phs002453) acquisition and generator are
`providers/dbgap/mvp/`.

Everything here is imported as a module, never loaded by file path or through
`sys.path` (`tests/packaging/test_imports.py`). Run a module with
`python3 -m manifest_generation.<module>`.

### `src/curation/`

Canonical Trait Mapping Table curation (#161, #164): candidate retrieval, the
chooser, resumable curation rounds and promotion into
`resources/reference-resources/canonical-trait-mapping-efo/`. It runs in its
own pixi environment, outside the `main` solve group, so its dependencies
cannot change what a Manifest Generator or a build resolves
(`pixi run -e curation <task>`).

### `workflow/`

Snakemake wiring. `Snakefile` is Store Release Build's DAG: it scans `stores/`
and wires dependencies, nothing else, and `release.py` is its entry point
(`pixi run release`). Per ADR 0023 it contains no source column name, no
manifest translation, and no layout branch -- each rule asks `ogstores.plan`
for a Step and hands it to `ogstores.run`.

`generate.smk` (Manifest Generation) lives here too, as coarse wiring over
`python3 -m manifest_generation.generate_candidate`. The two share no DAG: the accepted
bundle is a boundary only because a human froze it, and one graph spanning both
would silently regenerate a bundle and rebuild a Store when a generator config
changed. `workflow/` is also Snakemake's own namespace (`Snakefile`, `rules/`,
`scripts/`, `envs/`), which is why no package lives here.

### `resources/reference-resources/`

Auxiliary build-time inputs that are **not** the Source Collection of any
Store Release (ADR 0011): LD reference panels, reference allele-frequency panels,
ancestry-mixture references, QC panels, the Canonical Trait Mapping Table, and
the SomaScan target tables. Each carries a `resource.yaml` declaring kind,
ancestry, genome build, variant ID convention, and location. The scripts that
build or verify one are tooling, in `tools/reference-resources/<resource_id>/`
or, for the LD panels, `tools/ld-panel/`.

Small tables may be tracked here. Large panels live under the artifact root and
are referenced by path -- this repository is not an artifact store (ADR 0015).

### `resources/annotations/`

Curated metadata that may change *after* a Store Release is published without
changing the analytical asset -- Trait Annotations principally.

The distinction is deliberate and load-bearing. Re-curating a Trait Annotation
does not create a new Store Release; correcting Analytical Metadata, which
changes how the statistics are interpreted, does.

### `resources/inventories/`

Frozen Source Inventories with their provenance and, since #283, the selected
rows of the connector files they were frozen from. Generation reads them, never
a live source, so a change is a reviewed re-freeze under a new snapshot id,
never an edit.

### `resources/generators/`

Manifest Generation's inputs. `<generator>/` holds a generator's release
configs, reviewed tables, `generation-*.yaml` receipts and README; recorded
config paths and receipts resolve here, so they do not move. The code that
reads them is in `src/manifest_generation/`.

Every source's connectors and release config are on the shared pipeline
(ADR 0036), so the only code left here is the two R metadata resolvers under
`lib/metadata_resolvers/` that curation's R-resolver round-trip still sources.
`tests/packaging/test_layout.py` lists each with the ticket that retires it.
That is the code's migration, not a collection's: a collection's move onto the
pipeline is not finished until its full comparison run against the accepted
bundle is made and every difference explained, and two are still pending --
FinnGen's (`OGS-00017`) and ukb-b's (`OGS-00009`).

A generator's only output is a bundle directory. It never builds a Store, and
it invokes statistics rather than implementing them: if two Store Families
computing something differently would be a bug, it belongs in `opengwasdb`.

### `tools/`

Repository tooling that belongs to neither workflow and is not an input: the
LD-panel toolchain (`ld-panel/`), the release benchmarks and the proofs that
write `docs/benchmark-output/` (`benchmarks/`), the Reference Resources' build
and verify scripts (`reference-resources/<resource_id>/`), the environment
check (`env_check.py`, `pixi run env-check`), the site build (`build-site.sh`,
`pixi run docs`), and one-off analyses and spikes. None of it is a package:
each is run as a script, and the few tests that load one by path are listed in
`tests/packaging/test_imports.py`. If one starts being run as a step in
producing a Store Release, it belongs in `src/manifest_generation/` or
`workflow/`.

### `tests/`

One directory per suite, and `run_all_tests.py`, the runner behind
`pixi run test` (see Checks).

### `docs/`

Decisions (`adr/`), specifications (`spec/`), and the site GitHub Pages
serves. The sources of its rendered reports (`*.qmd`, `make-dashboard.r`) are
in `reports/`; `pixi run docs` renders each and copies it to the name the
site publishes, so a published link does not change when its source moves.

### `.github/`

CI, in `workflows/ci.yml`. It installs from the committed `pixi.lock` and
runs `env-check`, the suites, `bundle-check`, the index regeneration (which
must leave the tree clean) and the docs smoke test. Each suite that builds
Stores through the whole release stack runs on a runner of its own (see
Checks).

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
pixi run test --suite workflow   # one named suite (repeatable)
pixi run --environment docs docs-smoke
```

`pixi run test` finds the Python suites itself (#257): every
`tests/**/test_*.py` and `tests/**/run_tests.py` is one. It is named after its
directory when that holds no other Python suite, when it is a `run_tests.py`,
or when the file is named after the directory; otherwise
`<directory>/<file>`, without `test_` and with `-` for `_`, which is also the
fallback when an R suite already has the directory's name. Adding one needs no
list edit; an R suite is listed in `tests/run_all_tests.py`.
Repository code imports from the installed package: nothing inserts the
repository root or `src/` into `sys.path` or hands them to a subprocess
through `PYTHONPATH`. `tests/packaging` enforces both rules.

Every suite runs side by side with the rest, with single-threaded BLAS and a
Snakemake working directory of its own (`OGSTORES_TEST_SNAKEMAKE_DIRECTORY`),
so no test touches the checkout's `.snakemake/`. The slowest suites
(`SHARDED_SUITES` in the runner) are spread over up to one process per job
(`--jobs`), each repeating the fixtures of the tests it runs. `pixi run test
--list-release-stack` names the suites that build Stores through the whole
release stack. CI gives each of them a runner of its own and runs everything
else with `--skip-release-stack`.

`bundle.check(bundle, registry_root=...)` is the executable Release Bundle
contract. It returns a list of every registry-side error it can find (an empty
list means valid) and never raises for invalid bundle content. The returned
list also carries a second channel, `.tolerated`, naming the suppressions the
#134 legacy exemption, the #286 exemption for OGS-00011's non-CURIE
`trait_ontology_id` values (an accepted bundle is never rewritten), and a
bundle Store Release Build will not build deliberately tolerate -- with
their count and citation -- so a caller can surface the gap rather than read a
clean `[]` that silently erased it (issue #142). It reads only
files inside Release Bundles, plus a parent bundle when resolving
`derived_from`; it never opens a Store or inspects a Release Artifact. The
`bundle-check` task discovers every bundle under `stores/` and fails if any
error is returned, from the contract or from Manifest Generation's own rules
(`src/manifest_generation/generation_rules.py`, #250), and CI runs that task
explicitly. In particular, it rejects
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
