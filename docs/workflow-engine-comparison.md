# Workflow orchestration engine comparison

Date: 2026-09-10

## Question and scope

This note compares Snakemake, Nextflow, and a thin repository-owned
orchestrator for the current-server Store Release workflow. It reports evidence
and trade-offs; it deliberately does **not** choose an engine.

The comparison assumes the following workflow contract:

- one Store Release is processed at a time by one operator on the current Linux
  server; a queue, HPC execution, cloud execution, publication, and activation
  are outside the present scope;
- source acquisition is a reproducibility helper, but the workflow starts only
  from an acquired and verified Source Inventory;
- Store Family configuration and that inventory drive automatic Release
  Manifest generation and validation;
- a representative Preflight Run produces domain reports and stops at one hard
  human-review gate;
- approval permits the full run, whose conditional phases can include the
  layout-specific build, validation, top-hit construction, rho construction,
  and—when the Release is a derived Reference-Completed Release—reference
  completion;
- every expensive phase must be resumable, and the workflow stops at a
  validated Release Artifact;
- Python, R, OpenGWASDB, and native tools continue to be supplied through the
  repository's locked Pixi workspace; and
- large source, work, log, and Store files remain below `/data/opengwasdb`,
  while Git contains metadata and small reports.

These constraints extend, rather than replace, the repository's existing
model. The current [pipeline-stage vocabulary](release-metadata-schema.md#pipeline-stages)
already names `discover`, `select`, `derive`, `emit`, and `accept`; the
[external-artifact contract](../README.md#external-artifacts) already places
large products below `/data/opengwasdb/<store-family-id>/releases/<family-release-id>/`;
and [ADR 0012](adr/0012-manifest-generators-own-orchestration-only.md) keeps
reusable reading, normalisation, and Store building in OpenGWASDB. The
orchestrator should therefore call a single release-build interface across
layouts rather than encode the per-layout dispatch and adapters criticised in
[The release seam to opengwasdb has no interface: builder.entrypoint is inert,
the Builder Manifest adapter is copy-pasted and lossy, and the bundle format
has no owner module](https://github.com/opengwas/opengwasdb-stores/issues/82).

## Comparison at a glance

| Concern | Snakemake | Nextflow | Thin repository-owned orchestrator |
|---|---|---|---|
| Primary model | File-oriented dependency graph of rules and requested targets | Dataflow graph of processes connected by channels | An explicit, repository-defined stage/state machine |
| Current-server operation | Built-in local executor with core/resource limits | Local executor is the default; process resources are configurable | Direct local subprocesses; resource exclusion must be implemented |
| Pixi fit | Official Snakemake docs explicitly document installation with Pixi | Nextflow needs Java 17+; Conda installation exists but its own docs warn about freshness/conflicts | Uses the existing Python runtime and Pixi tasks; no new runtime |
| Python/R reuse | First-class external Python and R script directives | A process may execute any interpreter selected by its script/shebang | Calls the current scripts directly |
| Resume basis | Declared outputs plus timestamps/checksums and persisted provenance; incomplete outputs can be rerun | Task hash plus both the task cache and retained work outputs | Only the fingerprints, state transitions, and validators implemented here |
| Conditional phases | Configuration/input functions; checkpoints can re-evaluate a data-dependent graph | Conditional calls/filtering in the workflow and conditional processes | Straightforward imperative predicates over the Build Recipe |
| Large mutable Store | Directory and update outputs exist, but deletion/rollback semantics need a scale test | In-place input mutation is explicitly an anti-pattern for resume | Can model in-place mutation directly, but safety and rollback are entirely local responsibilities |
| Operational visibility | Dry run, DAG/rule graph, summaries, logs, benchmarks, self-contained HTML report | Execution log, HTML report, timeline, trace metrics, and DAG | Exactly the desired status/report UI can be built, but none is free |
| Human gate | Naturally split into a preflight target and a later full target; approval remains a domain artifact | Naturally split into named workflows or a phase parameter; approval remains a domain artifact | A stop/approve/continue transition can be native to the state model |
| Main cost introduced | Snakefile/rule semantics and careful handling of directory/in-place outputs | Nextflow/Groovy DSL, Java packaging, retained cache/work storage, and adapting in-place builds | Designing, implementing, documenting, and testing a dependable workflow engine |

## Snakemake

### Capabilities relevant here

Snakemake is file-oriented: rules declare inputs and outputs, and a requested
file or target rule selects the part of the graph to execute. A target rule can
collect a subset of results, which maps directly to separate `preflight` and
`release-artifact` targets. The CLI also supports `--until` to run only through
a named rule or file, `--dry-run`, graph rendering, and output summaries
([target rules](https://snakemake.readthedocs.io/en/stable/tutorial/basics.html#step-7-adding-a-target-rule),
[execution and visualisation options](https://snakemake.readthedocs.io/en/stable/executing/cli.html)).

It can invoke the repository's existing Python and R code without rewriting
that code into the workflow language. Its external-script interface supports
both languages and exposes named inputs, outputs, parameters, logs, threads,
resources, and configuration to the script
([Snakemake external scripts](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html#external-scripts)).
Local runs accept an overall core limit, while rules declare threads and other
resources; workflow profiles can hold server-specific defaults
([Snakemake resources and profiles](https://snakemake.readthedocs.io/en/stable/executing/cli.html#profiles)).

For recovery, Snakemake decides whether a result is current from declared
outputs and persisted metadata. The current CLI's default rerun triggers cover
code, inputs, modification times, parameters, and software environments; it can
report why outputs are stale and `--rerun-incomplete` reruns outputs recorded as
incomplete
([Snakemake rerun and incomplete-output options](https://snakemake.readthedocs.io/en/stable/executing/cli.html)).
Data-dependent checkpoints can re-evaluate the graph after a checkpoint
succeeds, so a manifest-derived layout or enabled-phase set can control later
rules if it cannot be known when the workflow is first parsed
([Snakemake data-dependent conditional execution](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html#data-dependent-conditional-execution)).

The operational surface is broader than a console log. Snakemake renders a DAG
in DOT or Mermaid form, prints normal and detailed output summaries, and can
produce a self-contained HTML report containing default statistics, provenance,
and nominated result files
([Snakemake reports and visualisation](https://snakemake.readthedocs.io/en/stable/executing/cli.html#reports)).
Those facilities can expose execution state and collect the domain-specific
ancestry, filtering, build, top-hit, rho, and validation reports, but they do
not generate those domain reports; each phase must still emit its own small,
reviewable record.

Snakemake has a direct packaging path into the existing environment contract:
its installation guide explicitly shows `pixi add snakemake` with conda-forge
and Bioconda. Rule-specific Conda environments are optional, so this repository
could continue treating the single root `pixi.lock` as the software authority
instead of adding a second environment layer
([Snakemake installation via Pixi](https://snakemake.readthedocs.io/en/stable/getting_started/installation.html#install-via-pixi)).

### Trade-offs and unresolved fit questions

The strongest fit is the repository's already file-shaped domain: Release
Manifest files, sidecars, reports, and external artifact paths are natural rule
inputs and outputs. A dry run and DAG make the planned path legible before a
many-hour operation begins. A two-command gate is also simple: request the
Preflight Review Packet target, record approval, then request the final
Release Artifact target.

The main risk is that an OpenGWASDB Store is a large directory which later
phases may update in place. Snakemake requires directory outputs to be marked
explicitly, uses a hidden timestamp file for freshness, and deletes a declared
directory output before rebuilding it. Its documentation warns that other jobs
must not create outputs inside such a directory because those outputs could be
deleted. Snakemake also has an `update()` output flag that preserves the prior
file/directory and says it will restore the prior version after failure
([directory outputs](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html#directories-as-outputs),
[updating existing outputs](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html#updating-existing-output-files)).
The documentation establishes the semantics, but not whether backup/restore is
practical for an OpenGWASDB directory tens of gigabytes in size. That requires
a representative interruption-and-resume test before this feature can be
treated as safe.

A conservative Snakemake design would avoid making the whole Store directory
the sole success signal. Each expensive rule would produce a separate,
validated phase-completion record below the Release work directory after the
Store operation succeeds. Rerun decisions would consume the accepted manifest,
Build Recipe, source-verification record, code/lock provenance, prior
phase-completion record, and a Store validation signature. This adds a small
adapter layer, but avoids trusting directory modification time as proof that an
in-place rho or top-hit phase is complete.

Snakemake's flexibility is itself a governance cost. Arbitrary Python in a
Snakefile can move domain logic back into the orchestration layer. The workflow
would need a firm convention that rules perform dispatch, dependency wiring,
and evidence collection only; the Store Build module and OpenGWASDB remain the
owners of domain operations.

## Nextflow

### Capabilities relevant here

Nextflow models work as processes connected by channels. It runs processes as
local operating-system processes by default and can separate process resource
settings from pipeline code using configuration and profiles
([Nextflow executors](https://docs.seqera.io/nextflow/executor),
[Nextflow configuration](https://docs.seqera.io/nextflow/config)). Although its
workflow language runs on the JVM, process bodies are not limited to Java or
Groovy: the official mixed-language example uses a shebang to execute a Python
process, and the same mechanism works for R or existing command-line programs
([Nextflow mixed scripting languages](https://www.nextflow.io/example2.html)).

Conditional phases can be expressed in the calling workflow using normal
conditional logic or channel filtering; the process-level `when` condition is
also available, though the documentation recommends putting conditional logic
in the caller
([Nextflow conditional process execution](https://www.nextflow.io/docs/latest/process.html#when)).
Named workflows can supply separate preflight and full entry points, or one
entry workflow can use an explicit requested phase. This gives the human gate a
clear process boundary rather than asking a many-hour JVM process to wait for
input.

Nextflow's recovery mechanism is task-identity/provenance aware. It
always writes task-cache entries; `-resume` reuses a task only when the computed
task hash matches, the cache entry remains, required outputs remain in the work
directory, and the prior exit status is valid. The hash includes process and
calling-workflow identity, inputs, script, referenced globals, bundled scripts,
and declared container/Conda/Spack environment. Both `.nextflow/cache` and the
work directory must be preserved
([Nextflow caching and resuming](https://docs.seqera.io/nextflow/cache-and-resume)).
That is strong recovery behaviour for self-contained processes, but makes the
cache and work trees durable operational assets rather than disposable scratch.

Nextflow has the richest built-in run telemetry of the three candidates. It can
emit an HTML execution report with status and CPU/memory/I/O metrics, an HTML
timeline, a tabular trace, queryable execution logs, and an HTML/image workflow
DAG; the DAG can also be previewed without task execution
([Nextflow reports](https://docs.seqera.io/nextflow/reports)). As with
Snakemake, these execution reports complement rather than replace the
OpenGWASDB-specific review evidence.

### Trade-offs and unresolved fit questions

The biggest fit question is the Store's lifecycle. Nextflow explicitly warns
that a process which modifies its input files cannot be resumed and calls that
behaviour an anti-pattern
([Nextflow modified-input warning](https://docs.seqera.io/nextflow/cache-and-resume#modified-inputs)).
Current release notes describe rho matrices built in place—for example, the
[FinnGen completed Release Build Recipe](../families/finngen-r13/releases/r13-pilot-20-completed/build.yaml).
A Nextflow implementation would therefore have to choose and prove one of two
patterns:

1. Keep the Store within one task-owned work directory through every in-place
   phase, then publish it only after final validation. This respects the cache
   model but makes one coarse, potentially many-hour task unless each phase can
   hand a new immutable Store directory to the next.
2. Let processes update a stable external Store path and emit validated marker
   files which Nextflow tracks. This preserves phase granularity but moves
   correctness outside Nextflow's normal input/output cache model; the adapter
   must detect partial mutation and establish idempotent recovery.

Nextflow normally executes each task in a hash-named work directory and
publishes selected outputs elsewhere. Current workflow-output documentation
uses symbolic links by default and offers copy mode for durable independent
outputs; it cautions that deleting the work directory breaks symlinked results
([Nextflow workflow outputs](https://training.nextflow.io/latest/hello_nextflow/01_hello_world/#publish-outputs)).
For a large final Store, copying could double both I/O and temporary storage,
while a symlink makes the Release Artifact depend on retained cache storage.
Placing the Nextflow work root under the release's `/data` work directory
avoids the Git worktree and keeps resume data on the large filesystem, but the
finalisation/storage pattern still needs an explicit test.

Nextflow also introduces a runtime and language boundary absent from the
current workspace. It requires Java 17 or later. The official installer offers
a Conda package, which Pixi can resolve from the repository's existing
channels, but the same Nextflow page warns that its Conda distribution can be
outdated and can encounter dependency or Java compatibility conflicts; it
recommends the self-installing distribution for freshness
([Nextflow installation](https://docs.seqera.io/nextflow/install)). Choosing
Nextflow would therefore require a deliberate reproducible packaging choice:
either validate and pin the Bioconda/Java resolution in `pixi.lock`, or pin and
verify a standalone Nextflow distribution while retaining `pixi run` as the
operator entry point. A self-updating, unpinned executable would conflict with
the existing locked-environment contract.

This is a concrete current-server requirement, not just a theoretical one. On
2026-09-10, `java -version` on the server's ambient `PATH` reported OpenJDK
`1.8.0_502`, while neither `nextflow` nor `snakemake` was present on that
`PATH`. Current Nextflow therefore cannot rely on the ambient JVM; its locked
Pixi feature would also have to supply a compatible Java runtime.

The dataflow DSL provides future portability and parallelism that are not
required by the current destination. They are not harms in themselves, but the
Groovy/Nextflow concepts and task-staging model are additional maintenance
surface for a workflow currently operated one Release at a time on one server.

## Thin repository-owned orchestrator

### Capabilities relevant here

A thin orchestrator would be a Python command in the existing Pixi environment,
for example a small stage registry plus a persistent run record. It could call
R, Python, and OpenGWASDB commands with `subprocess.run`; Python's standard
library exposes exit status and captured output, and `check=True` converts a
non-zero status into an exception
([Python subprocess documentation](https://docs.python.org/3/library/subprocess.html#subprocess.run)).
No second DSL or runtime is introduced, and the operator interface remains
exactly `pixi run <task>`.

This option can represent the domain directly:

- stage enablement is a predicate over the accepted Build Recipe and Store
  Layout;
- the Preflight Review Gate is an explicit state transition;
- every stage writes the report shape already expected by the Release Bundle;
- work, logs, and checkpoints use the existing Family-first `/data` layout;
- in-place top-hit and rho operations can be named as mutations of a
  not-yet-final Store rather than forced into an immutable-file DAG; and
- the command can refuse a second concurrent run, consistent with the
  one-Release-at-a-time operating model.

A small JSON/YAML record can be written to a temporary sibling and atomically
renamed into place on the same filesystem; Python documents a successful
`os.replace` as atomic, while warning that replacement can fail across
filesystems
([Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace)).
Alternatively, SQLite is already present in the development feature and
supports explicit commit and rollback transactions
([Python SQLite transaction control](https://docs.python.org/3/library/sqlite3.html#transaction-control)).
Either mechanism is adequate for *recording* state on one server. Neither makes
the large Store mutation itself transactional.

### Trade-offs and unresolved fit questions

The thin option has the lowest packaging and adaptation cost but the highest
semantic ownership cost. The repository would have to define and test all the
behaviour an established workflow engine supplies:

- dependency ordering and cycle rejection;
- input, configuration, code, and software-environment fingerprints;
- atomic transitions among pending, running, failed, and validated states;
- stale-state and interrupted-process recovery;
- stage invalidation and forced reruns;
- single-run locking and signal handling;
- stdout/stderr log capture and retention;
- dry-run/status/graph or equivalent transparency;
- report aggregation; and
- safe cleanup that never mistakes a durable Release Artifact for scratch.

An `exit_code == 0` or `path.exists()` test is not sufficient. Every successful
stage needs a domain validator and an input-bound completion record. Without
that discipline, a short implementation would appear resumable while silently
accepting a partially written Store after a crash. It is possible to keep the
implementation genuinely thin because there is no queue and no remote
executor, but resumability, provenance, and operator visibility are core
requirements and cannot be omitted in the name of thinness.

This approach provides no generic HTML timeline, resource plot, or DAG. The
existing domain reports may make a custom compact status page more useful than
generic telemetry, but that page—and every metric feeding it—would be new code
owned here.

## Cross-cutting findings

### The approval gate is a domain artifact

The gate should be two invocations for every candidate, not an interactive task
waiting in memory:

1. `preflight` automatically produces a Preflight Review Packet and exits
   successfully at the gate.
2. A separate approval action records the operator, timestamp, decision, and
   cryptographic identities of the Source Inventory, generated Release
   Manifest, Build Recipe, Preflight parameters, and reports reviewed.
3. `full` validates that approval still matches those exact inputs before doing
   expensive work.

Snakemake targets, Nextflow named workflows/parameters, and a thin state machine
can all enforce this shape. Merely touching an `approved` file is too weak: it
does not say what was reviewed and can incorrectly authorise a changed manifest
or recipe.

### Engine reports and scientific reports are different

Snakemake and Nextflow both provide useful execution-level reporting, while a
thin orchestrator would have to build it. None can infer whether ancestry,
effect scale, filtering, top hits, a rho matrix, or read-back queries look
sensible. Each phase therefore needs a stable report contract independent of
the chosen engine. The Preflight Review Packet should link or embed those small
domain reports and may additionally include the engine's DAG, timeline, and
resource summary.

### Resume must be validation-backed

The workflow should treat each expensive phase as complete only when all of the
following agree:

- the phase's declared inputs and parameters have the recorded fingerprints;
- the repository commit, OpenGWASDB commit, and `pixi.lock` digest match the
  recorded execution environment;
- the stage process completed;
- the expected outputs remain present; and
- a phase-specific validator passes against those outputs.

This is stricter than any engine's default freshness test, but it is necessary
because some large phases update one Store directory in place. It also follows
the repository's existing software-management requirement to record commits,
the lock digest, versions, command, and external-artifact root
([software-management proposal](software-management-proposal.md#task-interface)).
Pixi's lock file supplies exact resolved package records, and production runs
can use `pixi run --locked` or `--frozen` to prevent an implicit environment
change
([Pixi lock files](https://pixi.prefix.dev/latest/workspace/lock_file/),
[Pixi run options](https://pixi.prefix.dev/latest/reference/cli/pixi/run/)).

### Source verification should be its own reusable phase

Neither engine's default freshness check substitutes for the Source
Inventory's checksums. For input files above its small-file threshold,
Snakemake normally uses modification times; Nextflow's standard file hash uses
the full path, modification time, and size
([Snakemake timestamp behaviour](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html#ignoring-timestamps),
[Nextflow input hashing](https://docs.seqera.io/nextflow/cache-and-resume#modified-inputs)).
The workflow should verify acquired files against the Source Inventory once,
emit a small verification record bound to the inventory digest, and make later
phases consume that record. This preserves end-to-end checksum evidence without
rehashing every large source file merely to plan each resumed run.

### State belongs beside external work, not in the Git checkout

Long-run cache, checkpoints, raw logs, and large reports should live under a
release-specific work location such as:

```text
/data/opengwasdb/<family>/releases/<release>/work/workflow/
```

This applies to `.snakemake` state, Nextflow's `.nextflow/cache` plus `work`, or
the thin orchestrator's run records. Small accepted summaries can be copied or
generated into the Release Bundle. Keeping engine state release-scoped also
prevents one Release's resume history from being selected accidentally for
another.

### A common phase interface is useful before choosing the engine

The engine choice does not need to determine the Store Build API. Each phase
can have the same conceptual contract in all three implementations:

| Field | Purpose |
|---|---|
| `phase_id` | Stable name used in reports and resume state |
| `enabled` | Pure predicate over the accepted Build Recipe and predecessor evidence |
| `inputs` | Explicit files, artifact URIs, reference resources, and fingerprints |
| `command` | Repository-owned adapter or OpenGWASDB entry point, executed inside Pixi |
| `outputs` | Durable artifacts plus a small report and log pointer |
| `validator` | Read-back or structural check required before completion is recorded |
| `mutation_mode` | `new-output` or explicitly declared `in-place-update` |
| `resources` | Cores, memory, scratch, and worker parameters |

Keeping that interface engine-neutral prevents the workflow definition from
becoming a new copy of the layout-specific release seam.

## What would discriminate among the candidates

The documentation comparison leaves three empirical questions that are
material on this server:

1. Can a representative Store directory pass build → interrupt → resume →
   in-place top-hit/rho → validate without unsafe deletion or a second full
   copy under Snakemake's `directory()`/`update()` semantics?
2. Can Nextflow retain phase-level resume while producing the final Store at
   its durable Family-first path without mutating a cached input, duplicating
   the Store, or making the artifact depend on a disposable work-directory
   symlink?
3. How much code is actually required for a thin orchestrator to demonstrate
   input-bound state, crash recovery, dry-run/status visibility, the approval
   gate, and validation-backed resumption—not just sequential subprocess calls?

A later prototype can run the same small, mixed-layout Trial Store fixture
through those failure scenarios and measure disk amplification, resume time,
and clarity of the resulting review evidence. Those results, together with the
maintenance appetite for a Python DSL, a Nextflow DSL/JVM, or owned workflow
semantics, are the facts needed to choose; the current evidence does not make
that choice.
