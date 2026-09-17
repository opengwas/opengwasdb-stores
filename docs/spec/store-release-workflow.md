# Store Release workflow

How an accepted Release Bundle becomes a validated Store Release.

Governed by [ADR 0022](../adr/0022-flat-opaque-store-ids.md) (flat opaque store IDs) and [ADR 0023](../adr/0023-the-registry-store-seam-is-a-command-line.md) (the seam is a command line). Everything below follows from ADR 0023's single rule:

> The only thing this repository computes is an `opengwasdb` command line.

This document specifies Phase A (bundle to store). Phase B (what produces a bundle) is at the end, and is deliberately out of scope until Phase A is small and boring.

## Repository layout

```text
stores.tsv                      generated  master list, one row per Store Release
STORES.md                       generated  human view of the same
stores/
  OGS-00042/
    release.yaml                           identity, label, family, status, lineage, provenance
    build.yaml                             the recipe
    analyses.tsv                           membership; opengwasdb owns the schema
    validation.yaml          written back  merged evidence from the run
  by-label/                  generated     finngen-r13-pilot-20 -> ../OGS-00042
src/ogstores/                              bundle.py plan.py paths.py manifest.py run.py index.py
workflow/Snakefile                         Phase A: scans stores/, wires every release
workflow/generate.smk                      Phase B: acquisition + generation (separate DAG)
resources/families.yaml                    Store Family records (ADR 0024)
resources/reference-resources/<id>/        resource.yaml
resources/annotations/                     post-release curated metadata
resources/generators/<family-id>/          Phase B
resources/scripts/                         toolchains and repository tooling
tests/
```

Artifacts live outside git, at a path that is a pure function of the store ID:

```text
/data/opengwasdb/stores/OGS-00042/
  source/                    acquired or filtered source files
  work/                      checkpoints, scratch, logs
  work/analyses.tsv          derived build manifest: the bundle's analyses.tsv
                             with exclude_from_build rows removed
  work/analyses.exclusions.json   audit sidecar naming the dropped rows
  records/<step>.json        one per executed step
  store.opengwasdb           the Store Release
  store.opengwasdb.partial   transient staged destination
/data/opengwasdb/stores/by-label/   generated symlinks
```

## `release.yaml`

Registry identity and provenance. Never read by `opengwasdb`.

```yaml
store_id: OGS-00042
label: r13-pilot-20                 # source-natural display name; not an identifier
family: finngen-r13
status: built                       # candidate|accepted|built|validated|superseded|withdrawn
source_collection_id: finngen-r13
association_coverage: full_gwas
derived_from: ~                     # the parent's store_id for a completed release
created_at: '2026-08-18T08:51:59Z'
generator:                          # how the bundle was produced (Phase B)
  command: Rscript resources/generators/finngen-r13/generate.R --config=config-pilot-20.yaml
  version: sha256:1800b9cf...
build_environment:
  repo_commit: ...
  opengwasdb_rev: d6e5de7...
  pixi_lock_sha256: ...
notes: |
  ...
```

### Status lifecycle and transitions

A Store Release progresses through an explicit status state machine:

```text
candidate ──> accepted ──> built ──> validated ──> superseded
    │              │          │            │            │
    └──────────────┴──────────┴────────────┴────────────┴──> withdrawn
```

- `candidate` — Phase B output under evaluation; `validation.yaml` optional; unresolved rows tolerated. Transitions to: `accepted`, `withdrawn`, `superseded`.
- `accepted` — Frozen as build input for Phase A. Transitions to: `built`, `withdrawn`, `superseded`.
- `built` — Built by `opengwasdb`; `store.opengwasdb` produced. Transitions to: `validated`, `superseded`, `withdrawn`.
- `validated` — Passed `opengwasdb validate`; terminal release step complete. Transitions to: `superseded`, `withdrawn`.
- `superseded` — Replaced by a newer Store Release. Transitions to: `withdrawn`.
- `withdrawn` — Retracted/withdrawn release (terminal).

## `build.yaml`

The recipe, and the only input to `plan()`. Every value is either a registry fact or an `opengwasdb` flag.

