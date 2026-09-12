# Operator guide: build a Store Release from a raw source

Status: operator guide for issue #102, written against the completed FinnGen R13
rebuild (issue #101). It is the practical companion to the
[Store Release workflow specification](./spec/store-release-workflow.md) and the
production [`workflow/`](../workflow/README.md) implementation.

This guide takes you from "I have a source of GWAS summary statistics" to "I
have a validated Store Release". You can follow it without reading any workflow
source. The one thing you *do* have to write is the family-specific helper that
turns your upstream source into the **fixed input**; everything after that is
shared and unchanged between Store Families.

> **Vocabulary.** Store Family, Family Release ID, Analysis, Source Collection,
> Store Layout, Release Status and the other terms used here are defined in
> [`CONTEXT.md`](../CONTEXT.md). When this guide says **Store** it means the built
> OpenGWASDB artifact, `store.opengwasdb`; a **Release** is the registry record
> that describes it.

## 1. The one command

Everything below exists to produce the three fixed inputs; once you have them,
one command builds the Release:

```bash
pixi run release --configfile families/<store-family-id>/releases/<family-release-id>/build.yaml
```

That command is the whole operator surface. It runs the same phases for every
Store Family — eleven of them when both optional branches are enabled, fewer when
they are not. Anything you see appended after `--configfile …` is a Snakemake
flag (for example `--dry-run` or `--unlock`), not an `opengwasdb` argument.

## 2. What a Store Family owns, and what it gets for free

The fixed-input boundary is the whole point of the design. Put each decision on
the correct side of the line: if it needs to know what a *column*, a *trait*, or
a *provider* means, it is family-specific. If it reads, normalises, constructs,
mutates, or validates an OpenGWASDB Store, it is shared.

| Family-specific — **you write it** | Shared — **you get it unchanged** |
|---|---|
| A helper that acquires the upstream files into a raw directory | Source reading and normalisation into the Store schema |
| Selection policy: which Analyses from the Source Collection become this Release | Input validation, including source-file checksum verification |
| Provider metadata enrichment (labels, sample sizes, ancestry, ontology, licence, DOI/PMID) | Assigned Ancestry and effect-scale / phenotype SD resolution |
| Emitting the fixed input: the raw directory, `analyses.tsv`, and `build.yaml` | The `opengwasdb` build itself (the CLI subcommand you name) |
| The concrete `build.command` and its arguments, and the declarations of any Reference Resources | Top-Hit indexes (produced by the build) |
| Choosing whether to enable the rho and Reference-Completion branches | Rho (in-place), overview regeneration, and Store validation |
| Any family tolerances, such as escalating a failed effect-scale check to blocking | Completion records, resumption, phase invalidation, and the artifact layout |

There is deliberately **no family-specific build script**. If you find yourself
writing code that opens a Store, translates the manifest for a builder, or
probes a built Store, stop: that belongs in the shared workflow (issue #103
removes the three adapters that used to do it per family).

## 3. The fixed input

The shared workflow begins with exactly three inputs. It does not discover
Analyses by scanning the raw directory, and it has no Store-Family conditionals.

| Input | Where | What it is |
|---|---|---|
| Raw source directory | `source.root` in `build.yaml` | The acquired upstream summary-statistics files for this Release. Lives outside Git (ADR 0015). |
| `analyses.tsv` | Beside `build.yaml` unless `source.analyses` says otherwise | The exact selected Analyses, one row each, with their source file name and checksum. |
| `build.yaml` | The Release Bundle | Declares the input, the `opengwasdb` build command and its arguments, the Reference Resources, and the optional branches. |

A Source Inventory and `analyses.tsv` are different things. Your helper may
discover every upstream file and then *select* from it; the build consumes only
the `analyses.tsv` selection. A file present in the raw directory but absent from
`analyses.tsv` is not built.

### `analyses.tsv`

The columns are governed by the shared OpenGWASDB Analysis schema (ADR 0017) and
documented field-by-field in
[`docs/release-metadata-schema.md`](./release-metadata-schema.md). A buildable
Release needs at least:

| Column | Meaning |
|---|---|
| `analysis_id` | Stable registry Analysis ID. |
| `source_file` | File name, resolved relative to `source.root` unless absolute. |
| `analysis_label` | Display name, carried into the Store. |
| `sample_size` | Study N (with `n_cases`/`n_controls` for case-control traits). |
| `stored_effect_scale` | `sd`, `log_or`, or `log_hazard`. |
| `original_sd_method` | How the phenotype SD was obtained (and `original_sd` when the method carries a magnitude). |
| `checksum` / `checksum_algorithm` | `sha256` (the default) of the source file. Verified at input validation. |

A row with `exclude_from_build: true` is retained for audit but is **not** built.

## 4. Choose the build command for your Store Layout

`build.command` names an `opengwasdb` CLI subcommand. It is not a Python
entrypoint, and the workflow does not import it: it shells out to the same CLI
you can run by hand. The authoritative list is the CLI's own help:

```bash
pixi run --environment workflow opengwasdb --help
pixi run --environment workflow opengwasdb build-dense-vcf --help
```

Choose by the Store Layout you are producing. The command names are the CLI's;
run `opengwasdb <command> --help` for its flags.

| Observed Store Layout | `build.command` | Status |
|---|---|---|
| Dense Observed-Only, manifest-direct | `build-dense-vcf` | The Dense manifest builder. It reads each row's declared Source Reader Capability, so the same command serves GWAS-VCF and the native `opengwasdb.finngen-r13` source. Proven end to end at production scale by issue #101. Requires `store-id` and `release-id`. |
| Hybrid Observed-Only, manifest-direct | `build-hybrid` | Uses the same two-positional builder-manifest shape as Dense; not yet rebuilt through the workflow. |
| Hybrid Observed-Only, catalogue-routed | `build-hybrid-from-catalogue` | The catalogue path (issue #104): two pre-build phases annotate and route an Analysis Catalogue, then the build row-filters it to one ancestry. Exercised end to end by the `gwas-catalog-eur-hybrid` pilot. See [§4a](#4a-catalogue-routed-releases). |
| Ragged Observed-Only | `build-ragged-ssf` or `build-ragged-besd` | Not yet driven by the shared workflow. `build-ragged-ssf` takes a third positional (`filtered_dir`) and `build-ragged-besd` takes a BESD prefix, while the workflow supplies exactly two positional paths. |

The Reference-Completion child is built by its own command, not `build.command`:

| Child Store Layout | `reference_completion.command` |
|---|---|
| `dense-reference-completed` | `complete-dense` |
| `hybrid-reference-completed` | `complete-hybrid` |
| `ragged-reference-completed` | `complete-ragged` |

The workflow passes exactly **two positional paths** to the build command: the
resolved builder manifest it wrote, and the output Store directory. That is why
the table above distinguishes commands whose CLI signature is
`{input} {output}`. Everything else about the invocation is the opaque argument
passthrough described next.

The Dense manifest-direct path (`build-dense-vcf`) is the one proven end to end
at production scale by issue #101. The Hybrid *catalogue-routed* path
(`build-hybrid-from-catalogue`) is the one proven end to end by issue #104 on
the real `gwas-catalog-eur-hybrid` pilot. The Hybrid *manifest-direct* path
(`build-hybrid`) uses the same mechanism as Dense but has not yet rebuilt a real
Release through the workflow; treat its first Release as a pilot.

### Arguments are an opaque passthrough

`build.arguments` (and `rho.arguments`, and `reference_completion.arguments`) is
a mapping of CLI flag name to value, passed through unchanged:

```yaml
build:
  command: build-dense-vcf
  arguments:
    store-id: my-family
    release-id: r1-observed
    n-workers: 16
```

There is deliberately **no semantic schema**. `store-id` and `release-id` do not
exist as typed keys here; a newly required builder flag is absorbed by adding one
line, with no change to the workflow. That also means no key such as `workers`
or `chunk_shape` exists — the real flags are `--n-workers`,
`--chunk-variants`, and `--chunk-analyses`, and you should take their names and
defaults from `opengwasdb <command> --help`, not from memory.

A `true` value emits the flag alone; `false` omits it; a YAML list repeats the
flag once per item (`feature-flags: [alpha, beta]` becomes `--feature-flags
alpha --feature-flags beta`).

### 4a. Catalogue-routed releases

A release whose `build.command` is `build-hybrid-from-catalogue` does **not** run
the manifest-direct resolve phase. The branch is decided by the command, never by
a Store-Family name (issue #104), so nothing in the Snakefile is family-specific.
Instead it runs two pre-build phases, each with its own completion record:

1. **`assign_ancestry`** writes your Analyses as a lossless source manifest and
   runs `opengwasdb assign-ancestry` against an ancestry-mixture Reference
   Resource, producing a versioned **Analysis Catalogue**
   (`work/analysis-catalogue.tsv`). Analyses that are not European and Analyses
   the assignment cannot admit stay in the Catalogue — nothing is dropped.
2. **`route_catalogue`** derives a coverage table from your own selected sources
   through the configured reader, then runs `opengwasdb route-catalogue` to add
   the routing and eligibility columns (`work/routed-catalogue.tsv`). Because it
   is a separate phase, an interrupted routing step re-runs only routing — the
   expensive ancestry assignment is not repeated.

There is **no hand-authored coverage file**: the coverage table is derived from
your sources and bound into the routing phase's completion record, so editing a
source invalidates routing exactly as it invalidates the build.

The build then runs `opengwasdb build-hybrid-from-catalogue`, which row-filters
the routed Catalogue to `--ancestry` and builds a Hybrid Store from the Dense
Component panel. Analyses of another ancestry stay in the Catalogue, so the
built Store carries exactly the `assigned_ancestry == <ancestry>` subset.

A catalogue-routed release must declare three extra inputs, all checked at input
validation before anything expensive runs:

- the Source Reader Capability (`source.reader.capability`, or the equivalent
  `source.source_reader_capability`) that reads every source;
- an `ancestry_assignment.reference_resource_id` naming an `ancestry_mixture`
  Reference Resource with both a `location` (reference frequencies) and a
  `fine_group_map` (fine-to-super-population map);
- a `hybrid_dense_panel` Reference Resource for the Dense Component's variant
  panel.

The catalogue path is the one that can be genuinely lossy, so it is also the one
that needs a reference to the raw source. The `gwas-catalog-eur-hybrid` pilot is
the worked example:
[`families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10/build.yaml`](../families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10/build.yaml).

## 5. Write `build.yaml`

The schema evolves the pre-existing `build.yaml` in place — one filename, one
schema (ADR 0022). This is a complete, buildable skeleton; adapt the
family-specific values and delete the optional branches you do not use:

```yaml
store_family_id: my-family
family_release_id: r1-observed
store_layout: dense-observed
completion_state: observed-only

source:
  root: /data/opengwasdb/my-family/releases/r1-observed/source
  analyses: analyses.tsv
  source_format: gwas-vcf
  source_reader_capability: opengwasdb.gwas-vcf
  source_genome_build: GRCh38

normalisation:
  target_reference_assembly: GRCh38
  liftover: none

effects:
  stored_effect_scale: sd

shape:
  association_coverage: full_gwas

build:
  command: build-dense-vcf
  arguments:
    store-id: my-family
    release-id: r1-observed
    n-workers: 16

rho:
  enabled: true
  arguments:
    n-workers: 16

reference_completion:
  enabled: true
  family_release_id: r1-completed
  command: complete-dense
  arguments:
    ld-panel: /data/opengwasdb/reference/hgdp1kgp-hg38/panel
    ancestry: EUR
    release-id: r1-completed
    n-workers: 16

validation:
  required: yes

artifacts:
  artifact_root: /data/opengwasdb
  release_subdir: my-family/releases/r1-observed
```

`build.yaml` is executable on its own: it names the operation as a CLI
subcommand, so nothing in this repository has to import a builder. The optional
branches are configured here too, and only here (ADR 0022):

- **`rho`** is Dense-only. Enabling it on a non-Dense layout is refused at input
  validation, before anything expensive runs. The workflow runs
  `opengwasdb build-dense-rho` as an in-place mutation of the built Store, so it
  always regenerates `overview.html` after it.
- **`reference_completion`** registers and builds a *separate* child Store
  Release whose lineage names the observed parent (ADR 0007); its Store Layout
  is `<layout>-reference-completed` (so `dense-observed` produces a
  `dense-reference-completed` child). It never mutates the observed Store. The
  child's bundle is this bundle's sibling
  (`families/<family>/releases/<child-id>/`).

The full worked example is
[`families/finngen-r13/releases/r13-pilot-20/build.yaml`](../families/finngen-r13/releases/r13-pilot-20/build.yaml).

## 6. Check the fixed input before you build

Before any expensive work, run the plan loader. It reports pass/fail with a
reason that names the offending key, and it is the same check the workflow's
first phase runs:

```bash
pixi run --environment workflow python resources/lib/release_plan.py \
  families/my-family/releases/r1-observed
```

It refuses, naming the key:

- a `build.command` that is not an `opengwasdb` CLI subcommand;
- rho on a layout with no rho implementation (rho is Dense-only);
- a `store_layout` that contradicts the layout implied by `build.command`;
- a Reference Resource referenced by `ancestry_assignment`,
  `effect_scale_validation`, or `qc_panel` that is not declared in
  `reference_resources`;
- a `source.root`/`source.analyses` that does not exist;
- a source file named in `analyses.tsv` that is missing, or whose declared
  checksum does not match.

Use `--no-checksums` to skip hashing the source files when you only want the
structural checks.

## 7. Run the workflow

```bash
# Show what would run, without running anything:
pixi run release --configfile families/my-family/releases/r1-observed/build.yaml --dry-run

# Build:
pixi run release --configfile families/my-family/releases/r1-observed/build.yaml
```

The workflow runs these phases, skipping the optional ones the plan disables
(phase IDs are what you will see in `work/` and in error messages):

| Phase | What it does |
|---|---|
| `validate_fixed_inputs` | Checks `build.yaml`, `analyses.tsv`, the selected source files and checksums, and every declared Reference Resource. Refuses rho on a non-Dense layout here, before any Store work. Writes `sidecars/input-validation.json`. |
| `resolve_analysis_metadata` | Only on a **manifest-direct** release. Computes Assigned Ancestry and proportions, and effect-scale / phenotype SD. Writes the working input `work/analyses.resolved.tsv`, the builder manifest `work/builder-manifest.tsv`, and `sidecars/metadata-resolution.tsv`. Never writes the committed `analyses.tsv`. |
| `assign_ancestry` | Only on a **catalogue-routed** release. Annotates the sources into the versioned Analysis Catalogue (`work/analysis-catalogue.tsv`). Writes `sidecars/catalogue-assignment.json`. |
| `route_catalogue` | Only on a **catalogue-routed** release. Derives coverage from your sources and adds the routing/eligibility columns (`work/routed-catalogue.tsv`). Writes `sidecars/catalogue-routing.json`. |
| `build_observed_store` | Runs your `build.command` with its opaque arguments. Writes `sidecars/build-report.tsv`. |
| `build_observed_rho` | Only when `rho.enabled`. Mutates the built Store in place, adding `data.zarr/rho`. Writes `sidecars/rho-report.json`. |
| `regenerate_observed_overview` | Regenerates `overview.html` from the persisted Store, after every mutation. |
| `validate_observed_release` | Validates the Store and merges the result into `validation.yaml`, then lands the Release Status. |
| `register_completed_release` | Only when `reference_completion.enabled`. Registers the lineage-linked child Release Bundle. |
| `complete_store` | Builds the child Store from the observed Store into a `.partial` sibling, then moves it into place. Writes the child's `sidecars/completion-report.tsv`. |
| `build_completed_rho` | Only when `rho.enabled`. The child's in-place Rho Matrix. |
| `regenerate_completed_overview` | The child's `overview.html`, after its last mutation. |
| `validate_completed_release` | Validates the child Store and writes the child's `validation.yaml`. The final target when Reference Completion is enabled. |

## 8. Read the result

`opengwasdb info` reads the built Store's envelope back, and is the quickest
sanity check:

```bash
pixi run --environment workflow opengwasdb info \
  /data/opengwasdb/my-family/releases/r1-observed/store.opengwasdb
```

A clean observed Release writes these things back into the repository (the only
things that belong in Git — see §12):

| Path | Contents |
|---|---|
| `families/<family>/releases/<release>/validation.yaml` | Validation status, per-check results, warnings, and pointers to the sidecar reports. |
| `families/<family>/releases/<release>/release.yaml` | The Release Status line (`built` or `validated`) is updated here. |
| `families/<family>/releases/<release>/sidecars/` | The small per-phase reports: `input-validation.json`, `build-report.tsv`, `rho-report.json` when rho ran, and — on a catalogue-routed release — `catalogue-assignment.json` and `catalogue-routing.json` (instead of `metadata-resolution.tsv`). |

Everything large — the raw sources, the work files, and the built Store — stays
under the artifact root and is never committed.

## 9. Interruption and resumption

A run can be interrupted at any point. What you do next is always the same:
**re-run the same command.** You do not need to know which phase died.

What the workflow guarantees:

- **A phase is tracked by its completion record, not by the Store's timestamp.**
  Each phase writes `work/completions/<phase>.json` only after reading its output
  back and checking it. A half-written Store has no record, so it cannot
  masquerade as a completed phase.
- **A new-output phase writes into a `.partial` sibling** (`store.opengwasdb.partial`)
  and moves it into place only after the read-back passes. An interrupted build
  therefore never leaves a half-written Store at the Release's Store path.
- **A failed or interrupted phase writes no record**, so the next invocation
  re-runs it and everything downstream — whatever the Store's modification time
  says.
- **An interrupted Reference Completion resumes from `opengwasdb`'s own
  checkpoint.** `complete_store` records the exact command it issued beside the
  checkpoint and, on the next run, issues
  `opengwasdb complete-dense-resume` when the command is unchanged. A checkpoint
  from a different configuration is discarded rather than resumed against the
  wrong panel.

### After an unclean kill

If the process was killed with `SIGKILL` (or the shell died), Snakemake may have
left a lock behind and will refuse to start:

```
Error: Directory cannot be locked. ...
```

Clear it and re-run:

```bash
pixi run release --configfile families/my-family/releases/r1-observed/build.yaml --unlock
pixi run release --configfile families/my-family/releases/r1-observed/build.yaml
```

Snakemake may leave a lock behind after an interrupted run that could not clean
up (the issue #101 run needed `--unlock` after its deliberate interruption).
Interrupt a run deliberately with `SIGTERM` to the process group rather than
`SIGKILL`: it lets Snakemake shut its child processes down in an orderly way.
Reserve `SIGKILL` for a process that has genuinely hung.

## 10. What changes invalidate a completed phase

Every completion record binds the content hashes of:

- `build.yaml`;
- `analyses.tsv`;
- every selected source file;
- every declared Reference Resource descriptor (and any Reference Resource file
  tracked in this repository).

Change **any** of those, and that phase and everything downstream of it are
invalidated on the next run. In particular:

- Editing `build.arguments` changes `build.yaml`, so the build re-runs.
- Editing `analyses.tsv` (adding an Analysis, fixing a label, changing a
  checksum) re-runs the resolve phase and the build.
- Replacing a source file re-runs input validation and the build.
- Swapping a tracked Reference Resource (such as the ancestry-mixture panel)
  re-runs the resolve phase that used it.

Because the completion records live under the artifact root but the bundle lives
in Git, a **fresh checkout or `rsync` refreshes the bundle files' modification
times**, which makes Snakemake believe the inputs changed and re-runs from the
top even when the content is identical. Check with `--dry-run` first; a rerun
after a checkout is expected and produces the same Store, because the completion
records' content hashes still match. A run in the directory where the build
happened is a true no-op: `Nothing to be done`.

## 11. `built` versus `validated`

These are two different fields, and the difference matters:

- **`release.yaml` `status`** is the Release Status: `built` or `validated`.
- **`validation.yaml` `status`** is the validation outcome: `not_run`, `passed`,
  `failed`, or `passed_with_warnings`.

A clean run lands the release as `validated`, with per-check results in
`validation.yaml`:

```yaml
status: passed
checks:
  schema: passed
  files: passed
  store: passed
```

### When the Store built and validated but the release is `built`

Effect-scale validation can genuinely fail for one Analysis in an otherwise
clean Release. That is **evidence, not a workflow failure**: the failing check is
recorded, the Release lands as `built` rather than `validated`, and the run does
**not** fail. The FinnGen R13 pilot is the worked example — it is a good Store
with one honest scale inconsistency:

```yaml
# validation.yaml
status: failed
checks:
  schema: passed
  files: passed
  ancestry: passed
  effect_scale: failed
  sd_estimation: failed
  store: passed
warnings:
  - finngen-r13-HEIGHT_IRN: empirical effect-scale status=failed (...)
```

```yaml
# release.yaml
status: built
```

So: `validation.yaml` `status: failed` with `checks.effect_scale: failed` and
`release.yaml` `status: built` means "the Store is built and readable; one
Analysis's scale could not be confirmed; do not treat the numbers as silently
correct". It is expected for this Release, not a bug to chase. Rescaling the
Analysis silently would be worse than recording the failure.

If a family wants such a failure to stop the build instead, it sets
`effect_scale_validation.block_on_failure: yes` in `build.yaml`; the run then
refuses to build, and the failure is recorded before it stops. A Release that
never opted into empirical effect-scale validation records
`checks.effect_scale: not_run`, not a bare pass.

## 12. Where raw inputs, work files, and Stores live (ADR 0015, ADR 0018)

The repository stores registry metadata, manifests, recipes, summaries, and small
reports. It does **not** store raw data, Stores, work directories, or large logs
(ADR 0015). Large artifacts live under the configured artifact root, mirrored to
the Store Family and Family Release ID (ADR 0018):

```text
<artifact-root>/<store-family-id>/releases/<family-release-id>/
```

For this server the artifact root is `/data/opengwasdb`, so a Release looks like:

```text
/data/opengwasdb/my-family/releases/r1-observed/     # NOT in Git
├── source/                    # acquired raw inputs (source.root)
├── work/                      # transient work files
│   ├── completions/<phase>.json   # the records Snakemake tracks
│   ├── analyses.resolved.tsv      # the immutable resolved working input
│   └── builder-manifest.tsv       # the manifest the build command reads
├── store.opengwasdb/          # the built Store (mutated in place by rho/overview)
└── store.opengwasdb.partial   # only while a build is in flight

families/my-family/releases/r1-observed/            # tracked in Git
├── build.yaml                 # the plan
├── analyses.tsv               # the fixed selection
├── release.yaml               # identity + Release Status
├── validation.yaml            # validation record
└── sidecars/                  # small per-phase reports
```

Two consequences worth internalising:

1. **Never `git add` a Store, a raw source file, or a `work/` directory.** If you
   see one in `git status`, the artifact root is misconfigured. A Release Bundle
   points at the artifacts by path; it does not contain them.
2. **The Release Bundle is the reproducible input.** Someone can rebuild the
   Store from the tracked `build.yaml` and `analyses.tsv` plus the external
   sources named in them. That is what makes the registry an audit record.

## 13. Worked example: FinnGen R13 `r13-pilot-20`

This is the release issue #101 rebuilt end to end through this workflow. It is
Dense and manifest-direct, and it exercises both optional branches, so it is the
best template for a new family.

**The family setup.** `finngen-r13` has no family-specific build script. Its
helper ([`families/finngen-r13/generators/generate.R`](../families/finngen-r13/generators/generate.R),
with acquisition and metadata helpers) freezes a bounded 20-Analysis selection
from the public R13 manifest and emits the fixed input under
`/data/opengwasdb/finngen-r13/releases/r13-pilot-20/source/`. The committed
[`analyses.tsv`](../families/finngen-r13/releases/r13-pilot-20/analyses.tsv)
names those exact 20 files with their checksums.

**The plan.**
[`build.yaml`](../families/finngen-r13/releases/r13-pilot-20/build.yaml) sets
`build.command: build-dense-vcf`, enables `rho`, and enables
`reference_completion` for the `r13-pilot-20-completed` child against the
HGDP+1kGP hg38 EUR panel.

**The one command.**

```bash
pixi run release --configfile families/finngen-r13/releases/r13-pilot-20/build.yaml
```

**The result** (from issue #101's evidence record,
[`docs/finngen-r13-r13-pilot-20-rebuild-evidence.md`](./finngen-r13-r13-pilot-20-rebuild-evidence.md)):

| | Observed `r13-pilot-20` | Child `r13-pilot-20-completed` |
|---|---|---|
| Layout | Dense Observed-Only | Dense Reference-Completed |
| Analyses | 20 | 20 |
| Variants | 21,230,615 | 23,792,347 |
| Store size | 5.1G | 4.9G |
| Release Status | `built` | `validated` |

The observed release is `built`, not `validated`, because
`finngen-r13-HEIGHT_IRN` fails effect-scale validation — the expected outcome
described in §11. The child is `validated`. The observed build phase alone took
about 72 minutes at 32 workers; that is the expensive phase the completion-record
design exists to avoid repeating.

**Interruption in practice.** The build was interrupted by `SIGTERM` 28 minutes
in, well past reading the raw inputs. It left no `build_observed_store`
completion record and no `store.opengwasdb.partial`. Re-running the same command
rescheduled the build and everything downstream, and the run completed. A second
run in the same build directory with unchanged inputs was a no-op
(`Nothing to be done`); after a fresh checkout, expect the modification-time
rerun described in §10 instead. The precise timings, sizes, and the acceptance
evidence are in the evidence record above.

## 14. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `opengwasdb` is not on PATH | You ran the phase outside the Pixi environment. Use `pixi run release …`; it runs in the `workflow` environment. |
| `unknown opengwasdb CLI subcommand '…'` | Spell `build.command` exactly as `opengwasdb --help` lists it, e.g. `build-dense-vcf`, not `build_dense_vcf`. |
| `rho is Dense-only; store layout '…' has no rho implementation` | Disable `rho` for Hybrid/Ragged, or change the layout. Rho has no Hybrid or Ragged implementation. |
| `Directory cannot be locked` | A previous run was killed uncleanly. `--unlock`, then re-run. |
| The build re-runs a phase you thought was done | One of its bound inputs changed (§10) — or a fresh checkout refreshed the bundle's modification times. `--dry-run` shows which rules would run. |
| `checksum mismatch for …` | The source file changed, or `analyses.tsv` carries a stale checksum. Re-verify the file and update the checksum deliberately, not by deleting the check. |
| `validation.yaml` `status: failed` | Read `checks`: if only `effect_scale`/`sd_estimation` failed and `store: passed`, the Store is fine and the release is legitimately `built` (§11). Any other failing check is a real problem. |
| `git status` shows a Store or raw file | The artifact root is wrong. Move the artifact under `/data/opengwasdb` and fix `artifacts.artifact_root`; never commit it. |
