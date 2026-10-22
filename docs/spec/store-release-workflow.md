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
    release.yaml                           identity, label, status, lineage, provenance
    build.yaml                             the recipe
    analyses.tsv                           membership; opengwasdb owns the schema
    summary.yaml              generated    review view derived from analyses.tsv
    validation.yaml          written back  merged evidence from the run
  by-label/                  generated     finngen-r13-pilot-20 -> ../OGS-00042
src/ogstores/                              bundle.py plan.py paths.py manifest.py run.py index.py register.py release.py
workflow/Snakefile                         Phase A: scans stores/, wires every release
workflow/release.py                        Phase A operator entry point (pixi run release)
workflow/generate.smk                      Phase B: coarse wiring for one candidate (issue #153)
resources/reference-resources/<id>/        resource.yaml
resources/annotations/                     post-release curated metadata
resources/generators/<family-id>/          Phase B
tools/                                     repository tooling, neither a package nor an input (ADR 0038)
tests/
```

Artifacts live outside git, at a path that is a pure function of the store ID:

```text
<artifact-root>/OGS-00042/
  generate/                  Manifest Generation's work root: a legacy
                             generator's working bundle (#248)
  source/                    acquired or filtered source files
  work/                      checkpoints, scratch, logs
  work/analyses.tsv          derived build manifest: the bundle's analyses.tsv
                             with exclude_from_build rows removed
  work/analyses.exclusions.json   audit sidecar naming the dropped rows
  records/<step>.json        one per executed step
  store.opengwasdb           the Store Release
  store.opengwasdb.partial   transient staged destination
  records.before-force-<UTC>/   transient: a forced run's copy of records/
  publication.json           transient: a publication in progress (#195)
<artifact-root>/.release-locks/<id>.lock   one release's run lock (#195)
  replaced/<UTC>/            archive of a release a forced run replaced:
                             store.opengwasdb, records/, validation.yaml;
                             a completion phase's keeps no store.opengwasdb
<artifact-root>/by-label/   generated symlinks
```

`/data/opengwasdb/stores` is the built-in default root; the workflow resolves the real root from configuration (see "Artifact root comes from configuration").

## `release.yaml`

Registry identity and provenance. Never read by `opengwasdb`.

```yaml
store_id: OGS-00042
label: r13-pilot-20                 # source-natural display name; not an identifier
access_posture: public
status: built                       # candidate|accepted|built|validated|superseded|withdrawn
derived_from: ~                     # a two-id completed child's parent (ADR 0007); none when a release completes its own Store
created_at: '2026-08-18T08:51:59Z'
source_snapshot_id: finngen-r13-manifest-2026-05-12
generator:                          # how the bundle was produced (Phase B)
  version: sha256:1800b9cf...
  commands:                         # executed command log, in run order
    - Rscript resources/generators/lib/source-formats/finngen-r13-dense/generate.R --config=resources/generators/finngen-r13/config-pilot-20.yaml --mode=emit
notes: |
  ...
```

The file trims to identity, lineage, Release Status, creation time, source
snapshot identity, the generation command log, and prose (issue #136, ADR 0029).

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

**Buildable statuses** (#247). Phase A builds a bundle only when its Release Status is `accepted`, `built` or `validated`. `accepted` is the human-frozen input the build rests on; `built` and `validated` stay buildable so that a re-run, or a `--config force=1` replacement, still goes through. `candidate`, `superseded` and `withdrawn` are refused, as is a bundle with no valid status. The rule lives in the Release Bundle contract (`bundle.BUILDABLE_STATUSES`, asked through `bundle.is_buildable`, `bundle.build_refusal` and `bundle.partition_buildable`); the Snakefile's `all` target, its `onstart` check and the release entry point all ask it rather than restating it. A candidate's refusal says how to accept it; a superseded or withdrawn release never returns to `accepted`, so a replacement is a new Store Release.

#### The Release Status a Validation Record gives

`register` writes one of four verdicts. Which of them gives `validated` was decided in review on 7 Oct 2026 (#195), and `bundle.check()` enforces it: a `validated` release whose `validation.yaml` status is anything else fails `bundle-check`.

| Validation Record `status` | Release Status | Reason |
|---|---|---|
| `passed` | `validated` | `opengwasdb validate` passed with nothing to report. |
| `passed_with_warnings` | `validated`; the warnings stay in the record | `validate` exits 0 with `ok: true`. Its warnings are non-blocking by definition, because `errors` is the blocking list. ADR 0023 has this repository record `opengwasdb`'s verdict rather than re-judge it. The warnings stay visible: the record lists them, and the master list publishes `validate_status: passed_with_warnings`. A warning that should block belongs in `opengwasdb` as an error. |
| `failed` | never `validated` | A failed validate step publishes nothing, so `register` writes no record for it. |
| `not_run` | never `validated` | There is no verdict. |

A `built` release may carry any of the four. Moving it to `validated` remains a reviewed edit to `release.yaml`.

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
  overview: true                   # Dense/Hybrid only; Ragged's envelope excludes overview.html
```

`post` carries only the choices a Release makes differently from the defaults:
`rho: false` and `validate: true`. `plan()` applies those defaults when a recipe
omits them, and a recipe that states either key still wins. `top_hits` and
`overview` are always explicit because they genuinely vary between releases --
`overview` especially, since Ragged's closed Store envelope excludes
`overview.html` and defaulting it on would re-enable a step the planner then
refuses (ADR 0027, issue #128).

A Reference-Completed release completes its own Store under its id (ADR 0037). It is the same file plus a `complete` block, with `completion_state: reference_completed` and no `derived_from`: the `build` block builds and publishes the Observed-Only Store, and the `complete` block then completes that published Store and replaces it. The recipe names no Store path, because completion's source is the id's own Store path. `bundle.check()` requires both blocks of such a bundle and names the one that is missing. `plan()` plans it only for a Dense layout (`plan.py` below).

```yaml
store_id: OGS-00043
layout: dense
completion_state: reference_completed

build:
  command: build-dense-vcf
  options:
    source-reader-capability: opengwasdb.finngen-r13
    source-assembly: hg38

complete:
  command: complete-dense
  options:
    ld-panel: /data/opengwasdb/reference/ld/hgdp1kgp-hg38
    ancestry: EUR
    n-workers: 16
```

A two-id Reference-Completed child, the form from before ADR 0037, has a `complete` block instead of `build`, and its parent is `release.yaml`'s `derived_from`, whose artifact path is a pure function of that ID. Existing two-id bundles (OGS-00002, OGS-00022, OGS-00023) stay valid; nothing new should take that form.

### Artifact root comes from configuration

The Build Recipe says *what* to build; configuration says *where* it lands. An accepted bundle is immutable, so an absolute artifact path inside one would bind that bundle to the machine that created it and hardcode that machine's filesystem into the master list's `build_command`.

`paths.artifact_root()` therefore resolves the root in precedence order:

```text
1. workflow config override   pixi run release OGS-00042 --config artifact_root=/path
2. environment variable       OPENGWASDB_ARTIFACT_ROOT=/path
3. repository config file     ogstores.yaml: artifact_root
4. built-in default           paths.DEFAULT_ARTIFACT_ROOT
```

A workflow config override and the environment variable are per-invocation overrides for CI, a developer laptop, or a one-off run. Name the Store Release ids before `--config`, because it takes every word after it as a `key=value`. The `release` and `release-dry` tasks pass those words to Snakemake as given; they once ended in a bare `--`, which made Snakemake read the override as a target (#195). The tracked `ogstores.yaml` names this deployment's default root and is the reviewed place to change it. `plan()` never reads the root from `build.yaml`: the workflow resolves it once and passes it in, and the build command published in the master list is rendered under the same resolved root.

### Temporary files stay on the artifact root

Every step's subprocess runs with `TMPDIR`, `TMP` and `TEMP` set to `<artifact root>/<store id>/tmp` (`paths.tmp_dir`, set once in `run.step_env`), and the entry point's own Snakemake run uses `<artifact root>/.tmp` (`paths.entry_point_tmp_dir`). `opengwasdb` spills through `tempfile`, which follows `TMPDIR`, and `/tmp` is on a small root filesystem: OGS-00009's release died with `No space left on device` that way (#312). Both directories are created when missing, with no configuration. Their contents belong to the step that wrote them, which removes them; nothing here deletes the directory. A step killed mid-run (a signal, or `ENOSPC`) leaves its spill there until it is removed by hand (#316).

### The passthrough rule

`options` keys are `opengwasdb` flag names. `plan()` renders `key: value` as `--key value`, booleans as bare flags, and never interprets a key. The registry does not know what `--min-cor` means and must not acquire a copy of `opengwasdb`'s parameter schema.

An unrecognised or invalid flag fails in the CLI argument parser in milliseconds, before any expensive work. That is the intended validation mechanism: the schema is enforced once, where it is defined.

`plan()` composes only the arguments that are the registry's own facts — store identity, the derived build manifest path, and artifact paths. Everything else comes from `options`.

### Retired: `builder.entrypoint`

The previous `builder.entrypoint: opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest` named an internal module and function. It encoded the seam violation in the data model, and forced typed dispatch and per-layout adapters. Migration is mechanical: entry point to subcommand, one lookup table.

This also removes the "catalogue-routed" special case — `build-hybrid-from-catalogue` is another `command:` value, not another code path.

## Store identity passed to `opengwasdb`

```text
--store-id   <store_id>    OGS-00042
--release-id <store_id>    OGS-00042
```

An observed-only built store's `manifest.json` therefore reads
`store_id: OGS-00042, release_id: OGS-00042`. The Store Release has one
identifier and a store found on disk joins directly back to its registry record.
`plan()` does not read Store Family for any purpose. Reference Completion takes
only the child `--release-id` and preserves the source Store identity, as defined
by the upstream completion interface.

The Release Bundle's source-natural `label` is display/provenance metadata, not
identity. The current `opengwasdb` CLI cannot populate the manifest's free-form
provenance mapping with it; [opengwasdb#181](https://github.com/opengwas/opengwasdb/issues/181)
tracks that upstream capability. Until it exists, the label stays in the Release
Bundle and generated views rather than being placed in either identity field.

## Phase A never writes `analyses.tsv`

> Anything that writes a column into `analyses.tsv` belongs to Phase B.

This settles where Ancestry Assignment and effect-scale/phenotype-SD estimation run: both produce shared-core columns -- `assigned_ancestry`, `ancestry_assignment_method`, `ancestry_prop_*` from the first; `original_sd`, `original_sd_method`, `stored_effect_scale` from the second -- so both run before acceptance, in Phase B.

Running them at build time would make Phase A mutate its own input, and the accepted bundle would stop being fixed input. It would also make the bundle stop determining the store: the same bundle built twice against a refreshed Ancestry Reference Panel would assign different ancestries. Frozen into `analyses.tsv` in Phase B, the assignment is checksummed, diffed in a pull request, and the build is a pure function of the bundle.

Effect-scale validation is additionally an *acceptance* decision rather than a build step. `finngen-r13/r13-pilot-20` is the worked case: effect-scale validation failed for `HEIGHT_IRN` (implied phenotype SD 0.665 against declared-standardised provenance) while the Store itself built and passed `opengwasdb validate`, and the pilot recorded a NO-GO. A check that can veto a release has to run before thirteen hours are spent, not after.

Store Release Build keeps a cheap residual role: `bundle.check()` asserts the shared required columns are present through `opengwasdb.model.analyses`, and `assigned_ancestry` is vocabulary-valid wherever it appears. That `assigned_ancestry` is present is Manifest Generation's own rule (`src/manifest_generation/generation_rules.py`, #250), which `bundle-check` runs beside the contract. Asserting presence is structural validation; recomputing the values is not.

The one thing Phase A writes is the **derived build manifest** under the artifact root (`work/analyses.tsv`), which drops `exclude_from_build` audit rows. It is not the bundle's `analyses.tsv` and it writes no column back into the bundle: the accepted input stays byte-identical, and the derived file is a projection of the registry's own selection decision, not a recomputation of Analytical Metadata (ADR 0025).

## `src/ogstores/` — the modules

The package is shared by both phases. Its module surfaces are `bundle.py`,
`plan.py`, `paths.py`, `manifest.py`, `run.py`, `index.py`, `register.py`,
`resources.py`, `runtime.py`, `release.py` and `readiness.py`;
`_pdeath_supervisor.py` is an internal helper `run.py` uses to contain a
detached build's process group (ADR 0026).

### `bundle.py`

```python
@dataclass(frozen=True)
class Bundle:
    store_id: str
    root: Path              # stores/OGS-00042/
    release: dict           # release.yaml
    build: dict             # build.yaml
    analyses_path: Path
    validation: dict | None # validation.yaml, or None when absent

def load(store_id: str, registry_root: Path | str | None = None) -> Bundle: ...
def check(
    bundle: Bundle,
    previous_status: str | Bundle | None = None,
    registry_root: Path | str | None = None,
) -> list[str]: ...
def summarise(bundle: Bundle) -> dict[str, str | int]: ...
```

`check` covers registry-side facts only: required identity and provenance keys,
`store_id` matches the directory name and both YAML documents, ID format,
declared bundle files exist, checksum syntax, a `derived_from` resolves to a
registered Store Release, a `reference_completed` bundle without one declares
both its `build` and `complete` blocks, Release Status vocabulary and optional transitions,
and `analyses.tsv` parses via `opengwasdb.model.analyses.read_analyses`. It
delegates the Analysis schema rather than reimplementing it, including rejecting
every column named by the pinned upstream `RETIRED_ANALYSIS_COLUMNS` list. Two
registry-owned vocabularies are checked on top of that delegation: every
non-empty `assigned_ancestry` must be one of the ancestry mixture's seven
super-population codes (a free-text Source Ancestry Label is not an Assigned
Ancestry), and `n_cases`/`n_controls` must be blank on a non-case-control
Analysis rather than `0` (issue #133). Both were silent-failure classes found
in the committed bundles; the checks make a reintroduction fail `bundle-check`
rather than a summary. For BESD builds,
the non-empty `source_snapshot.besd_prefix` introduced by `908797f` is checked
as frozen provenance metadata, but the referenced BESD files are not inspected.
An `artifacts` block is rejected: issue #126 made the artifact root deployment
configuration resolved by `paths.artifact_root()`, not part of a valid immutable
Release Bundle.

The return value is a list of every error found in one pass; an empty list means
the bundle is valid. Invalid or malformed bundle content is diagnostic data and
never makes `check` raise. It reads only files within Release Bundles (including
a registered parent needed for lineage resolution), never opens a Store, never
stats a source path, and never inspects any Release Artifact. The
`pixi run bundle-check` task discovers and checks every directory under
`stores/`, and CI runs that gate explicitly so a newly committed bundle cannot
bypass the contract.

`summarise` reads only the loaded bundle's `analyses.tsv` and derives admission
counts (manifest, admitted, excluded) plus first author, publication PMID,
tissue, context, assigned ancestry, sample size, and download source. Constants
remain verbatim, varying identifiers become `mixed (n)`, varying quantities
become `min-max`, and varying URLs become their common host/path prefix. A
missing column, an empty table, or any empty value renders as `NA`; zero is not
absence.

Admission is explicit (issue #217): `n_manifest` is every manifest row,
`n_admitted` the rows whose `exclude_from_build` is not `true`, and
`n_excluded` the rest, all parsed with the same strict classifier the build
manifest uses (`manifest.classify_exclusion`). `n_analyses` is the admitted
count -- a Store's membership -- so a derived view never presents an excluded
manifest row as a built Analysis. The index rule writes the result to
`summary.yaml` in every bundle and publishes the same values in the master
list. CI runs the rule and rejects any resulting dirty tree.

The summary does not fill a missing `source_url` from
`release.yaml:source_snapshot.besd_prefix`: `908797f` made that prefix frozen
BESD input provenance, not per-Analysis download metadata. Likewise,
`summary.yaml` is a registry review view, not an `opengwasdb` Store overview;
the `6092fee` rule that Ragged releases cannot generate `overview.html` remains
unchanged.

### `plan.py`

```python
@dataclass(frozen=True)
class Step:
    name: str               # build|complete|top-hits|rho|overview|validate, or completion-<step>
    argv: list[str]
    inputs: list[Path]
    outputs: list[Path]
    phase: str = "build"    # "completion" for the second phase of a single-id release
    published_store_arg: int | None = None  # argv index naming the id's published Store

def plan(bundle: Bundle, artifact_root: Path | str | None = None) -> list[Step]: ...
```

A pure function that turns one release's `build.yaml` into the command lines needed to build it. Nothing more.

Given the `build.yaml` above it returns four `Step`s, each holding an argv plus the files that step reads and writes:

```python
[Step(name="build",    argv=["opengwasdb", "build-dense-vcf",
                             "/data/opengwasdb/stores/OGS-00042/work/analyses.tsv",
                             "/data/opengwasdb/stores/OGS-00042/store.opengwasdb",
                             "--store-id", "OGS-00042", "--release-id", "OGS-00042",
                             "--source-reader-capability", "opengwasdb.finngen-r13",
                             "--source-assembly", "hg38",
                             "--n-workers", "8", "--chunk-variants", "1000"], ...),
 Step(name="top-hits", argv=["opengwasdb", "build-dense-top-hits", "<store>"], ...),
 Step(name="overview", argv=["opengwasdb", "regenerate-overview",  "<store>"], ...),
 Step(name="validate", argv=["opengwasdb", "validate",             "<store>",
                             "--format", "json"], ...)]
```

Internally it is a lookup table -- `("dense", "observed_only")` to `build-dense-vcf`, `("ragged", "reference_completed")` to `complete-ragged` -- plus about fifteen lines per entry assembling positional arguments, plus a renderer turning `options` into flags without reading them.

It reads `build.yaml` and `release.yaml` and nothing else: no I/O beyond path construction, no store opened, no `analyses.tsv` row read. The artifact root is not one of those reads -- it is deployment configuration, resolved by the caller with `paths.artifact_root()` and passed in (issue #126). So it is deterministic and testable by string comparison, and it is the only place in this repository that knows how to invoke `opengwasdb` -- which is why the Snakefile's rules and the master list's `build_command` are two renderings of one thing and cannot disagree.

A Build Recipe may also declare an optional `variant_reference` pre-build stage for the Dense-VCF and Hybrid builders (issue #145). `plan()` then prepends an `extract-variant-reference` Step and appends the artifact to the build step's inputs. The declared destination must equal the build step's `--variant-reference` option -- the one bounded, syntactic exception to ADR 0023's never-interpret rule -- but whether the artifact already exists is a runtime fact the executor decides when the step runs, never the planner (ADR 0031, issue #147).

A Build Recipe may declare optional named `indexed_subsets` (stores #209). `plan()` then emits one `build-indexed-subset` Step per declaration, in declaration order, after the Store and its inline index steps exist and before `overview` and `validate`. Each Step is an ordinary `Step`, so `run.py` gives it the usual durable `records/indexed-subset-<name>.json`. `plan()` resolves nothing: the declaration records the maintained `resource_id`, the concrete `variant_list` path and the `reference_assembly`, and the release entry point verifies the maintained resource and the runtime capability before a build. A Ragged release rejects any declaration rather than silently dropping it. See "Indexed Variant Subsets" below and [ADR 0034](../adr/0034-indexed-variant-subsets-in-release-planning.md).

A `reference_completed` release with no `derived_from` completes its own Store under its id (ADR 0037), and `plan()` returns two phases, build then completion:

- **Build phase:** exactly what the same bundle plans as `observed_only` without its `complete` block: the same step names, argv and record names, ending in `register`. This is what lets a Store built before the bundle gained its `complete` block be completed without a rebuild (#305).
- **Completion phase:** `complete`, then the declared post steps in their usual order, then `completion-validate`, then `completion-observed-view-check`, ending in `completion-register`. `complete` reads the id's published Store and writes the id's Store path. Both are the same canonical token, so the Step's `published_store_arg` names the argument that is the published Store. `complete` passes `--release-id <id>-completed` and no `--store-id`: the completed manifest inherits its `store_id`, the id, from its source (opengwasdb ADR 0011). The completion command builds the Top-Hit Index inline, so `post.top_hits` plans no completion-phase step; `rho`, `overview` and each declared Indexed Variant Subset do, as `completion-rho`, `completion-overview` and `completion-indexed-subset-<name>`.

`completion-observed-view-check` is the Observed-View Check (opengwasdb `check-observed-view`, [ADR 0037](../adr/0037-reference-completion-replaces-its-store.md) Decision 2): `opengwasdb check-observed-view <published Store> <staged Store> --seed 20261021 --n-random 1000 --output <work dir>/observed-view-check.json`. Its first Store argument is the published one (`published_store_arg` 2), which staging leaves alone, and the second the staged one, rewritten to `.partial`. The seed is one fixed default for every release, the ADR's benchmark seed; there is no `build.yaml` option for it. The verdict is the command's stdout, so it is the step's record; the same JSON is written to `work/observed-view-check.json`, not to `records/`. A non-zero exit fails the step, so `completion-register` never runs and the Observed-Only Store is not touched: exit 1 (a mismatch) is reported with the failing counters and their counts and the path of the verdict, exit 2 (a refusal, which prints no verdict) as a refusal with the command's stderr. Every such message names the verdict's path, `<artifact root>/<id>/work/observed-view-check.json`; the counts include the axis ones (`parent_rows_unmapped`, `order_preserved`, `analyses_identical`), and a stdout that holds no decodable verdict is reported as "verdict unreadable" with the reason and the start of stdout. Snakemake removes a failed job's record by contract, so a failed check leaves no record: its verdict is that file, and the fixed seed makes it exactly reproducible from the same staged Store. The evidence of a swap is the passing check's record: `completion-register` refuses to publish without it, and archives a copy of it with the swap (below). A runtime without the command refuses a release that completes its own Store before anything is written (`resources.verify_observed_view_check_for_releases`, called from the entry point's dry run and run and from the Snakefile's `onstart`, as the Indexed Variant Subset guard is), naming the command and the runtime pin.

Every completion-phase record name differs from every build-phase one, so both phases' records stay in `records/` once the completed Store replaces the Observed-Only Store. The first step keeps the name `complete`, so `register` still accepts a `complete-dense-resume` substitution for it. Only a Dense release is planned this way: `plan()` refuses another layout, naming it and ADR 0037, because the Observed-View Check that gates the replacement compares only Dense Stores so far. A two-id child (`derived_from`) plans exactly as before. The golden is `tests/plan/golden/OGS-00003-single-id-completed.json`: OGS-00003's recipe with a `complete` block, whose build phase is OGS-00003's own golden.

This is the entire adapter layer. It replaces `workflow/phase.py`, `workflow/model.py`, `release_plan.py`, `release_manifest.py`, `metadata_resolution.py` and `catalogue_coverage.py` (~4,100 lines on PR #105).

`paths.py` also names the derived build manifest. The `analyses` token — positional for Dense/Hybrid/Ragged-SSF and the `--analyses` flag for Ragged BESD — resolves to `<artifact-root>/<store_id>/work/analyses.tsv`, and the step's declared `inputs` name it too, so the workflow builds the manifest before the builder runs. The bundle's own `analyses.tsv` never appears in a build argv. Completion (`complete-*`) commands consume only a parent Store and take no analyses manifest.

### `manifest.py`

```python
def materialise_build_manifest(source_path, manifest_path, sidecar_path) -> BuildManifestResult: ...
```

Materialises the derived build manifest: it reads the bundle's `analyses.tsv`, drops every row whose `exclude_from_build` is `true`, preserves every other column and the surviving row order, re-densifies `analysis_index` `0..n-1` when that column exists, and writes the manifest and its exclusion-audit sidecar atomically. It fails loudly on a malformed exclusion value, an all-excluded or header-only manifest, or a missing `analysis_id` column. Per ADR 0025 the registry enforces this decision here rather than teaching `opengwasdb` a registry-only audit column; the Snakefile calls this module and carries no filtering logic itself.

### `paths.py`

Artifact layout as pure functions of the store ID. No other module constructs an artifact path. `artifact_root()` resolves the deployment root from configuration -- workflow override, `OPENGWASDB_ARTIFACT_ROOT`, the tracked `ogstores.yaml`, then the built-in default -- and every path builder takes that root as an argument (issue #126).

### `run.py`

Executes one `Step`: `execute_step()` runs the argv, captures
stdout/stderr/timing/exit status, writes `records/<step>.json`, and enforces the
two safety rules below; `run_step()` and `run_plan()` drive one and many steps.
It does not read the step's output back, interpret it, or re-validate it.

### `index.py`

Renders the generated views. `generate_index()` discovers every valid bundle
under `stores/`, writes each bundle's `summary.yaml`, regenerates `stores.tsv`
and `STORES.md`, and refreshes the tracked `stores/by-label/` symlink tree (and
the artifact-side `by-label/` tree when an artifact root is configured). It
reads git, `analyses.tsv`, and `validation.yaml`, never a Store or a Release
Artifact: it resolves the artifact root only to render derived paths
(`store_uri` from `paths.store_path()`, `build_command` from `plan()`), so the
two renderings cannot drift (ADRs 0022, 0030).

### `register.py`

`register_release()` assembles `validation.yaml` from the step records plus the
`opengwasdb validate` verdict, compares each record's executed argv against
`plan()`'s planned argv, harvests the observed measurements, and publishes the
staged Store. It is the only writer of `validation.yaml` (issues #119, #135;
ADR 0023). Publication is one recoverable transaction: `publication.json` is
written first, and `complete_publication()` finishes it, now or after a crash
(see Safety, #195).

### `release.py`

The supported operator entry point, run by `pixi run release` and
`pixi run release-dry` through `workflow/release.py` (#195). It allowlists
options, and around one Snakemake run it does four things: it completes
pending publications, refuses unsafe runs, snapshots what a forced run may
replace, and settles the run. See Safety.

### `amendment.py`

The one amendment an accepted Release Bundle allows (ADR 0037, #305).
`refuse_changed_bundles()` refuses a run of a bundle that completes its own
Store when it no longer plans the Store its release published, and names what
changed. See Safety, *The one amendment an accepted bundle allows*.

### `resources.py` and `runtime.py`

The run-time counterparts to the pure planner. `resources.py` verifies a
declared Indexed Variant Subset's maintained variant list against its
`resource_id` declaration. `runtime.py` (stores#218) is the pre-write runtime
guard: it proves the resolved `opengwasdb` is the tracked pin (from the
executable's own install metadata, failing closed on an unprovable identity),
provides every subcommand the *scheduled steps* name, and can complete each
parent a scheduled `complete` job consumes -- where a same-run `build`/`complete`
of that parent makes the stale on-disk artifact irrelevant. Both are called by
the entry point before the snapshot and by the Snakefile's `onstart` for a
direct `snakemake` run. See [the release runtime](../runtime.md).

## Safety

**Tracked outputs are record files, not store directories.** Snakemake handles directory outputs poorly, and a half-written store must never satisfy a rule.

**Staged release transaction lifecycle.** A release executes entirely against `store.opengwasdb.partial` across all steps: `build` or `complete` creates `store.opengwasdb.partial`, and every mutating post-step (`top-hits`, `rho`, `overview`) as well as `validate` operates directly on that staged `.partial` path. Only upon successful terminal validation/finalization is `store.opengwasdb.partial` published (atomically renamed) to the final `store.opengwasdb` path. If any step fails or is interrupted, `store.opengwasdb.partial` is retained for debugging or resumption, and any pre-existing final Store and `validation.yaml` remain completely untouched without needing whole-Store copying. `force=True` replacement applies only at this terminal publication moment. A release that completes its own Store (ADR 0037) stages its completion the same way: `complete` reads the published `store.opengwasdb` and writes `store.opengwasdb.partial`, and its post steps run there. Its planned argv names the same canonical token twice, so `rewrite_argv_for_staging` leaves the argument the Step marks as the published Store (`published_store_arg`) alone, requires it to be that token exactly, and rewrites the one other match. Any other step with two matching tokens is still refused as ambiguous. `register`'s drift check holds the marked argument to its planned token: a record whose published-Store argument names the staged path is drift, because the step read its own staged Store.

**The supported entry point** (#195, review round 1 of #196). Releases run through `pixi run release` and `pixi run release-dry`. Both are `workflow/release.py`, which runs `ogstores.release`. It wraps one Snakemake run in a guard that no Snakemake option can switch off, in five stages:

1. **Recovery.** It takes the run lock of each release it was asked for (below), then completes any publication a crash left half done (`publication.json`, below). A release whose lock another live run holds is left alone. It also puts back or archives any `store.opengwasdb.backup` left by `run.publish_store(force=True)`. This happens before any refusal, so a refusal never blocks recovery.
2. **Preflight.** A requested release whose bundle completes its own Store and no longer plans the Store it published is refused first, beyond the one amendment ADR 0037 allows (below). A requested release holding a leftover records snapshot is refused next, before the dry run, so an operator sees that refusal and its resolutions rather than the `IncompleteFilesException` the leftover would cause. A Snakemake dry run then lists the jobs, and the lock of every scheduled release is taken. The guard refuses a published release that would be rebuilt, unless it was named with `--config force=1` or every job scheduled for it completes its own published Observed-Only Store (below). It also refuses a release holding a leftover records snapshot, until an operator resolves it. Nothing has been written when either refusal happens. If no job is scheduled, it stops there without starting Snakemake.
3. **Snapshot.** For each forced, published release, `records/` is copied to `records.before-force-<UTC>/`, keeping mtimes.
4. **The run.** One Snakemake run, with `--config release_run=<UTC>` naming this run, and so any snapshot it took. Under that token a register job publishes but leaves `publication.json` in place.
5. **Settlement.** Each publication the run left is finished (`release.finish_publication`). Steps a killed job did not reach are completed. Snakemake's mark that a killed register job's output is incomplete is cleared. Only then is the marker removed. Then a failed run's snapshots are put back as `records/`, and a successful run's are archived.

**Only buildable bundles are built** (#247). Before any of the stages above, a registered release named as a target whose Release Status is not buildable (see *Buildable statuses*) is refused, before any lock is taken or anything is written; an unregistered id still gets the Snakefile's "Unknown Store Release ID" guidance. The refusal names the status and, for a candidate, how to accept it. `all`, and an invocation naming no release, means every registered release that is buildable; the entry point lists each one it leaves out, with the reason, and so does `--dry-run`. A named child can schedule its unbuilt parent, so once the dry run lists the jobs, every scheduled release is checked the same way, before the snapshot or any build work.

The entry point accepts only release ids or `all`, `--config key=value`, `--cores`, `--dry-run`, `--keep-going`, `--rerun-incomplete`, and `--resolve-snapshot restore|delete` (below). Anything else is refused before anything is written, including `--no-hooks`, `--touch`, `--forceall`, `--nolock`, `--ignore-incomplete` and `--snakefile`. `release_run` is reserved for the entry point. `--dry-run` writes nothing: it prints Snakemake's plan and what the guard would do.

**Two runs never touch one release at once** (review round 3 of #196). Each run holds an exclusive `fcntl.flock` on `<artifact-root>/.release-locks/<ID>.lock` for every release it touches, from recovery through settlement. `--resolve-snapshot` holds it too. The lock is per release, not per artifact root, because a release build can run for hours, and one lock per root would stop every other release meanwhile. Locks are only ever tried, never waited for, so two runs cannot deadlock. The lock file holds its holder's pid. It lives outside the release's own directory, so taking it never changes a file of a release that a refused run promises it left alone.

While another live run holds a release's lock, any run of that release is refused with "a run of `<ID>` is in progress (pid …)", and so is `--resolve-snapshot`. Recovery never completes or removes another live run's marker. The lock is passed to Snakemake's supervisor, so it stays held while Snakemake lives. The kernel drops it when every holder has died, SIGKILL included, and that is exactly what tells a live run from a dead one's leftovers. So the leftover-snapshot refusal, which asks an operator to resolve the snapshot, is given only when the lock is free. `--dry-run` reports a live run's holder without taking or writing anything.

**An interrupted run settles** (review round 3 of #196). The entry point runs Snakemake and its jobs in their own session, led by `run.py`'s parent-death supervisor (ADR 0026), so nothing outlives the entry point. On Ctrl-C or SIGTERM, the entry point ignores further interrupts and stops Snakemake, in three steps:
1. One SIGINT to the session, as a terminal's Ctrl-C would send, puts Snakemake in cancel mode, so it schedules nothing more.
2. A running job does not stop on that: a job process runs its `run:` block in a worker thread and waits for it, and a step's command runs in its own supervised session. So after two seconds the job processes, never Snakemake itself, are SIGKILLed, and their step supervisors kill the commands.
3. Snakemake, its jobs failed, exits on its own and releases its own lock.

Snakemake's own handling of SIGTERM would wait for running jobs to finish, which can take hours. The entry point then settles the run as failed. Any publication that had begun is completed. Otherwise the snapshot is restored as `records/`, byte for byte, and Snakemake's incomplete marks on the restored records are cleared, as `--resolve-snapshot` does. Settlement ignores further interrupts, and the exit code is 128 plus the signal: 130 for SIGINT, 143 for SIGTERM. The release is then exactly as it was before the forced run.

**Running `snakemake` directly bypasses the entry point.** The Snakefile keeps its checks as defence in depth, in its `onstart` hook. It refuses a run that would build a bundle whose Release Status is not buildable (#247), and its `all` target expands only to buildable bundles. It refuses a run of a bundle that completes its own Store and no longer plans the Store it published, unless the release holds the entry point's force-transaction snapshot. It refuses a run that would rebuild a published release, unless the release holds that snapshot or every job scheduled for it completes its own published Observed-Only Store. `--no-hooks` disables those checks (the `all` target still expands only to buildable bundles), and a direct run gets no recovery, snapshot, restore or settlement.

**Why the check runs before any job.** A fresh checkout is enough to rebuild a published release by accident: every bundle file is then newer than the records beside its published Store, so Snakemake schedules the whole chain again. Without the refusal, that run rebuilt into `.partial`, rewrote the release's `work/` and `records/`, and only then failed at publication. The check cannot run inside a job, because Snakemake deletes a job's existing outputs before running it. Two kinds of run are unaffected, because neither schedules a job for the published release: re-running an up-to-date release, and building a new Reference-Completed child of an up-to-date published parent. A third is let through: completing a release's own published Observed-Only Store (below).

**Completing a published Observed-Only Store** (#305, ADR 0037). Completion-phase jobs over a release's own published Observed-Only Store are its next step, not a rebuild, so they run without `--config force=1`. Which Store is published is read from records, never from the Store (ADR 0023). `records/register.json` without `records/completion-register.json` says the build phase has published and the completion phase has not. A job is the record it writes (`run.job_record`): its step's, its phase's register's, or none for `build_manifest`. A release is let through (`run.completing_own_stores`) only when it completes its own Store (`Bundle.completes_own_store`), its records say so, and every job scheduled for it writes a record of its plan's completion phase. Any build-phase job over a published Store, `build_manifest`, a build-phase step and the build phase's `register` included, is refused as before, with the same message. The same rule covers a completion interrupted in an earlier run and a Store published before its bundle declared completion.

An interrupted completion resumes rather than restarts. Snakemake reruns the missing or incomplete completion-phase record, as it reruns any step. `complete-dense` keeps a checkpoint beside the staged Store it writes, `<ID>/.store.opengwasdb.partial.checkpoint`, and refuses to start while one exists. Before `run.execute_step` starts a fresh `complete-dense`, it records the step's planned argv, atomically, in `<ID>/work/complete.started.json` (`paths.completion_start_path`), and it removes that record once a completion finishes. When the checkpoint exists, `execute_step` runs `opengwasdb complete-dense-resume <checkpoint>` in place of the planned `complete-dense` (`run.resume_argv_for_completion`), passing on a planned `--n-workers`, but only if the recorded argv is the one planned now. A different plan is refused, naming its changed tokens; so is a checkpoint with no record, which this workflow did not start, or an older one did. Either refusal runs nothing, keeps the checkpoint, and says that `--config force=1` discards it. Only stores' own planned argv is compared, so no `opengwasdb` option, checkpoint key or default is restated here (ADR 0023). `register` accepts the substitution and records `resumed: true`. Nothing but a forced run discards a checkpoint, a planned `--overwrite` included: a forced run's preflight removes the checkpoint and its record, because it rebuilds the Store the checkpoint was computed from, and then rebuilds both phases.

**The one amendment an accepted bundle allows** (#305, ADR 0037). An accepted Release Bundle is immutable, except that an `observed_only` bundle may gain a `complete` block and switch to `completion_state: reference_completed`. That is how a Store published before its bundle declared completion is completed under its id. Before any job runs, `amendment.refuse_changed_bundles` holds a bundle that now completes its own Store (`Bundle.completes_own_store`), and whose build phase has published, to the Store it published. The baseline is the published release's records, never its Store, and only what made that Store:
- once the build phase has published (`records/register.json`), its Store-writing steps, `variant-reference` if planned and `build`, must be the steps its register record registered (its `inputs`), each planned with the argv its own record planned (`planned_argv`, which `register` held the executed argv to). The manifest the bundle's `analyses.tsv` makes now must be byte-for-byte the one the Store was built from, `work/analyses.tsv`, and must exclude the same Analyses (`work/analyses.exclusions.json`). Neither published file is optional: a missing derived build manifest, and an exclusion audit sidecar that is missing, unreadable, or does not list its dropped Analyses by `analysis_id`, are each refused as a membership that cannot be compared, naming the file, never read as no exclusions. Only a missing register record means a phase has not published: one that exists but is not a readable JSON object is refused as a phase that cannot be compared, naming it, and so is such a step record;
- once the completion phase has published (`records/completion-register.json`), the same holds for its `complete`.

Post steps (`top-hits`, `rho`, `indexed-subset-<name>`, `overview`, `validate`, and their completion-phase counterparts) are re-runnable in-place additions (ADR 0034, opengwasdb ADR 0061), so a new declaration, or a planner that has changed their argv since, is not refused. Observed-only and two-id releases are not checked: they behave as before #305. Nothing enforces accepted-bundle immutability of `release.yaml`'s prose and provenance, for any bundle; review does. Gaining a `complete` block passes, because the build phase plans what the bundle planned as `observed_only`. A record names the artifact root it was written under (`record_path`), and the bundle is planned under that root for the comparison, so a release copied to another root still matches. The refusal lists each change:
- a step's changed tokens, with their flag, `` `--min-cor 0.0` became `--min-cor 0.5` ``;
- a Store-writing step planned but not published, or published but no longer planned;
- each Analysis added, removed, changed (naming its columns), newly excluded or no longer excluded.

It ends "Nothing was written". `--config force=1` keeps its meaning, so a forced release is not checked: a forced run may rebuild a named release from its bundle as it now stands. The entry point checks the requested releases and their `derived_from` parents before it takes any lock. A release another live run holds is not checked then, because that run may be rewriting its records. It is checked once this run holds its lock, before recovery or the dry run, or the run is refused as in progress; no entry-point run succeeds, "nothing to be done" included, without checking every such release it touches. A dry run names a release it could not check for that reason. The Snakefile's `onstart` checks the scheduled releases. So the entry point always refuses a changed bundle, but a direct `snakemake` run refuses it only when it schedules a job: one with nothing to schedule never reaches `onstart` and writes nothing, and, like every direct run, it has none of the entry point's recovery or settlement either.

**Replacing a published release** (decided in review on 7 Oct 2026):

```sh
pixi run release OGS-00005 --config force=1
```

Force reaches only the release ids named as targets. A published release scheduled only as a dependency, or through `all`, is still refused. The register job runs in a subprocess that re-parses the Snakefile, so it cannot see the targets. It replaces a published Store only when `release_run` names a snapshot that release holds, and only the entry point creates one.

**Publication is one recoverable transaction.** Once `register` has verified every record and assembled the new Validation Record, it writes `publication.json` beside the Store. The marker records the whole outcome: the archive, the snapshot, what becomes of a replaced Store (`replaced_store`: `archive` or `delete`), the new `validation.yaml` text, `register.json`, and for a replacement the replaced Validation Record. `complete_publication` then carries out the steps below. Each step checks what is already done, so a later run can repeat it safely:

1. For a replacement, the old Store is set aside as `.backup`.
2. The staged Store is renamed into place.
3. A forced run renames the old Store into `replaced/<UTC>/` (the snapshot's `<UTC>`). A completion phase deletes it instead (`replaced_store: delete`, below): it renames it into its archive as `store.opengwasdb.deleting`, in one step, then removes that tree. Removing a tree is not atomic, so a crash part-way leaves a tree under that name, and the next run removes the rest. An archive with neither that tree nor a `.backup` means the deletion is done.
4. The snapshot moves into the archive as `records/`. A completion phase has no snapshot; a copy of the replaced release's `records/register.json` is written to the archive's `records/` instead (below), and when it deleted the Store, a copy of the passing `records/completion-observed-view-check.json` too.
5. The replaced Validation Record is written into the archive. It is a copy, because the bundle's file is rewritten in place, and git keeps it.
6. The new `validation.yaml` and the phase's register record are written: `records/register.json`, or `records/completion-register.json` for a completion phase. The marker's `register_record` names which.
7. The marker is removed, last. Under the entry point the register job does not remove it (`finalize=False`). The entry point does, in `finish_publication`, once Snakemake has finished the job and any mark that a killed job left `register.json` incomplete is cleared with `snakemake --cleanup-metadata`. A direct `register_release()` removes its own marker.

A crash between any two steps, up to and including the marker's removal, therefore leaves either the marker or a release that is already consistent with no Snakemake mark outstanding. The next entry-point run finishes a pending marker before anything else. If only the register job dies, the same run's settlement finishes it. A state that fits no step, such as neither a Store nor a staged Store, raises `PublicationError` and keeps the marker for an operator. The new record's `replaced.archive`, and `register.json`'s `replaced_archive`, say where the archive is. A first publication uses the same marker, without the archive steps. A marker written before #309 has no `replaced_store`. A crash may have left one, so it is still read, and it archives, as it did when it was written.

**A release that completes its own Store is published twice** (ADR 0037). Its build phase's `register` publishes the Observed-Only Store exactly as any release's does. Its completion phase's `register`, `register_release(..., phase="completion")`, then replaces that Store through the same transaction, with four differences from a forced run:
- it needs no `force`: replacing the Store its build phase published is what it is for;
- it takes no records snapshot. Its records are named apart from the build phase's (`plan.py` above), so a failed or interrupted completion leaves the build phase's records untouched and the Observed-Only Store published. A snapshot would be restored on failure and throw away a part-finished completion's records. The next unforced run resumes it (*Completing a published Observed-Only Store*, below);
- its archive stamp is its own: this second's `<UTC>`, suffixed `-2`, `-3`, ... when that archive already exists, because a forced run replaces twice, once per phase, possibly within one second. The archive holds a copy of the replaced release's `records/register.json` and of the passing `records/completion-observed-view-check.json` under `records/`, and the replaced Validation Record. The originals stay in `records/` with the other records;
- it deletes the Store it replaces once the completed Store is published, rather than archiving it (ADR 0037, Decision 3, #309): the completed Store holds everything the replaced one held, so steady state keeps one copy, 33 GB less for UKB-B. So it publishes only over a passing Observed-View Check. Before it writes the marker, `register` reads `records/completion-observed-view-check.json` and takes the first JSON object on its stdout as the verdict (a step's output may be captured twice). No record, a stdout that holds no verdict, and any `passed` but `true` are each refused, naming the record, and nothing is changed. This is beside the rule that every planned step's record shows success. A forced run's completion phase archives the Store it replaces, as every forced replacement does. Its build phase's publication has already moved the run's snapshot into `replaced/<release_run>/`, so `register.forced_completion` reads the build phase's `records/register.json`: the run forced the release when that record's `replaced_archive` names this run's archive, and did not when it is null or another run's. Nor did it when the record has no `replaced_archive`: `register` has written the field since #195, so such a record predates #195, and a forced run would have rewritten it. Every Store published before #195 has one, and completing it under its own id (#305) meets it. A record that exists but is not a readable JSON object, and a `replaced_archive` that is neither a path nor null, are refused, naming the record, and nothing is changed: read as unforced, either would delete a Store a forced run archives.

Both phases run in one entry-point run, so the completion phase starts while the build phase's marker still stands: the entry point removes it only after Snakemake's last job. The completion phase's first step, `complete`, therefore finishes that publication itself, marker last, before it stages anything (`register.finish_build_phase_publication`, called from `run.execute_step` for a step that reads the published Store). A staged Store beside a pending first publication is a state `complete_publication` refuses, so it must never arise. This is safe at that point: `complete` consumes `records/register.json`, so Snakemake has already recorded the build phase's register job complete, and no mark that it is incomplete is outstanding. A crash before the marker is removed leaves it, with nothing staged, and the next run's recovery finishes it as any other. A pending publication of the completion phase itself is refused there, as everywhere.

**Before the marker, a crash leaves the old release in place.** The Store and `validation.yaml` are untouched, and the leftover snapshot makes the next run of that release refuse to start, forced or not (decided in review on 7 Oct 2026). The refusal names the snapshot and the two resolutions, which an operator chooses and the entry point carries out:
- `pixi run release <ID> --resolve-snapshot restore` replaces `records/` with the snapshot, when the killed run had already rewritten records;
- `pixi run release <ID> --resolve-snapshot delete` deletes it, when `records/` is known to describe the Store beside it.

Either resolution also clears Snakemake's marks that the killed run's outputs (the release's records and derived manifest) are incomplete. Otherwise a restored record would stop every later run with `IncompleteFilesException`. A whole-host crash also leaves Snakemake's lock on the working directory. If a later run reports it, check that no other run is active, then run `snakemake --snakefile workflow/Snakefile --unlock`. That writes nothing under a release.

**Nothing deletes an archive.** `publish_store(force=True)` renames the old Store into it rather than removing it, and so does crash recovery of a `store.opengwasdb.backup` found without a marker. An archive failure is raised, never swallowed. **A forced run's archive holds a full Store**, so replacing a release costs its size again in disk until a person deletes the archive: 2.7 GB for OGS-00005 and about 35 GB for OGS-00009; OGS-00010's was 50 GB before #312 retired it. Only a completion phase deletes a Store, the one it replaced, and only under its marker.

Tests inject crashes at each named boundary through `run.fault_boundary` and the test-only `OGSTORES_TEST_KILL_AT` and `OGSTORES_TEST_KILL_GROUP_AT` variables. The boundaries are the register job's (`register.PUBLICATION_BOUNDARIES`) and the entry point's (`release.ENTRY_POINT_BOUNDARIES`). `tests/register/test_register.py` kills or fails the publication transaction at every register-job boundary and completes it: a forced replacement at each boundary it crosses, and a completion phase's, which deletes the old Store, at each it crosses (`old-store-deleting` is part-way through the deletion). A boundary that neither kind is killed at fails that suite. `tests/workflow/test_release_faults.py` kills the whole stack (entry point, Snakemake and register job) through the supported entry point at every entry-point boundary, `marker-removed` included. It also does so at one register-job boundary of each kind Snakemake can tell apart: before the marker (`publication-started`), with the marker but no `records/register.json` (`store-published`), and with both (`register-record-written`). It also kills a release that completes its own Store while its completion phase is part-way through deleting the Observed-Only Store (`old-store-deleting`). It then asserts that the next run recovers, and fails if the code gains an entry-point boundary the suite does not crash. The same suite pauses a forced run once its build has started (`<step>-started` points in `run.execute_step`, with the test-only `OGSTORES_TEST_PAUSE_AT` and `OGSTORES_TEST_PAUSE_FILE` variables). It shows that a second run, a forced one, `--resolve-snapshot` and a dry run all see the live run's lock, and that the run then completes. It also shows that Ctrl-C, SIGTERM to the entry point, and SIGTERM to every process each restore the records byte for byte, mid-build. For a completion (#305), Ctrl-C mid-`complete` and SIGTERM to every process mid-`completion-validate` each leave the Observed-Only Store published and the build phase's records untouched, and the next unforced run resumes, `complete` from its checkpoint.

**Moving a published release to another Artifact Root** (#331). A step record names the root its step ran under: its `argv`, `planned_argv`, `inputs`, `outputs`, `record_path` and, for `register`, `published_store`. A release directory renamed into another root keeps its file times, so the workflow at the new root finds nothing to do, but the next `register` of its build phase compares those argv with the plan under the new root and refuses them as drift naming the old root. `tools/relocate_store.py` moves a release and its records together:

```sh
pixi run -e dev python3 tools/relocate_store.py OGS-00015 --from <old-root> --to /data/opengwasdb/stores --dry-run
```

- It renames `<old-root>/<id>` to `<new-root>/<id>`, so both roots must be on one filesystem. It never copies.
- In `records/*.json` it rewrites only those six fields. A string that is `<old-root>/<id>` or a path under it moves under `<new-root>/<id>`; nothing else changes.
- `stdout`, `stderr` and every other field stay as the step wrote them, and so does `work/analyses.exclusions.json`. They are the history of what ran, and nothing checks the paths in them.
- A rewritten record is written as `run` writes one and keeps its mtime, because the workflow decides what to rerun by the order of those mtimes. A record that would not be written back byte for byte is refused, with the difference.
- `<new-root>/<id>/relocations/<UTC>.json` is the receipt. It holds both roots, the commit of the tool, the Store id check, each record's sha256 before and after, how many strings each field had moved, and where the old root is still named, deliberately.
- It refuses, with nothing moved, while `publication.json` or a staged Store (`store.opengwasdb.partial*`) exists, when the target exists, the roots are on different filesystems, or a run holds the release's lock at either root. It also refuses when a record names an artifact path of another root or another release, as OGS-00017's still name OGS-00016's, or names the old root in a field it does not rewrite, and when `opengwasdb info` reports another `store_id`. Without `opengwasdb` on PATH that last check is skipped, and the output and receipt say so.
- It holds the release's lock at both roots from its last check to the receipt.
- `--dry-run` makes every check and prints what would change, writing nothing.

The workflow suite moves a published fixture release with the tool and shows the release at the new root has nothing to do and its next register passes. It also moves one with a plain rename and shows that register refuses it, naming the old root.

**`validation.yaml` is written only by the terminal `register` step.** A failed run leaves the previous one intact.

## `workflow/Snakefile`

One Snakefile for the whole registry, not one per release. It scans `stores/*/` at parse time and wildcards on `store_id`, so Snakemake's own expansion *is* the multi-release runner -- there is no separate batch script.

Dependency wiring only, per ADR 0023. It contains no source column name, no manifest translation, and no layout branch. `get_artifact_root()` is the one configuration read: it resolves the artifact root through `paths.artifact_root()`, so the workflow never takes it from a Build Recipe (issue #126).

```text
build_manifest ──> build ──> top_hits ──> rho ──> indexed-subset-<name> ──> overview ──> validate ──> register
```

`build_manifest` is the derived build manifest's rule (ADR 0025). It runs before every observed-only build command, because `plan()` names the manifest it writes as the build step's first input. A two-id Reference-Completed child substitutes `complete` for `build` and takes the supported tail; completion consumes only a parent Store, so it depends on no manifest step.

A release that completes its own Store (ADR 0037) runs both phases in one run. Its build phase is the chain above. Its completion phase then starts from the build phase's `register` record, because it completes the Store that phase published:

```text
register ──> complete ──> completion-rho ──> completion-indexed-subset-<name> ──> completion-overview ──> completion-validate ──> completion-observed-view-check ──> completion-register
```

Completion-phase steps share the `step` rule, under their own `{step}` values. `completion-register` is a rule of its own, `completion_register`, which calls `register_release(..., phase="completion")`, passing `force` only in a forced run (`register.forced_completion`, below). `all` and the id's target alias depend on that record, so naming the id runs both phases. The existing rules' bodies, and the inputs of every build-phase job, are exactly what they were before ADR 0037. Snakemake's default rerun triggers compare a rule's code and a job's input set with what it recorded, so a Store built before then is not rescheduled when its bundle gains a `complete` block (#305).

Post-steps are conditional on `post` and on the selected command's Store-format support: `rho` is Dense-only, and `overview` is Dense/Hybrid-only because the documented Ragged envelope excludes `overview.html`. A declared `indexed_subsets` list inserts one `build-indexed-subset` Step per declaration between those inline index steps and `overview`/`validate`, so overview reports the subsets and validation audits them. `rho` defaults off and `validate` defaults on, so a Build Recipe names only the steps it chooses differently (ADR 0027, issue #128). A two-id Reference-Completed child substitutes `complete` for `build` and takes the supported tail. Each rule's shell is the `Step`'s argv via `run.py`; each rule's output is the step's record file.

### Indexed Variant Subsets

An optional named Indexed Variant Subset (stores #209, epic #214) is a
Dense-only derived acceleration artifact defined by opengwas/opengwasdb ADR
0061. It is built from a maintained canonical-ALID variant list:

```text
opengwasdb build-indexed-subset STORE SUBSET_NAME \
  --variant-list hm3.alid.txt --reference-assembly GRCh38
```

A Build Recipe declares the subset in a top-level `indexed_subsets` list, and
`plan()` emits one Step per declaration before `overview` and `validate`. The
Step's record is the ordinary durable record every Step gets, so planned and
executed argv, inputs, outputs, stdout/stderr, timing, success and the
`opengwasdb` revision used are all captured; `register` requires the record and
compares executed against planned argv like any other Step.

**Runtime prerequisite.** The capability first shipped in opengwas/opengwasdb
`4819a2ee` (opengwas/opengwasdb#264), a descendant of the format-0.2.0 builders.
The repository's tracked current-format runtime now provides it (stores#218; see
[the release runtime](../runtime.md)), so a subset-declaring release and every
other supported release run on the same pinned executable. Before a run that
declares a subset, `ogstores.release` (and the Snakefile `onstart` hook, for a
direct `snakemake` run) still:

1. probes the resolved `opengwasdb` for `build-indexed-subset` and refuses with
the required capability when it is absent;
2. resolves each declaration's `resource_id` under
`resources/reference-resources/<id>/resource.yaml` and hashes the declared
`variant_list` against the maintained sha256, byte size and row count.

Both refusals happen before the snapshot or any build work. `plan()` never reads
a resource. Separately, `ogstores.runtime.verify_runtime_for_releases` refuses a
run whose resolved runtime is not the tracked pin, is missing any subcommand a
scheduled plan names, or would complete a parent Store that is not format
0.2.0. A subset-declaring release is format 0.2.0 and its Validation Record
names the runtime commit that ran, which is the tracked pin for every supported
run. This is opt-in per release in the sense that no Store is converted or
rebuilt by declaring or not declaring a subset.

**Registry migration (stores#211, epic #214).** Every existing registered
Release Bundle with a Dense Component now declares the default `hm3` subset:
`OGS-00003`, `OGS-00004`, `OGS-00005`, `OGS-00008`, `OGS-00009`,
`OGS-00011` and `OGS-00017` (and `OGS-00010`, retired by #312). The four Ragged-only bundles (`OGS-00001`,
`OGS-00002`, `OGS-00006`, `OGS-00007`) are unchanged. The declaration is
planning metadata only: it adds no Analysis and changes no `release.yaml`
status, lineage or `analyses.tsv`, so it mints no new Store Release and rebuilds
no primary data; building the derived namespace for an already-published Store
is a deliberate capability-runtime run, not part of this migration.
`ogstores.audit_indexed_subsets` (`pixi run
audit-indexed-subsets`) prints the full in-scope list and refuses a
Dense-component bundle that is neither migrated nor explicitly opted out
(`indexed_subsets: []`), and a Ragged-only bundle that declares a subset. It
reads only `build.yaml` and the small resource declaration, so it runs in CI
without the reference root. Its scope is every registered Release Bundle under
`stores/`, which since #252 is the only home for one.

### HM3 benchmark reporting

An Indexed Variant Subset is an optional derived artifact, so a routine release
benchmark must be able to report a Store that has none without failing. That is
the contract of opengwas/opengwasdb#272's shared
`benchmarks._query_shapes.measure_hm3_extract`, and the registry-side report is
`tools/benchmarks/release-benchmark-report.py` (stores#212, epic #214):

```text
pixi run -e dev python <stores>/tools/benchmarks/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb --all --hm3-only \
  --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
  --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md
```

The report measures every registered Store's `hm3` record only through that
core seam and records the core status verbatim: `measured` carries the timing
and result count, and `missing`, `not_applicable` (Ragged-only) and
`unsupported` are non-failing operational statuses. It never times a query or
reimplements classification. Where a per-Store anchor exists
(`tools/benchmarks/release-benchmark-anchors.yaml`) the core generic harness
surfaces the seven query shapes beside HM3; otherwise the shapes are marked
`not_configured` and the run continues. Because the tracked pin predates the
capability, the report is run against an explicit `--opengwasdb-checkout` and
records that checkout's git revision and the imported package's fingerprint.

**HM3 benchmark absence is operational evidence, not a Store validation
failure.** None of `missing`, `not_applicable` or `unsupported` is a
`bundle.check()` error, a Validation Record verdict, or a Release Status
change; a Store with no `hm3` subset is still a valid Store. The one loud case
is a subset that is *present and structurally valid* whose extraction raises:
the core helper propagates it, and the report does not downgrade it to
`missing`, because a broken index reported as absent is a wrong answer that
looks like a right one.

**Lineage ordering is why the DAG spans every store rather than one.** A two-id Reference-Completed child declares its parent's `register` record as an input, resolved from `release.yaml`'s `derived_from`. A per-release workflow driven by a batch loop would have to sequence parents before children by hand, and would get it wrong. Here it is a declared edge.

Resumption is Snakemake's, over the record files. `complete-dense` additionally owns a checkpoint directory and a separate `complete-dense-resume` entry point. `run.execute_step` selects it when an interrupted `complete` left its checkpoint and the plan it started from is the plan now (Safety, *Completing a published Observed-Only Store*), and `register` accepts the substitution.

Scanning every store means DAG construction is proportional to the registry, which is immaterial at twenty stores and worth revisiting past a few thousand. The `index` target is unaffected either way: it depends only on bundle files, never on store artifacts, so refreshing the master list never proposes a build.

### Operator interface

```sh
pixi run release OGS-00003             # one registered release, plus any parent it depends on (workflow/release.py)
pixi run release OGS-00003 OGS-00004   # several registered releases; lineage order is resolved
pixi run index                         # regenerate master list, summaries, by-label/
```

A release target must be an ID currently registered under `stores/`; the
operator finds valid IDs in `stores.tsv`. IDs in these commands are real
targets, not placeholders. All three are targets of the same Snakefile.

> **Production note**: Workflow tests (`tests/workflow/`) are fixture-scale and run
> in temporary environments. Full production runs (such as building all seven Trial
> Store Releases via `pixi run release OGS-00001 OGS-00002 OGS-00003 OGS-00004 OGS-00005 OGS-00006 OGS-00007`)
> require raw source and reference preflight to confirm external data exists before
> execution, and must not be run until those inputs are verified.

## The master list

`stores.tsv`, `STORES.md`, every bundle's `summary.yaml`, and both `by-label/`
trees are **generated** by the `index` rule, committed, and verified in CI by
regenerating them and failing if the tree is dirty. `analyses.tsv` remains the
authority for summary values; the generated files are views that cannot go
stale.

**`index` reads git, never the artifact root.** That is the whole constraint, and it is narrower than it first appears. It does not mean the master list is limited to bookkeeping; measurements are welcome, they just have to land in the bundle when the build happens rather than be scraped off disk whenever someone runs the indexer.

Two kinds of column, and they are not in tension:

| | examples | drifts? | so |
|---|---|---|---|
| derived | `store_id`, `label`, `layout`, `status`, `build_command`, `n_analyses`, membership summary fields | yes, if hand-maintained | regenerate from bundles; CI checks |
| observed | `n_variants`, `n_associations`, store size, `format_version`, elapsed, validate verdict | no -- facts about an event that happened once | `register` writes them into the bundle at build time |

Observed values cost nothing to collect: builders already print Store
measurements and completion timing. `register` puts them in `validation.yaml`,
git records them, and `index` reads them from there -- so CI can still
regenerate the entire file, observed columns included. Analysis count is not
copied from that record: it is derived from the membership table with the rest
of the summary. The numbers become reviewable in a pull request diff rather
than being whatever the disk said last time.

This subsumes the deleted `docs/store-catalog.md`, whose per-store numbers were a stale compilation from a date months earlier and which `generate_index()` removes when it is present. A generated `STORES.md` cannot be stale.

The only thing deliberately excluded is anything whose answer changes without a commit -- whether a store still exists on disk, whether it is still readable. That is monitoring, not registry.

`stores.tsv` columns:

```text
derived   store_id  label  layout  completion_state  status  derived_from
          store_uri  created_at  opengwasdb_rev  generator_command  build_command
          n_analyses  n_manifest  n_excluded  first_author  publication_pmid
          tissue  context  assigned_ancestry  sample_size  source_url
observed  format_version  n_variants  n_associations  store_bytes
          build_elapsed_s  validate_status
```

`n_analyses` is admitted membership; `n_manifest` and `n_excluded` keep the
excluded manifest rows visible (issue #217).

`completion_state` is the completion state of the Store users can query, not
the bundle's declaration (issue #307, ADR 0037). `register` records it as
`observed.completion_state` at publication, so a release that completes its own
Store reads `observed_only` while completion runs and `reference_completed`
after the swap. A Validation Record written before that field carries none: the
column then keeps the declared value, which is the state an Observed-Only
release's or a two-id child's Store was published in, except for a release that
completes its own Store, whose earlier publication can only have been the
Observed-Only one, so its row reports `observed_only`. Where the published state
is `observed_only` and the bundle declares `reference_completed`, `STORES.md`
marks the cell `observed_only (completion pending)`; `stores.tsv` keeps the bare
state so a machine reader still sees a documented value. A record whose state
contradicts its bundle otherwise -- an Observed-Only Store under a two-id
child's id -- is refused rather than published.

`store_uri` is a pure function of the identifier (`<artifact-root>/<store-id>/store.opengwasdb`, ADRs 0022 and 0030). The one-time migration-note fallback that previously preferred a legacy family-first path is removed (issue #137), so the published location is where a Store must live, resolved from deployment configuration, never a stale note.

### Readiness reconciliation (issue #217)

`readiness.py` (driven by `python3 -m ogstores.release_readiness`) renders a
reconciliation view that separates manifest, admitted, excluded and intended
bundle membership from what was actually published, and keeps each kind of
evidence on its own channel: Release Status, source acquisition, intended
membership, observed build, built membership, Reference Completion, the
required format-0.2.0 `by_variant` index, the optional HM3 Indexed Variant
Subset, Store validation, registration, format/runtime, lineage and warnings.
`missing` and `unknown` are not passes, and a candidate-generation record is
reported as a gap in Store validation rather than as a clean verdict. The
interim single-id state -- `reference_completed` declared while the
Observed-Only Store is published -- is reported as that declared order of
business on the Reference Completion, format/runtime and observed-build
channels, never as a mismatched artifact (issue #307, ADR 0037).

The manifest count is a *selection*, not intended membership: an authoritative
Source Inventory is reported separately and stays `unknown` where none is
recorded (issue #217). Built Analysis IDs are read only through the supported
core API, `read_analyses(open_store(path).analyses_path)`, which keeps Store
opening and parsing in OpenGWASDB; the registry never constructs
`analyses.tsv`, joins a filename, or touches a storage array. A built-ID
reconciliation and a committed-bundle baseline difference are reported as
separate sections, so a count is never treated as ID equivalence and a
committed baseline is never presented as proof of what is on disk. The
supported `opengwasdb info --format json` manifest metadata is a third,
separate channel.

`--info` refreshes the manifest-metadata channel and `--built` performs the
built-ID read; both are read-only and neither runs a build or a Store
validation. Historical host-side facts that cannot be re-derived from git,
including the committed-versus-published provenance mismatches and the
prospective reference-declaration gaps, live in `docs/readiness-evidence.yaml`,
rendered to `docs/store-release-readiness.md`.

Two commands per store matter, and they are different kinds of thing. The **generator command log** (`inventory.tsv` + config to bundle) is recorded by the generator into `release.yaml` as `generator.commands`. The **build command** (bundle to store) is *derived by `plan()`*, the same function the workflow renders its rules from. A hand-maintained list would be wrong within a month.

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

That closes the loop with one comparison, and catches two failure modes CI alone cannot: a step run by hand with different flags, and a `build.yaml` edited after the build. The one legitimate divergence is `complete-dense-resume` substituted for a planned `complete-dense`, which `run.py` selects to resume an interrupted completion from its checkpoint; `register` accepts that specific pair and records `resumed: true` rather than reporting drift.

### No per-store `commands.sh`

An earlier draft generated a runnable `stores/<id>/commands.sh` per release. It is deliberately not in this design.

It was justified on portability -- rebuild without this repository -- but a script is not what makes a rebuild portable. The inputs are, and they are hundreds of gigabytes of source files and LD panels under the artifact root. Its other benefit, a visible diff in every command line when `plan()` changes, is already delivered twice: by `stores.tsv`'s `build_command` column, and by the golden-argv test in `tests/`.

A per-store command *log* is still wanted, but for Phase B rather than Phase A, and for the opposite reason -- see below.

## `validation.yaml`

The field-by-field format is defined once in
[`docs/release-metadata-schema.md`](../release-metadata-schema.md#validationyaml);
this section records only how the workflow produces it.

Assembled by `register` from the step records: the JSON each build command already prints, plus `opengwasdb validate`'s verdict.

The verdict is read from `opengwasdb validate --format json`, which the planned validate step requests (opengwasdb#175). It prints one object, `{"ok", "errors", "warnings"}`, and the record follows from it exactly:
- `status` and `checks.store` are `failed` when `ok` is false, `passed_with_warnings` when `warnings` is non-empty, and `passed` otherwise;
- `warnings` and `errors` are those two lists, as printed;
- a plan with no validate step records `not_run`.

`register` accepts only what the pinned CLI can print. stdout must be exactly that one object, with no other output and no other key, a boolean `ok`, and lists of strings. `ok` must be true exactly when `errors` is empty. The record's `exit_code` must be an int and `success` a bool; a bool or float exit code is refused by type, because it would compare equal to 0 or 1. The step must have exited 0 and succeeded exactly when `ok` is true. Any other record is refused with `ValidateVerdictError` and nothing is written: malformed, extra, coerced or self-contradictory output, such as `ok: true` with an error. Text output is never searched for the word "warning" (#195).

The record describes this run and no other. `register` replaces the bundle's previous `validation.yaml`; it never merges with it. The old record's `checks`, `warnings`, `errors` and `reports` describe another run, and republishing them under a new `validated_at` would present them as current (#195).

The one thing kept is the accepted bundle's Phase B acceptance evidence, in a separate `acceptance` block:
- from a candidate record, every block it holds verbatim, dated, with the commit it came from, except the fields describing the generator's own run (`status`, `validated_at`, `validator`, `build_environment`, `observed`, `errors`). `register` names no generator's evidence, so a new kind is carried unchanged (#250);
- from a previous `register`-written record, its `acceptance` block as it stands.

Nothing in the block feeds `status`, `checks.store`, `warnings` or `errors`. The candidate record's commit comes from `register.committed_revision()`, which the Snakefile calls before `register_release`; `register_release` itself spawns no subprocess.

`register` also compares each record's executed argv against `plan()`'s planned argv and fails on drift, per "Planned and executed argv are different facts" above. It records; it does not judge. This repository does not decide whether a store is scientifically sound — it captures what `opengwasdb` reported and who accepted it.

The record's top-level `status` is the release-level verdict, and the generated master list publishes that value and no other. A per-check entry in `checks` describes one check and cannot override the record: a record reads `status: failed` precisely when a check failed, and publishing the passing check in its place is the "wrong answer that looks like a right answer" CONTRIBUTING names as the worst outcome. A release with no Validation Record publishes an empty verdict.

## Tests

The checks cover the things this repository is responsible for:

1. **Every Release Bundle satisfies `bundle.check()`** and Manifest Generation's
   own rules (#250). The CI gate discovers
   bundles dynamically, reports all errors per bundle, and accesses no Store or
   Release Artifact. Retired Analysis columns are fixture-tested against the
   pinned upstream list rather than a registry-owned copy.
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
| [opengwasdb#175](https://github.com/opengwas/opengwasdb/issues/175) | Closed on `dev` | Machine-readable `--format json` in `validate` and `info` for `validation.yaml`; the planned validate step passes it, and `register` reads its verdict (#195) |
| [opengwasdb#176](https://github.com/opengwas/opengwasdb/issues/176) | Closed on `dev` | Phase B — `estimate-phenotype-sd` CLI over canonical manifests |

All layouts (Dense, Hybrid, Ragged SSF, and BESD) are unblocked on `opengwasdb@dev`. The active pin in `pixi.toml` ([opengwasdb-stores#106](https://github.com/opengwas/opengwasdb-stores/issues/106)) brings these capabilities into the workspace environment.

### The pinned current-format runtime (stores#218)

Since stores#218 the active pin is an immutable core revision. [The release
runtime](../runtime.md) records which one, with its full identity, how it was
coordinated and what upgrading it does not do; this specification does not
repeat the revision, so a repin cannot leave it stale. The active pin carries
the format-0.2.0 builders, the required Ragged/Overflow `by_variant/`
index, Indexed Variant Subset support, the shared HM3 benchmark seam and the
bounded Dense-validation memory work. `pixi.lock` resolves it with Python 3.12,
`zarr>=3.4,<4` and `numcodecs>=0.17`.

Two consequences are contract, not incidental:

- **Reference Completion needs a format-0.2.0 observed parent.** A 0.1.0 parent
  is readable but not completable by a 0.2.0 builder (opengwas/opengwasdb ADR
  0038 §4). The runtime guard refuses the run before any write when a
  *scheduled* `complete` job would consume a 0.1.0 parent, and directs the
  operator to [the release runtime](../runtime.md) for the supported conversion
  or rebuild order. A same-run `build`/`complete` of the parent is a fresh
  0.2.0 Store and is not gated; a missing parent that is not scheduled to be
  produced is refused. Every completed-release build ticket must supply a
  compatible parent, or convert/rebuild one first. Bumping the runtime does not
  convert or requalify a published Store.
- **Inner chunks name the format's shape, and must tile the decided shard.**
  `--chunk-variants` / `--chunk-analyses` name the inner chunk; their defaults
  are `[1000, 64]`, and the shard `[100000, 1024]` is fixed and not exposed. On
  an axis at least as long as the shard, the inner chunk must divide it, so a
  wrapper must not pass `--chunk-analyses 1000` for a release with 1,024 or more
  Analyses; omit it and take the default. No committed Build Recipe passes an
  inner-chunk option.

## Phase B — what produces a bundle

Manifest Generation is the `manifest_generation` package under `src/manifest_generation/` (#249): the shared candidate workflow and emitter at its root, per-Source-Provider connector code under `providers/`, and each generator's configs and reviewed inputs under `resources/generators/<generator>/` (the legacy R generators also remain there until they retire). Each Release Bundle records the commands it ran in `release.yaml:generator.commands`. The GWAS Catalog eur-hybrid family now has an implemented, resumable candidate workflow (`src/manifest_generation/generate_candidate.py`, issue #153); `workflow/generate.smk` wires its stages as an optional coarse DAG. The five rules below fix that boundary; everything inside it is open.

### Phase A and Phase B are separate workflows

They meet at the accepted Release Bundle and share no DAG. Phase B may well use Snakemake too -- acquisition is genuinely DAG-shaped, with per-file downloads, checksums and filtering -- but as `workflow/generate.smk` with its own entry point; the GWAS Catalog candidate workflow uses it only for coarse stage wiring, and its operator entry point is `pixi run generate-candidate` (issue #153).

The reason is not that one graph would be complex. It is that **the accepted bundle is a boundary only because a human froze it.** Span both phases with one DAG and Snakemake will correctly, silently, regenerate a bundle and rebuild a 71 GB store because a generator config changed upstream. The acceptance gate stops existing, and with it the property the whole of Phase A rests on: that its inputs are fixed.

Their outputs also live in different places and are reviewed differently. Phase B writes into git and is reviewed in a pull request; Phase A writes to the artifact root and is reviewed through `validation.yaml`. And their iteration shapes are opposite: a generator is rerun twenty times while selection is tuned, a build is run once for thirteen hours.

### Why Phase B needs a recorded command log and Phase A does not

| | how the commands are known | so the record is |
|---|---|---|
| Phase A | *derived* from a declarative `build.yaml` by `plan()` | regenerable, CI-checkable, verified against `records/` |
| Phase B | *not derivable* -- family-specific imperative code calling several scripts in sequence | must be **recorded as it runs** |

`release.yaml` now holds `generator: {version, commands}` -- an executed command log (a list), because generation calls acquire, select, assign-ancestry, estimate-SD and emit in turn and a single command string is wrong as soon as more than one step runs (issue #136). The previous `generator.name` path and a family-first `--config=...` argument pointed at files that no longer exist and were corrected in the same change. That is the per-store script that Phase A does not need, and it is a log rather than a prediction.

### The five fixed rules

**Phase B is a separate workflow, meeting Phase A at the accepted bundle** — above.

**Phase B owns every `analyses.tsv` column, including Ancestry Assignment and effect-scale resolution** — see "Phase A never writes `analyses.tsv`", and "Who implements the statistics" below.

**A generator's only output is a bundle directory. It never builds a store.** The four copy-pasted `resources/generators/lib/source-formats/*/build-store.py` adapters exist only because nothing else could reach a builder; under ADR 0023 nothing but the workflow may.

**Acquisition is separate from selection.** Acquisition is per Source Collection and is the expensive resumable part. Selection is per Store Release. The Source Collection is a grouping string recorded on the release, not a directory (ADR 0028).

**Every generator emits through one emitter** (#248, ADR 0035). `src/manifest_generation/emit.py` is the only code that writes a candidate into `stores/<id>/`. A Python generator builds a `CandidateBundle` and calls `emit()`. A legacy R or BESD generator keeps a family-shaped working bundle in its work root, `<artifact-root>/<output.store_id>/generate` (`paths.generation_dir`, reported by `python3 -m manifest_generation.emit work-root <id>`), and its `--mode=emit-registry` projects the current contract from that working bundle and the config's `build:` block (`manifest_generation.emit project`, called from R through `resources/generators/lib/emit_registry.R`). A directory rendered elsewhere can be emitted with `python3 -m manifest_generation.emit <id> --from <dir> --command ...`; anything a bundle does not hold is refused. No generator config names a path outside `stores/` for the *emitted* bundle; a Work Root may be external, such as the artifact root's `<store-id>/generate`, the resolution root, or a `/data/opengwasdb/work/...` directory a release config declares. The emitter:

- writes every YAML document in one style, with `status: candidate` and `generator.commands`, and refuses a rendered status or command log that disagrees;
- stages the bundle under `stores/.staging/<id>/`, checks it against the Release Bundle contract with lineage resolved in the real registry, and writes `summary.yaml`;
- swaps it into `stores/<id>/` in one step, and only over nothing or an earlier candidate;
- holds `stores/.staging/<id>.lock` throughout, so two emits of one id never mix.

```text
resources/inventories/<id>.tsv discovered upstream analyses, once acquisition
                               produces one at scale (ADR 0024). The contract,
                               the readiness vocabulary and the freeze/preflight
                               commands are in resources/inventories/README.md;
                               the code is
                               src/manifest_generation/source_inventory.py.

resources/generators/<family-id>/
    README.md                  the exact commands
    config-<label>.yaml
    inventory.py               freeze a Source Inventory, then preflight it
    generate.R|py              inventory + config -> stores/OGS-xxxxx/

resources/generators/lib/                  shared helpers
resources/generators/lib/source-formats/   Source-Format-scoped generation code
```

The entry-point directory is scoped to the generator's historical family slug (ADR 0028), while the library is source-format-scoped, so families sharing a Source Collection share selection code without a configuration system by accident. This resolves the old `resources/generators/<source-format>-<layout>/` naming collision, where one directory served two families and grew a configuration system to tell them apart.

A generator has the same shape as the build workflow: discover upstream, select rows, shell out to `opengwasdb` for the statistics, write the bundle.

### The Source Inventory seam

Selection starts at a **frozen Source Inventory**, not at a glob over an acquisition mirror (issues #151, #283). A Source Collection's acquisition writes an **Acquisition Manifest** and its metadata adapter a **Source Metadata Table** ([source connectors](source-connectors.md)); the shared freeze reconciles the release config's **Selection** of the table against the manifest and writes the inventory, with both connector files' selected rows frozen beside it. `resources/inventories/README.md` is the contract; `src/manifest_generation/source_inventory.py` owns the readiness vocabulary (`ok` and `already_present` are ready, nothing else is).

Two operator commands sit at this seam, both take a release config ([release config](release-config.md), #287), and both are cheap:

- `pixi run inventory-freeze --config <release config>` writes the frozen snapshot, its frozen connectors and a provenance sidecar recording the Selection, both connectors' checksums and the duplicate-content groups awaiting review. It never overwrites a snapshot;
- `pixi run preflight --config <release config>` re-derives the plan from a frozen snapshot and the release config — per-status counts, ready count/bytes and design split, planned method tier per study design, expected exclusions, duplicate groups, Reference Resource presence, requested cores, work root and free space — reading only the inventory, small per-Analysis metadata and filesystem metadata, and refusing a snapshot not frozen for the config's Source Collection and Selection, or whose frozen connectors are gone or no longer have the sha256 its provenance records, or whose frozen acquisition is not filtered to the config's region policy ([source connectors](source-connectors.md#region-filtered-acquisition)). It never opens an association body and never checksums the source corpus. This inventory-readiness gate is distinct from Store Release Build's preflight and from a **Rehearsal Run** (`CONTEXT.md`).

A snapshot is frozen so membership cannot move underneath a proposal: re-freezing after further acquisition, or for a changed Selection, creates a new snapshot id, and the config is repointed deliberately. Duplicate source content across accessions is reported for a human decision rather than collapsed.

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

That CLI wiring is provided upstream by `estimate-phenotype-sd` ([opengwasdb#176](https://github.com/opengwas/opengwasdb/issues/176)), now available on `dev` and active via #106; Manifest Generation must invoke the upstream CLI command rather than keeping local estimation shims or per-family connectors. The previous `src/manifest_generation/phenotype_sd_estimate.py` was a 47-line shim taking JSON arrays on the command line, which forced each family to open source files and extract `se`/`af`/`beta` itself; that is how `resources/generators/lib/effect_scale_validation.R` grew to 562 lines, of which roughly 60 are the acceptance policy that genuinely belongs here and the rest re-implements reference-panel access and allele harmonisation `opengwasdb` already owns.

### The shared pipeline (issues #153, #287)

Manifest Generation is one pipeline fed by source connectors (ADR 0036), first
written for the EBI GWAS Catalog `hybrid__European` release (#153) and run for
any release config since #287. It is deliberately thin: it turns a frozen
Source Inventory into a candidate Release Bundle by invoking OpenGWASDB for the
statistics, and it never builds, validates or registers a Store (ADR 0012,
ADR 0017). One operator entry point runs every stage:

```sh
pixi run generate-candidate OGS-00011 \
  --config resources/generators/gwas-catalog-eur-hybrid/release-eur-hybrid-full.yaml \
  --cores 64
```

The stages, and what they do not do (`--stage <name>` runs one; a later stage
run alone repeats the in-memory steps it needs, never `freeze`, `preflight` or
`resolve`):

| Stage | Reads | Writes | Never does |
|---|---|---|---|
| `freeze` | the release config's connector files and Selection | the frozen inventory, its frozen connectors and provenance, when absent; `<work-root>/<id>/freeze/<snapshot>.json` | overwrite a frozen snapshot, or accept one frozen for another Selection |
| `preflight` | frozen inventory, provenance, config, filesystem metadata | `<work-root>/<id>/preflight/<snapshot>.json` | open an association body; checksum the corpus |
| `prepare` | frozen inventory, frozen Source Metadata Table | `<work-root>/<id>/resolver/analyses.tsv` | reconstruct a source path or re-select membership |
| `resolve` | the resolver manifest | records in the Source Collection's Resolution Area; `<work-root>/<id>/resolver/{index.json,resolve.log,resolution_receipt.json}` | load a reference, fork a worker, or compute ancestry/SD here |
| `verify` | the records the receipt names | nothing | accept a missing, stale, edited or duplicate record, or one outside the Selection |
| `render` | records, frozen Source Metadata Table, the Canonical Trait Mapping Table, config | Trait Ontology Mapping, then the release policy, then `<work-root>/<id>/bundle/` | compute a statistic; write into `stores/`; carry files filtered to another region policy |
| `emit` | the rendered bundle | a staged bundle, then `stores/<id>/` | build, validate or accept a Store; write a non-`candidate` status |

Re-running a stopped run repeats nothing costly: `freeze` finds the frozen
snapshot and `resolve` reuses every record whose fingerprint still matches.

The resolver subprocess is `opengwasdb resolve-analyses` (opengwasdb#208). It
owns the process pool, the single genome-scale reference load, and the atomic,
fingerprint-aware per-Analysis checkpoints. It parallelises by worker
processes, so it runs with `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS` and `NUMEXPR_NUM_THREADS` set to 1 unless the operator has set
them: unlimited, each worker's numerical libraries start a thread per core
(#300). The registry pins that resolver by
immutable commit in `pixi.toml` rather than vendoring it (ADR 0023).

**`--resolver` is verification-only.** The hidden flag runs another resolver
executable in place of the pinned one: the hermetic tests' fake resolver, or
the resolver a past run's records were made with. The OGS-00011 regeneration
(#287) used it to resume records whose fingerprints carry opengwasdb `a0656c0`
while the pin was `4e92256`, which would otherwise re-resolve every Analysis. The
receipt records the override (`resolver.override`: the executable's `path` and
`sha256`, beside the version and git hash its index reported), and `emit`
refuses such a receipt into the repository's own `stores/`: a verification run
emits into a temporary `--registry-root`. A receipt into `stores/` must record
`resolver.override: null`; one without the key -- written before #287, or
stripped of it -- is refused there too, and re-running the `resolve` stage
rewrites it while reusing every matching record. A relative `--resolver` path
is resolved once, against the caller's working directory, and that absolute
path is both the one run and the one recorded.

**Resolution Records are kept per Source Collection (#284, ADR 0036).** A
Resolution Record belongs to the Analysis, not to a Store Release, so a Trial
Store Release, a full release and each ancestry split of one collection share
them. The `resolve` stage resolves into the Source Collection's Resolution
Area under the resolution root. The root is deployment configuration, resolved
as the Artifact Root is: `--resolution-root`, else `OPENGWASDB_RESOLUTION_ROOT`,
else `ogstores.yaml`'s `resolution_root` (`/data/opengwasdb/work/resolution`),
else the same built-in default. A release config cannot name it
(`output.resolution_root` is refused), so two releases over one collection
always find the same area and the same lock.

```text
<resolution-root>/<source_collection_id>/       the Resolution Area
  .lock                       flock held by the resolving run and its resolver
  checkpoint/<settings-digest>/
                              resolve-analyses' records directory for one set
    <analysis_id>.json          of resolver settings: the latest record per
    index.json                  Analysis, and the last run's index only
  records/<analysis_id>/<fingerprint_digest>/<record_sha256>.json
                              kept Resolution Records, write-once

<work-root>/<store-id>/       this Store Release's Work Root
  preflight/<snapshot>.json
  resolver/analyses.tsv       this Selection's resolver manifest
  resolver/resolve.log
  resolver/index.json         the resolver's index for this run
  resolver/resolution_receipt.json
```

- **Reuse is automatic, and the resolver decides it.** Every run invokes the
  resolver with `--resume`; the candidate entry point has no switch for it and
  refuses `--resume`. The resolver skips an Analysis whose existing successful
  record carries the fingerprint this run would produce, and re-resolves one
  whose file (path, checksum, size or mtime), references (ancestry reference
  and its declared build, group map, extraction panel, Variant Reference,
  reference-AF resources),
  settings (method tier, sample size, gates, MAF and INFO requests) or tool
  revision changed. A `controlled_failure` record is re-run on every run, as
  the resolver has always done; nothing else is.
- **One checkpoint per set of resolver settings.** A checkpoint holds one
  record per Analysis. If one checkpoint served every setting, a Selection
  resolved under other settings would evict another's records, and its next
  run would re-resolve them into new records (the resolver's timings differ)
  for an unchanged fingerprint, so two Stores could name different records of
  one measurement. So the run resolves into `checkpoint/<settings-digest>/`. The
  digest covers the flags `resolver_settings` passes (everything but the
  manifest, records directory and worker count), the content checksum of each
  file they name, and the installed resolver's identity; it is recorded in the
  receipt as `resolver.settings_digest`. It is only a routing key: the
  resolver still checks every Analysis's full fingerprint, so a setting the
  digest misses causes a re-resolve, never a wrong reuse. Per-Analysis inputs
  (file, method tier, sample size, MAF and INFO requests) are not in the
  digest. Two Selections that disagree on them for one Analysis share a
  checkpoint and re-resolve it, while the kept records of both survive.
- **A record is kept, never replaced.** After accounting its records, a run
  copies each of its Selection's records into `records/`, addressed by
  fingerprint digest and checksum, through an exclusive temporary, a rename and
  a directory sync. An existing kept file must hold the same bytes; a
  difference is refused. A receipt never names a checkpoint file.
- **One run at a time per collection.** A checkpoint's `index.json` is one file
  and its records are written in place, so two runs sharing an area would
  interleave. The `resolve` stage takes an exclusive, non-blocking `flock` on
  `.lock` and refuses a second run at once, quoting the holder. The locked
  descriptor is handed to the resolver subprocess, so the lock is held until
  both the run and its resolver have exited: a killed run cannot let another
  run interleave with its still-running resolver. Nothing removes the lock
  file; the kernel releases the lock when the last holder exits. The area must
  live on a filesystem that honours `flock` across every host running
  Manifest Generation: a local filesystem, or NFS with locking. `/data` on the
  generation host is XFS (`stat -f -c %T /data`), a local filesystem. `verify`
  and `emit` read only kept records and take no lock.
- **Store Release Build reads nothing here.** A record reaches a Store only
  through the emitted bundle; `tests/packaging/test_boundary.py` refuses any
  reference to the Resolution Area from `src/ogstores`, `workflow/Snakefile`
  or `workflow/release.py`.

**Canonical resolver manifest.** The manifest is derived from the frozen
inventory's *exact* `data_file` paths — no filename is reconstructed from an
accession — with the per-design method tier from `defaults.by_study_design` and
the total N from the Source Metadata Table frozen beside the inventory, which
the provenance checksums (#287). Every per-Analysis metadata value the bundle
carries comes from that frozen table, with the release config's `defaults`
filling the collection-wide ones (`source_genome_build`, `license`,
`publication_doi`, `consortium`) where a cell is blank. Each manifest row
declares that `source_genome_build`, and the resolver is passed the Ancestry
Reference Panel's `genome_build` from its `resource.yaml` as
`--reference-build`: the pinned resolver takes no build on trust
(opengwasdb#277), so a missing one is refused rather than guessed, and no
`--source-build` fallback is passed. Reading the table is a join on an
already-frozen Selection, never a second selection: membership is still the
inventory's. An inventory frozen without its connectors (the pre-#283
candidate-table freeze) is refused.

**Record accounting.** `verify` fails, and `emit` refuses before anything
replaces a prior candidate, when a selected Analysis has no record, when the
receipt names a record for an Analysis outside the Selection's manifest, when
an Analysis appears twice, when a kept record's bytes no longer match the
checksum the receipt bound, when a record declares a `record_schema_version`
this registry does not accept, or when a record's source checksum/size/file,
method tier and reader capability no longer match the manifest that was
resolved. Records in a shared checkpoint for Analyses outside the Selection
belong to other Selections and are not a failure. The fingerprint digest is
recomputed over the record's own inputs, and the source file's recorded
size/mtime are re-checked against disk, so an edited, replaced or stale record
is caught. This is the "wrong answer that looks like a right answer" guard
applied to membership.

**Resolution receipt.** A record's self-digest only proves it is internally
consistent. A record produced under different gates, ancestry reference or
fine-group map, extraction panel, reference-AF resources or resolver revision
still recomputes a valid self-digest, so a standalone `verify`/`emit` that only
recomputed it could freeze a stale Analysis. After a successful resolver
invocation the `resolve` stage therefore atomically writes
`<work-root>/<id>/resolver/resolution_receipt.json` (`receipt_schema_version`
2), with exactly these keys:

- `manifest_sha256` and `analysis_ids`: the resolver manifest's checksum and
  the Selection's Analyses in manifest order;
- `resolver`: the actual `opengwasdb resolve-analyses` argv, the settings
  digest, the opengwasdb version and git hash its index reported, and
  `override`: null, or the `path` and `sha256` of a `--resolver` executable
  that ran instead of the pinned one;
- `contract`: the registry-recomputable slice of the resolution contract
  (declared gates, MAF floor, reader capability, extraction panel, ancestry
  reference/group-map and reference-AF resource content fingerprints, and the
  installed resolver's content identity);
- `resolution_area`: the Source Collection's Resolution Area, an absolute path;
- `records`: for every `analysis_id`, the kept Resolution Record it was decided
  from, as `fingerprint_digest`, `record_sha256` and `record` (its path in the
  area);
- `receipt_schema_version` and `record_schema_version`.

A receipt with a missing or unknown key, a missing or relative
`resolution_area`, or a malformed record entry is refused; the accepted key set
is the constant `RECEIPT_KEYS`, which a new key extends explicitly. The receipt
names exactly the Selection's records, whether this run resolved or reused
them. Standalone `verify`/`emit` recompute the contract from the current
config, references and installed tool and require it to equal the receipt, read
exactly the records it names, and require each record's checksum and digest to
match it; a `--stage emit` without a prior successful `resolve` fails rather
than trusting hand-assembled records. The registry never re-derives the
resolver's statistics; it binds and re-checks the inputs that determine them.
Core count and the resolution root are recorded as evidence but are not part
of the contract, so neither changes it.

Receipts written before #284 (`receipt_schema_version` 1) bound records in the
Store Release's own Work Root, `<work-root>/<id>/resolver/records/`. They remain
the provenance of the candidates they produced: each emitted `release.yaml`
records its receipt's path and checksum in `source_snapshot.resolver_receipt`,
and nothing in Store Release Build reads them. This registry refuses to verify
one again, by name, rather than misreading it; a regeneration re-runs `resolve`.
To spare that run the work, an operator may first copy the old Work Root's
`resolver/records/<analysis_id>.json` files (not its `index.json`) into the
checkpoint the run will use, `checkpoint/<settings-digest>/`, while no run holds
the lock. `python3 -m manifest_generation.resolution_area_import --config
<release config> --source <old records directory>` does that for one or more
such directories (#290): it routes the records to the checkpoint the config's
resolver settings select, writes each atomically, keeps each through the area's
write-once path, and refuses, by name, a record that is not one the run could
have written (#290 review). The run's own evidence must be present and must
corroborate the record: a run with no `index.json`, a run with no receipt in the
Work Root's sibling `receipt/`, or an index naming no Analysis is refused, never
treated as nothing to check. A record must be named by that index, its
measurements (`assigned_ancestry`, `gate_reason`, `status`, `original_sd` and
`original_sd_method`, and its file name) must be the ones the index recorded,
and, when the receipt binds per-record sha256 (`record_digests`), its bytes must
match too. It must also be an Analysis of the Source Collection's checksummed
intent, its fingerprint must digest to the value it stores, and the
`source_file`/`source_recorded_sha256`/`source_recorded_bytes` it bound must be
the ones the collection's Acquisition Manifest records. Only a successful record
is seeded; a failed one would claim a measurement it does not have. The digest is
`resolver_settings_digest(resolver_settings(config))` in
`manifest_generation.candidate_workflow`. The resolver resumes each record whose
fingerprint still matches, and re-resolves the rest, exactly as it would its own.

**Release policy and controlled exclusions (issue #152).** The registry, not the
resolver, decides membership. A ready Analysis whose Assigned Ancestry is not
the super-population the Selection expects (`release_policy.selection_superpopulation`,
`EUR` for OGS-00011), is unassigned, or was gated out on an EAF
orientation failure is excluded; a quantitative Analysis on the computable
`estimated_from_source_maf` tier whose phenotype SD is unavailable is excluded,
while one whose tier declares its scale (`declared_standardised`, Sun 2018) has
no SD to estimate -- the resolver reports `scale_already_declared`, the
`phenotype_sd` check is `not_applicable`, and it is included with `original_sd`
blank (#300);
a resolver `controlled_failure` is excluded. Each excluded row stays in
`analyses.tsv` with `exclude_from_build: true` and a machine-checkable reason in
`inclusion_reason`, and is explained in `sidecars/exclusions.tsv`. The
exclusions that test a measurement (zero build-eligible rows, the reviewed
placeholder-effect listing, EAF orientation, Assigned Ancestry against the
Selection, phenotype SD) are declared Quality Checks (#285, ADR 0036), and every
Analysis's result for each check is recorded in `sidecars/quality_checks.tsv`,
an optional Release Bundle contract file, passing or not. An Analysis an
exclusion check could not test is refused rather than included. Case-control
rows are included on `log_or`/`binary_trait` with their case/control counts and
an explicit SD skip; duplicate-content accessions are reported and never
collapsed. Because the candidate is a Candidate Bundle, no exclusion blocks the
bundle's existence — it is review material.

**Candidate output.**

```text
stores/OGS-00011/
  release.yaml              # status: candidate; frozen-snapshot evidence and executed command log
  build.yaml                # observed-only Hybrid Build Recipe (opengwasdb subcommand + flags)
  analyses.tsv              # membership: included rows plus exclude_from_build audit rows
  validation.yaml           # Phase B checks; observed Store measurements are null
  sidecars/
    source_readiness.tsv    # every frozen inventory row, with membership + duplicate group
    ancestry.tsv            # one row per selected Analysis
    sd_estimation.tsv       # one row per selected Analysis
    exclusions.tsv          # one row per excluded selected Analysis, with its reason
    reference_overlap.tsv   # one row per selected Analysis
    quality_checks.tsv      # one row per selected Analysis: every Quality Check's result
    analysis_targets.tsv    # only when Analyses have molecular targets: one row per targeted Analysis (#300)
    sparse_regions.tsv      # only for a region-filtered release: its filtering's plan, from acquisition (#300)
    filter_summary.tsv      # only for a region-filtered release: one row per filtered file, from acquisition (#300)
```

**Determinism and atomicity.** Aggregation is ordered by the frozen inventory,
not by completion order or worker count, and every numeric sidecar value is
formatted through the issue-#143 sub-tolerance formatter, so 1 worker and 64
workers produce byte-identical tables and sidecars. The pinned OpenGWASDB
Analysis schema is run over the included rows, and the bundle is then emitted
through the shared emitter, `src/manifest_generation/emit.py` (#248). The
emitter stages it under `stores/.staging/<id>/` (hidden, so
`bundle-check`/`index` cannot see a half-written candidate), runs
`bundle.check()` against the real registry and Manifest Generation's own rules
(#250), writes `summary.yaml`, and only
then swaps it into `stores/<id>/` in one step (`renameat2(RENAME_EXCHANGE)`,
falling back to a move with restore). It replaces only an earlier candidate: a
bundle with any later Release Status is refused, and needs a new Store Release
id. A killed resolver, a failed check or a refused emit leaves the previous
candidate untouched.

The command log in `release.yaml` records the operator invocation and the exact
`opengwasdb resolve-analyses` argv that ran; the candidate remains `status:
candidate` until a human accepts it (see the family README's human-review
section).