```yaml
store_id: OGS-00042
layout: dense                       # dense | ragged | hybrid
completion_state: observed_only     # observed_only | reference_completed

build:
  command: build-dense-vcf          # an opengwasdb CLI subcommand; never a Python path
  options:                          # opengwasdb flag names, passed through verbatim
    source-reader-capability: opengwasdb.finngen-r13
    source-assembly: hg38
    n-workers: 8
    chunk-variants: 1000

post:
  top_hits: true
  rho: false                        # dense only; opengwasdb rejects otherwise
  overview: true                   # Dense/Hybrid only; Ragged's envelope excludes overview.html
  validate: true

artifacts:
  root: /data/opengwasdb/stores
```

A Reference-Completed release is the same file with a `complete` block instead of `build`. It carries no parent path: the parent is `release.yaml`'s `derived_from`, and its artifact path is a pure function of that ID.

```yaml
store_id: OGS-00043
layout: dense
completion_state: reference_completed

complete:
  command: complete-dense
  options:
    ld-panel: /data/opengwasdb/reference/ld/hgdp1kgp-hg38
    ancestry: EUR
    n-workers: 16
```

### The passthrough rule

`options` keys are `opengwasdb` flag names. `plan()` renders `key: value` as `--key value`, booleans as bare flags, and never interprets a key. The registry does not know what `--min-cor` means and must not acquire a copy of `opengwasdb`'s parameter schema.

An unrecognised or invalid flag fails in the CLI argument parser in milliseconds, before any expensive work. That is the intended validation mechanism: the schema is enforced once, where it is defined.

`plan()` composes only the arguments that are the registry's own facts — store identity, the derived build manifest path, and artifact paths. Everything else comes from `options`.

### Retired: `builder.entrypoint`

The previous `builder.entrypoint: opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest` named an internal module and function. It encoded the seam violation in the data model, and forced typed dispatch and per-layout adapters. Migration is mechanical: entry point to subcommand, one lookup table.

This also removes the "catalogue-routed" special case — `build-hybrid-from-catalogue` is another `command:` value, not another code path.

## Store identity passed to `opengwasdb`

```text
--store-id   <family>      finngen-r13
--release-id <store_id>    OGS-00042
```

The built store's `manifest.json` then reads `store_id: finngen-r13, release_id: OGS-00042` — a human-readable family plus the globally unique registry key, so a store found on disk joins back to its registry record without a lookup table.

## Phase A never writes `analyses.tsv`

> Anything that writes a column into `analyses.tsv` belongs to Phase B.

This settles where Ancestry Assignment and effect-scale/phenotype-SD estimation run: both produce shared-core columns -- `assigned_ancestry`, `ancestry_assignment_method`, `ancestry_prop_*` from the first; `original_sd`, `original_sd_method`, `stored_effect_scale` from the second -- so both run before acceptance, in Phase B.

Running them at build time would make Phase A mutate its own input, and the accepted bundle would stop being fixed input. It would also make the bundle stop determining the store: the same bundle built twice against a refreshed Ancestry Reference Panel would assign different ancestries. Frozen into `analyses.tsv` in Phase B, the assignment is checksummed, diffed in a pull request, and the build is a pure function of the bundle.

Effect-scale validation is additionally an *acceptance* decision rather than a build step. `finngen-r13/r13-pilot-20` is the worked case: effect-scale validation failed for `HEIGHT_IRN` (implied phenotype SD 0.665 against declared-standardised provenance) while the Store itself built and passed `opengwasdb validate`, and the pilot recorded a NO-GO. A check that can veto a release has to run before thirteen hours are spent, not after.

Phase A keeps a cheap residual role: `bundle.check()` asserts these columns are present and vocabulary-valid, delegating to `opengwasdb.model.analyses`. Asserting presence is registry-side structural validation; recomputing the values is not.

The one thing Phase A writes is the **derived build manifest** under the artifact root (`work/analyses.tsv`), which drops `exclude_from_build` audit rows. It is not the bundle's `analyses.tsv` and it writes no column back into the bundle: the accepted input stays byte-identical, and the derived file is a projection of the registry's own selection decision, not a recomputation of Analytical Metadata (ADR 0025).

## `src/ogstores/` — five modules

### `bundle.py`

```python
@dataclass(frozen=True)
class Bundle:
    store_id: str
    root: Path              # stores/OGS-00042/
    release: dict           # release.yaml
    build: dict             # build.yaml
    analyses_path: Path

def load(store_id: str, registry_root: Path) -> Bundle: ...
def check(
    bundle: Bundle,
    previous_status: str | Bundle | None = None,
    registry_root: Path | str | None = None,
) -> list[str]: ...
```

