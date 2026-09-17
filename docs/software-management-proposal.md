# Unified software management proposal

Status: proposed. This document turns the inventory in
`software-management-audit.md` into one repository-wide execution contract.

## Decision

Adopt **Pixi** at the repository root, with one committed `pixi.toml` and
`pixi.lock`. The sole user-facing execution interface becomes:

```text
pixi run <task>
```

Pixi is the deep environment module: its small interface hides Conda/Bioconda
resolution, Python and R runtimes, native executables, environment activation,
and task invocation. The repository's scripts remain implementations behind
that interface. Artifact/reference roots remain data configuration and never
contain software environments.

Pixi is a particularly good fit here because one workspace can compose reusable
features into smaller environments, attach tasks to those environments, and
record all resolved environments in one lock file. Its documented examples
explicitly cover small tool environments, docs environments, developer
supersets, and CI selection of named environments. See the official
[multi-environment documentation](https://pixi.prefix.dev/latest/workspace/multi_environment/).

## Workspace shape

Use Conda packages for Python, R, compiled libraries, Quarto, and bioinformatics
executables. Use PyPI only for packages unavailable or intentionally sourced
there. Configure `conda-forge` and `bioconda` once at workspace level.

Suggested features:

| Feature | Contents | Used by |
|---|---|---|
| `python` | Python, NumPy, SciPy, PyYAML | Python utilities and tests |
| `store-build` | pinned install of `opengwasdb` | ancestry/build integration |
| `r` | R, data.table, yaml | generators and R tests |
| `reporting` | tidyverse, ggplot2, knitr, scales, jsonlite, curl, Quarto | reports/site |
| `ld-panel` | bcftools, plink2, Python/SciPy | panel acquisition and generation |
| `dev` | lint/test-only tools | contributor and CI checks |

Suggested environments:

- `default`: Python + R + store-build; normal generator/test work.
- `docs`: R + reporting.
- `ld-panel`: Python + ld-panel; no reporting stack.
- `dev`: default + reporting + ld-panel + development tools.

This avoids installing the full stack for every job while retaining a single
manifest and lock. Environments that must share versions should use a solve
group, so the tested version is the production version.

## Task interface

The first task set should be deliberately small:

```text
pixi run test
pixi run test-python
pixi run test-r
pixi run docs
pixi run ld-acquire -- <arguments>
pixi run ld-materialize -- <arguments>
pixi run validate-environment
```

`test` owns test ordering and cleanup. Callers should not locate a sibling
virtualenv, activate Conda, or know which interpreter runs a script. Tests invoke
`sys.executable` or the executable already selected by Pixi.

`validate-environment` should report tool versions and fail early when a required
executable is unavailable. Long-running job provenance should record the git
commit, `pixi.lock` hash, task name, arguments, and external artifact root.

## Paths and configuration

Use explicit data variables with repository defaults documented in one place:

```text
OPENGWASDB_ARTIFACT_ROOT=/data/opengwasdb/stores
OPENGWASDB_REFERENCE_ROOT=/data/opengwasdb/reference
```

`OPENGWASDB_ARTIFACT_ROOT` is the directory that contains one directory per
Store Release id (`<artifact-root>/OGS-00042/`), per ADR 0022. It is now
actually read by `paths.artifact_root()`, which resolves it after a workflow
`--config artifact_root=` override and before the committed `ogstores.yaml`
and the built-in default (issue #126).

Software lives in Pixi's workspace/cache, not beneath either data root. Remove
hard-coded sibling `.venv` discovery and the LD materializer's
`<reference-root>/tools/bin/plink2` lookup. Resolve executables from the Pixi
environment (`PATH`) and accept explicit overrides only for debugging.

The current temporary environment at
`/data/opengwasdb/reference/hgdp1kgp-hg38/tools` must remain until the active
panel job finishes, then can be deleted after the locked Pixi environment is
installed and the job's tool versions are recorded.

## Why not parallel managers?

- `renv` provides strong R isolation and lock/restore semantics, but it would
  still need a second manager for Python, Quarto, bcftools, and plink2. Its own
  documentation notes that source restoration can depend on system libraries.
  See [renv project environments](https://rstudio.github.io/renv/) and
  [package installation](https://rstudio.github.io/renv/articles/package-install.html).
- A plain `environment.yml` covers the languages and native tools but does not
  provide the repository task interface. Modern Conda supports exact lockfiles,
  including `pixi.lock`, so choosing Pixi does not abandon the Conda ecosystem;
  see the official [Conda environment documentation](https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html).
- Separate `uv` + `renv` + Conda environments would produce three dependency
  interfaces and three upgrade workflows. That reduces locality and makes it
  easy for CI, documentation, and production jobs to exercise different stacks.

## Migration plan

1. Add `pixi.toml`, resolve and commit `pixi.lock`, and pin the minimum Pixi
   version in the workspace.
2. Add the named tasks and make all current test suites pass through them.
3. Declare `opengwasdb` as an explicit pinned dependency rather than borrowing
   `../opengwasdb/.venv`; update tests and generator documentation.
4. Replace hard-coded tool/interpreter paths with `PATH` lookup and central root
   configuration. Add environment validation and provenance capture.
5. Add CI that installs from the lock without updating it, runs `test`, and
   separately exercises `docs` and the lightweight LD-panel synthetic tests.
6. Update all README command examples to `pixi run ...` and remove obsolete
   activation instructions.
7. Once the active LD build completes, verify its recorded bcftools/plink2
   versions against the lock and remove its data-local Conda environment.

## Acceptance criteria

- A fresh checkout needs only Pixi plus access to declared external data.
- Every checked-in test and document build has a named task.
- CI and local execution consume the committed lock without implicit solving.
- No script reaches into a sibling checkout's virtualenv or a reference-data
  directory for software.
- Runtime provenance identifies the lock hash and exact native tool versions.
- Updating software is one reviewed manifest/lock change, with all task suites
  passing before merge.