`check` covers registry-side facts only: required identity and provenance keys,
`store_id` matches the directory name and both YAML documents, ID format,
declared bundle files exist, checksum syntax, `derived_from` resolves to a
registered Store Release, Release Status vocabulary and optional transitions,
and `analyses.tsv` parses via `opengwasdb.model.analyses.read_analyses`. It
delegates the Analysis schema rather than reimplementing it. For BESD builds,
the non-empty `source_snapshot.besd_prefix` introduced by `908797f` is checked
as frozen provenance metadata, but the referenced BESD files are not inspected.

The return value is a list of every error found in one pass; an empty list means
the bundle is valid. Invalid or malformed bundle content is diagnostic data and
never makes `check` raise. It reads only files within Release Bundles (including
a registered parent needed for lineage resolution), never opens a Store, never
stats a source path, and never inspects any Release Artifact. The
`pixi run bundle-check` task discovers and checks every directory under
`stores/`, and CI runs that gate explicitly so a newly committed bundle cannot
bypass the contract.

### `plan.py` (~200 lines)

```python
@dataclass(frozen=True)
class Step:
    name: str               # build|complete|top-hits|rho|overview|validate
    argv: list[str]
    inputs: list[Path]
    outputs: list[Path]

def plan(bundle: Bundle) -> list[Step]: ...
```

A pure function that turns one release's `build.yaml` into the command lines needed to build it. Nothing more.

Given the `build.yaml` above it returns four `Step`s, each holding an argv plus the files that step reads and writes:

```python
[Step(name="build",    argv=["opengwasdb", "build-dense-vcf",
                             "/data/opengwasdb/stores/OGS-00042/work/analyses.tsv",
                             "/data/opengwasdb/stores/OGS-00042/store.opengwasdb",
                             "--store-id", "finngen-r13", "--release-id", "OGS-00042",
                             "--source-reader-capability", "opengwasdb.finngen-r13",
                             "--source-assembly", "hg38",
                             "--n-workers", "8", "--chunk-variants", "1000"], ...),
 Step(name="top-hits", argv=["opengwasdb", "build-dense-top-hits", "<store>"], ...),
 Step(name="overview", argv=["opengwasdb", "regenerate-overview",  "<store>"], ...),
 Step(name="validate", argv=["opengwasdb", "validate",             "<store>"], ...)]
```

Internally it is a lookup table -- `("dense", "observed_only")` to `build-dense-vcf`, `("ragged", "reference_completed")` to `complete-ragged` -- plus about fifteen lines per entry assembling positional arguments, plus a renderer turning `options` into flags without reading them.

It reads `build.yaml` and `release.yaml` and nothing else: no I/O beyond path construction, no store opened, no `analyses.tsv` row read. So it is deterministic and testable by string comparison, and it is the only place in this repository that knows how to invoke `opengwasdb` -- which is why the Snakefile's rules and the master list's `build_command` are two renderings of one thing and cannot disagree.

This is the entire adapter layer. It replaces `workflow/phase.py`, `workflow/model.py`, `release_plan.py`, `release_manifest.py`, `metadata_resolution.py` and `catalogue_coverage.py` (~4,100 lines on PR #105).

`paths.py` also names the derived build manifest. The `analyses` token — positional for Dense/Hybrid/Ragged-SSF and the `--analyses` flag for Ragged BESD — resolves to `<artifact-root>/<store_id>/work/analyses.tsv`, and the step's declared `inputs` name it too, so the workflow builds the manifest before the builder runs. The bundle's own `analyses.tsv` never appears in a build argv. Completion (`complete-*`) commands consume only a parent Store and take no analyses manifest.

### `manifest.py` (~150 lines)

```python
def materialise_build_manifest(source_path, manifest_path, sidecar_path) -> BuildManifestResult: ...
```

Materialises the derived build manifest: it reads the bundle's `analyses.tsv`, drops every row whose `exclude_from_build` is `true`, preserves every other column and the surviving row order, re-densifies `analysis_index` `0..n-1` when that column exists, and writes the manifest and its exclusion-audit sidecar atomically. It fails loudly on a malformed exclusion value, an all-excluded or header-only manifest, or a missing `analysis_id` column. Per ADR 0025 the registry enforces this decision here rather than teaching `opengwasdb` a registry-only audit column; the Snakefile calls this module and carries no filtering logic itself.

### `paths.py` (~70 lines)

Artifact layout as pure functions of the store ID. No other module constructs an artifact path.

### `run.py` (~120 lines)

Executes one `Step`: runs the argv, captures stdout/stderr/timing/exit status, writes `records/<step>.json`, and enforces the two safety rules below. It does not read the step's output back, interpret it, or re-validate it.

## Safety

**Tracked outputs are record files, not store directories.** Snakemake handles directory outputs poorly, and a half-written store must never satisfy a rule.

**Staged release transaction lifecycle.** A release executes entirely against `store.opengwasdb.partial` across all steps: `build` or `complete` creates `store.opengwasdb.partial`, and every mutating post-step (`top-hits`, `rho`, `overview`) as well as `validate` operates directly on that staged `.partial` path. Only upon successful terminal validation/finalization is `store.opengwasdb.partial` published (atomically renamed) to the final `store.opengwasdb` path. If any step fails or is interrupted, `store.opengwasdb.partial` is retained for debugging or resumption, and any pre-existing final Store and `validation.yaml` remain completely untouched without needing whole-Store copying. `force=True` replacement applies only at this terminal publication moment.

**`validation.yaml` is written only by the terminal `register` step.** A failed run leaves the previous one intact.

## `workflow/Snakefile` (~110 lines)

One Snakefile for the whole registry, not one per release. It scans `stores/*/` at parse time and wildcards on `store_id`, so Snakemake's own expansion *is* the multi-release runner -- there is no separate batch script.

Dependency wiring only, per ADR 0023. It contains no family name, no source column name, no manifest translation, and no layout branch:

```text
build_manifest ──> build ──> top_hits ──> rho ──> overview ──> validate ──> register
```

`build_manifest` is the derived build manifest's rule (ADR 0025). It runs before every observed-only build command, because `plan()` names the manifest it writes as the build step's first input. A Reference-Completed release substitutes `complete` for `build` and takes the supported tail; completion consumes only a parent Store, so it depends on no manifest step.

Post-steps are conditional on `post` and on the selected command's Store-format support: `rho` is Dense-only, and `overview` is Dense/Hybrid-only because the documented Ragged envelope excludes `overview.html`. A Reference-Completed release substitutes `complete` for `build` and takes the supported tail. Each rule's shell is the `Step`'s argv via `run.py`; each rule's output is the step's record file.

**Lineage ordering is why the DAG spans every store rather than one.** A Reference-Completed release declares its parent's `register` record as an input, resolved from `release.yaml`'s `derived_from`. A per-release workflow driven by a batch loop would have to sequence parents before children by hand, and would get it wrong. Here it is a declared edge.

Resumption is Snakemake's, over the record files. `complete-dense` additionally owns a checkpoint directory and a separate `complete-dense-resume` entry point, but the workflow does not yet select it: a resume must be invoked by hand, and `register` accepts the substitution when it sees it. Wiring this into the workflow needs a decision about how a resume participates in the `.partial` staging transaction, which `rewrite_argv_for_staging` currently rejects.

Scanning every store means DAG construction is proportional to the registry, which is immaterial at twenty stores and worth revisiting past a few thousand. The `index` target is unaffected either way: it depends only on bundle files, never on store artifacts, so refreshing the master list never proposes a build.

### Operator interface

```sh
pixi run release OGS-00003             # one registered release, plus any parent it depends on
pixi run release OGS-00003 OGS-00004   # several registered releases; lineage order is resolved
pixi run release-family finngen-r13    # every release of one family
pixi run index                         # regenerate stores.tsv, STORES.md, by-label/
```

A release target must be an ID currently registered under `stores/`; the
operator finds valid IDs in `stores.tsv`. IDs in these commands are real
targets, not placeholders. All four are targets of the same Snakefile.

> **Production note**: Workflow tests (`tests/workflow/`) are fixture-scale and run
> in temporary environments. Full production runs (such as building all seven Trial
> Store Releases via `pixi run release OGS-00001 OGS-00002 OGS-00003 OGS-00004 OGS-00005 OGS-00006 OGS-00007`)
> require raw source and reference preflight to confirm external data exists before
> execution, and must not be run until those inputs are verified.

## The master list

`stores.tsv`, `STORES.md` and both `by-label/` trees are **generated** by the `index` rule, committed, and verified in CI by regenerating them and failing if the tree is dirty. Authority stays with each store directory, which is self-describing; everything else is a view that cannot go stale.

**`index` reads git, never the artifact root.** That is the whole constraint, and it is narrower than it first appears. It does not mean the master list is limited to bookkeeping; measurements are welcome, they just have to land in the bundle when the build happens rather than be scraped off disk whenever someone runs the indexer.

Two kinds of column, and they are not in tension:

| | examples | drifts? | so |
|---|---|---|---|
| derived | `store_id`, `label`, `family`, `layout`, `status`, `build_command` | yes, if hand-maintained | regenerate from bundles; CI checks |
| observed | `n_variants`, `n_analyses`, `n_associations`, store size, `format_version`, elapsed, validate verdict | no -- facts about an event that happened once | `register` writes them into the bundle at build time |

Observed values cost nothing to collect: `build-dense-vcf` already prints `{n_variants, n_analyses}` and `complete-dense` already prints `{n_imputed, elapsed_s}`. `register` puts them in `validation.yaml`, git records them, and `index` reads them from there -- so CI can still regenerate the entire file, observed columns included. The numbers also become reviewable in a pull request diff rather than being whatever the disk said last time.

This subsumes `docs/store-catalog.md`, which today says of itself that its per-store numbers are a stale compilation from a date months earlier. A generated `STORES.md` cannot be stale.

The only thing deliberately excluded is anything whose answer changes without a commit -- whether a store still exists on disk, whether it is still readable. That is monitoring, not registry.

`stores.tsv` columns:

```text
derived   store_id  label  family  layout  completion_state  status  derived_from
          store_uri  created_at  opengwasdb_rev  generator_command  build_command
observed  format_version  n_analyses  n_variants  n_associations  store_bytes
          build_elapsed_s  validate_status
```

Two commands per store matter, and they are different kinds of thing. The **generator command** (`inventory.tsv` + config to bundle) is recorded by the generator into `release.yaml`. The **build command** (bundle to store) is *derived by `plan()`*, the same function the workflow renders its rules from. A hand-maintained list would be wrong within a month.

### Planned and executed argv are different facts

`plan()` is rendered two ways, and they carry different amounts of the truth:

```text
build.yaml
    |
    +--> plan()                     pure: -> [Step(name, argv, inputs, outputs)]
          |
          +--> stores.tsv           renderer 1: one row, the build command
          +--> Snakefile rules      renderer 2: one rule per Step
                    |
                    +--> run.py     adds: .partial -> rename, record write, timing
                    +--> snakemake  adds: resumption, resource limits,
                                          cross-store lineage dependencies
```

So `stores.tsv` records the **planned** argv, and it is not by itself evidence that anything ran, nor that what ran matched. Those are separate facts with separate checks:

| | what it is | exists | checked by |
|---|---|---|---|
| planned argv | `plan(build.yaml)` -> `stores.tsv` | before any build | CI: regenerate, fail if the tree is dirty |
| executed argv | `run.py` -> `records/<step>.json`, with exit status, timing, and the `opengwasdb` rev actually used | after each step | `register`: compare against planned, fail on mismatch |

That closes the loop with one comparison, and catches two failure modes CI alone cannot: a step run by hand with different flags, and a `build.yaml` edited after the build. The one legitimate divergence is `complete-dense-resume` substituted for a planned `complete-dense`; `register` accepts that specific pair and records `resumed: true` rather than reporting drift.

### No per-store `commands.sh`

An earlier draft generated a runnable `stores/<id>/commands.sh` per release. It is deliberately not in this design.

It was justified on portability -- rebuild without this repository -- but a script is not what makes a rebuild portable. The inputs are, and they are hundreds of gigabytes of source files and LD panels under the artifact root. Its other benefit, a visible diff in every command line when `plan()` changes, is already delivered twice: by `stores.tsv`'s `build_command` column, and by the golden-argv test in `tests/`.

A per-store command *log* is still wanted, but for Phase B rather than Phase A, and for the opposite reason -- see below.

## `validation.yaml`

Assembled by `register` from the step records: the JSON each build command already prints, plus `opengwasdb validate`'s verdict. `register` also compares each record's executed argv against `plan()`'s planned argv and fails on drift, per "Planned and executed argv are different facts" above. It records; it does not judge. This repository does not decide whether a store is scientifically sound — it captures what `opengwasdb` reported and who accepted it.

## Tests

The checks cover the things this repository is responsible for:

1. **Every Release Bundle satisfies `bundle.check()`.** The CI gate discovers
   bundles dynamically, reports all errors per bundle, and accesses no Store or
   Release Artifact.
2. **`plan()` argv is correct.** Golden argv per store, ~5 steps each, no fixture stores required.
3. **Conditional branches and resumption.** Rho off, no completion child, partial record sets produce the right step set.
4. **A failed step cannot damage a live store or a good `validation.yaml`.**
5. **Records merge into `validation.yaml` correctly**, and `register` fails when a record's executed argv differs from the planned argv — except for the `complete-dense-resume` substitution, which it accepts and records.
6. **The builder never sees an excluded row.** `tests/manifest/` asserts the regression directly: the manifest a planned build step consumes is the derived file, and an `exclude_from_build: true` `analysis_id` is absent from it while the bundle keeps the audit row. It also covers re-densified `analysis_index`, preserved columns and order, pass-through, and each loud failure mode.

Fixture-scale end-to-end runs stay, as *one* smoke test. Source formats, store contents, layouts, queries and scientific invariants are tested once, in `opengwasdb`.

## Upstream dependencies

The upstream prerequisites on `opengwasdb` `dev` (merged in PR #178 / epic opengwasdb#177) and pinned in this repository via #106:

| Issue | Status | Role in Store Release workflow |
|---|---|---|
| [opengwasdb#170](https://github.com/opengwas/opengwasdb/issues/170) | Closed on `dev` | Dense and Hybrid releases — canonical `analyses.tsv` names |
| [opengwasdb#172](https://github.com/opengwas/opengwasdb/issues/172) | Closed on `dev` | Ragged SSF releases — canonical `analyses.tsv` names (`sample_size`, `source_file`) |
| [opengwasdb#173](https://github.com/opengwas/opengwasdb/issues/173) | Closed on `dev` | BESD releases — `--analyses` Analytical/Attribution Metadata overlay |
| [opengwasdb#174](https://github.com/opengwas/opengwasdb/issues/174) | Closed on `dev` | Dense/Hybrid `--source-reader-capability` and `--source-assembly` CLI defaults |
| [opengwasdb#175](https://github.com/opengwas/opengwasdb/issues/175) | Closed on `dev` | Machine-readable `--format json` in `validate` and `info` for `validation.yaml` |
| [opengwasdb#176](https://github.com/opengwas/opengwasdb/issues/176) | Closed on `dev` | Phase B — `estimate-phenotype-sd` CLI over canonical manifests |

All layouts (Dense, Hybrid, Ragged SSF, and BESD) are unblocked on `opengwasdb@dev`. The active pin in `pixi.toml` ([opengwasdb-stores#106](https://github.com/opengwas/opengwasdb-stores/issues/106)) brings these capabilities into the workspace environment.

## Phase B — what produces a bundle

Not designed yet. Four rules fix its boundary now, so Phase A is not built against a moving target; everything inside that boundary is open.

### Phase A and Phase B are separate workflows

They meet at the accepted Release Bundle and share no DAG. Phase B may well use Snakemake too -- acquisition is genuinely DAG-shaped, with per-file downloads, checksums and filtering -- but as `workflow/generate.smk` with its own entry point.

The reason is not that one graph would be complex. It is that **the accepted bundle is a boundary only because a human froze it.** Span both phases with one DAG and Snakemake will correctly, silently, regenerate a bundle and rebuild a 71 GB store because a generator config changed upstream. The acceptance gate stops existing, and with it the property the whole of Phase A rests on: that its inputs are fixed.

Their outputs also live in different places and are reviewed differently. Phase B writes into git and is reviewed in a pull request; Phase A writes to the artifact root and is reviewed through `validation.yaml`. And their iteration shapes are opposite: a generator is rerun twenty times while selection is tuned, a build is run once for thirteen hours.

### Why Phase B needs a recorded command log and Phase A does not

| | how the commands are known | so the record is |
|---|---|---|
| Phase A | *derived* from a declarative `build.yaml` by `plan()` | regenerable, CI-checkable, verified against `records/` |
| Phase B | *not derivable* -- family-specific imperative code calling several scripts in sequence | must be **recorded as it runs** |

`release.yaml` currently holds `generator: {name, version, command}` -- a single command string, which is wrong as soon as generation calls acquire, select, assign-ancestry, estimate-SD and emit in turn. Phase B's design has to replace it with the executed sequence. That is the per-store script that Phase A does not need, and it is a log rather than a prediction.

### The four fixed rules

**Phase B is a separate workflow, meeting Phase A at the accepted bundle** — above.

**Phase B owns every `analyses.tsv` column, including Ancestry Assignment and effect-scale resolution** — see "Phase A never writes `analyses.tsv`", and "Who implements the statistics" below.

**A generator's only output is a bundle directory. It never builds a store.** The four copy-pasted `resources/generators/lib/source-formats/*/build-store.py` adapters exist only because nothing else could reach a builder; under ADR 0023 nothing but the workflow may.

**Acquisition is separate from selection.** Acquisition is per Source Collection, shared across families, and is the expensive resumable part. Selection is per Store Release. The Source Collection is a grouping string on the family record, not a directory (ADR 0024).

```text
resources/families.yaml        one entry per family; names the Source Reader
                               Capability and the Source Collection

resources/inventories/<id>.tsv discovered upstream analyses, once acquisition
                               produces one at scale (ADR 0024). Does not exist
                               yet -- every collection's inventory was null.

resources/generators/<family-id>/
    README.md                  the exact commands
    config-<label>.yaml
    generate.R|py              inventory + config -> stores/OGS-xxxxx/

resources/generators/lib/                  shared helpers
resources/generators/lib/source-formats/   Source-Format-scoped generation code
```

The entry point is family-scoped, matching `CONTEXT.md`'s definition of a Manifest Generator; the library is source-format-scoped, so families sharing a Source Collection share selection code without a configuration system by accident. This resolves the old `resources/generators/<source-format>-<layout>/` naming collision, where one directory served two families and grew a configuration system to tell them apart.

A generator has the same shape as the build workflow: discover upstream, select rows, shell out to `opengwasdb` for the statistics, write the bundle.

### Who implements the statistics

Phase B must *interpret* source statistics — it cannot emit a meaningful `analyses.tsv` otherwise. The line is not between interpreting and not interpreting; it is between deciding and computing:

> If two Store Families computing something differently would be a bug, `opengwasdb` implements it. Otherwise Phase B does.

| | example | owner |
|---|---|---|
| which Analyses, and which method tier applies | FinnGen's endpoint categories; choosing `estimated_from_beta_distribution` for a source with no AF | Phase B |
| the computation | phenotype-SD estimation, allele alignment to a reference, AF extraction at sites | `opengwasdb` |
| the acceptance policy | the SD-disagreement tolerance, and whether a failure blocks the release | Phase B |

There is no catch-22 requiring a Store to exist first. `opengwasdb.readers.interface` already declares source reading for pre-build annotation — "extracting allele frequency and standard error at a requested set of sites for annotation (ancestry assignment, phenotype-SD estimation)" — and `assign-ancestry` is the working example: a raw source manifest in, a resolved `SourceReader` per row, annotated metadata out, no Store involved. `opengwasdb.build.phenotype_sd.estimate_phenotype_sd` likewise already implements every computed ADR-0029 tier, and already specifies that method selection is caller-supplied rather than inferred.

Deferring instead -- building first and correcting the Store afterwards -- is not available. `stored_se = original_se / original_sd` is applied at write time, so a Store built without the SD holds wrong standard errors, and there is no rescale operation: Stores are immutable (ADR 0004, ADR 0007) and Reference Completion writes a new one. It would also mean building every candidate to discover which are unusable, when `GCST002047`'s odds-ratio beta column and `GCST003566`'s inverted EAF are both detectable from the source.

That CLI wiring is provided upstream by `estimate-phenotype-sd` ([opengwasdb#176](https://github.com/opengwas/opengwasdb/issues/176)), now available on `dev` and active via #106; Phase B must invoke the upstream CLI command rather than keeping local estimation shims or per-family connectors. The previous `resources/generators/lib/phenotype_sd_estimate.py` was a 47-line shim taking JSON arrays on the command line, which forced each family to open source files and extract `se`/`af`/`beta` itself; that is how `resources/generators/lib/effect_scale_validation.R` grew to 562 lines, of which roughly 60 are the acceptance policy that genuinely belongs here and the rest re-implements reference-panel access and allele harmonisation `opengwasdb` already owns.
