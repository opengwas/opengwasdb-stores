# Software management audit

Date: 2026-08-05

## Executive summary

The repository has no unified, machine-readable software environment. There is
no Python project/requirements file, R lockfile, Conda environment, container,
tool-version file, Makefile, or CI workflow in the tree. Software requirements
are instead implicit in imports, subprocess calls, shebangs, prose, and an
external sibling checkout. Consequently a fresh checkout cannot be reproduced
from repository metadata, and release-level script hashes preserve generator
identity but not the software environment that executed the generator.

The workloads naturally divide into one shared environment plus two explicit
external integrations:

1. Python/R report, generator, validation, and test code.
2. Bioinformatics command-line tools (`plink2`, `bcftools`) used by LD-panel
   acquisition and construction.
3. The separately developed `opengwasdb` Python package, currently consumed
   from a user-supplied checkout rather than declared as a package dependency.

The recommended target is a repository-owned Pixi workspace for Python, R,
Quarto, and native tools, with one manifest, one lock, and named tasks; `renv`
should not be added as a second competing resolver. CI should install the locked
environments and run all lightweight tasks. `opengwasdb` should be declared at
an immutable Git revision (or released package version) instead of being found
through `PYTHONPATH`. Tools must live in the environment, never below a
reference-data directory. The detailed decision is in
[`software-management-proposal.md`](software-management-proposal.md).

## Scope and method

This is a static audit of tracked source files. Generated HTML was excluded as
software evidence, as were local `.git` and `.claude` state and the contents of
`/data`. The repository explicitly says that it stores metadata and small
reports while large source/build artifacts live externally
([README.md](../README.md#L85-L87)); that separation should also apply to
software environments.

## Inventory

### Languages and runtimes

| Runtime | Evidence and use | Version status |
|---|---|---|
| Python | Python 3 shebangs are used by the LD acquisition and generation scripts ([acquire_hgdp1kgp.py](../resources/scripts/ld-panel/acquire_hgdp1kgp.py#L1-L6)), tests ([run_tests.py](../tests/ld-panel-generation/run_tests.py#L1-L8)), and the Store workflow phase runner ([phase.py](../workflow/phase.py#L1)) (`#103` deleted the three `build-store.py` adapters, the last scripts with a non-3 `#!/usr/bin/env python` shebang). | No version declared. Syntax such as `X | None` requires Python 3.10+, while `datetime.UTC` in `resources/lib/release_yaml.py` requires 3.11+, making 3.11 the effective minimum. |
| R | Generators and tests run as `Rscript`; the shared generator workflow documents direct invocations ([generator README](../resources/generators/gwas-ssf-ragged/README.md#L6-L21)). | No R version declared or locked. |
| Bash | The site build is a Bash script with strict mode ([build-site.sh](../resources/scripts/build-site.sh#L1-L16)). | No Bash/platform version declared. It also assumes GNU-like utilities. |
| Quarto | Quarto renders the reports in the site build ([build-site.sh](../resources/scripts/build-site.sh#L21-L28)). | No version declared. Only prose says to use an activated Conda environment ([build-site.sh](../resources/scripts/build-site.sh#L6-L7)). |
| YAML/TSV/Markdown/QMD | Configuration, release metadata, manifests, documentation, and executable reports. Release build metadata names an import-style builder entry point ([build.yaml](../families/pqtl-interval-2018/releases/2018-sun-pilot-100/build.yaml#L1-L10)). | Schema versions exist for domain metadata, but do not constrain software. |

### Python packages

The only direct third-party scientific imports in this repository are:

- `numpy`: LD construction, eigendecomposition, fixtures/tests, and the consumer
  smoke test (for example [construct_block.py](../resources/scripts/ld-panel/construct_block.py#L11-L21)).
- `scipy` (`scipy.linalg`): LD eigendecomposition
  ([backfill_eigendecomposition.py](../resources/scripts/ld-panel/backfill_eigendecomposition.py#L58-L66)).
- `opengwasdb`: store construction/read-back, ancestry assignment, variant
  normalization, and LD consumption. The Store workflow shells out to its CLI
  ([phase.py](../workflow/phase.py#L188-L195)) and reads a Store's Zarr groups
  through its Python API ([phase.py](../workflow/phase.py#L607-L620)).

Everything else imported is from the Python standard library or local
`resources.lib` code. None of these packages has a declared version constraint.
The documented workflow points `PYTHONPATH` at a separate checkout and invokes
that checkout's virtual-environment Python
([generator README](../resources/generators/gwas-ssf-ragged/README.md#L17-L21),
[ancestry workflow](../resources/generators/gwas-ssf-ragged/README.md#L63-L68)).
The LD consumer similarly accepts an `--opengwasdb-repo` path and mutates
`sys.path` ([smoke_test_consumer.py](../resources/scripts/ld-panel/smoke_test_consumer.py#L9-L18)).

### R packages

Direct package usage comprises:

- Core pipeline/tests: `data.table`, `yaml` (for example
  [generate.R](../resources/generators/gwas-ssf-ragged/generate.R#L1-L3) and
  [run_tests.R](../tests/effect-scale-validation/run_tests.R#L11-L15)).
- Network-assisted SomaScan metadata generation: `curl` alongside `data.table`
  ([generate-somascan-targets.R](../scripts/somascan/generate-somascan-targets.R#L1-L6)).
- Curation/dashboard: `tidyverse`, `data.table`, and `jsonlite` (the broad
  `tidyverse` dependency is loaded in [ebi-studies.r](../resources/scripts/ebi-studies.r#L32-L33)).
- Reports: `dplyr`, `ggplot2`, `knitr`, `scales`, `tibble`, `tidyr`, `DT`, and
  `data.table`; the beta-scale report lists most explicitly
  ([beta-scale-estimation.qmd](../resources/scripts/beta-scale-estimation.qmd#L18-L24))
  and the curation report also calls `DT::datatable` and `knitr::kable`.

No package versions or repository snapshot are recorded. Some dependencies are
declared only through namespace calls rather than `library()`, which makes an
informal package list easy to miss.

### External executables and operating-system facilities

| Executable/facility | Consumers | Declaration/pinning |
|---|---|---|
| `plink2` | Block extraction/export ([construct_block.py](../resources/scripts/ld-panel/construct_block.py#L133-L166)) and VCF-to-PGEN conversion ([materialize_reference.py](../resources/scripts/ld-panel/materialize_reference.py#L15-L28)). | Not versioned. Usually PATH-resolved, but materialization uniquely hard-codes `<reference-root>/tools/bin/plink2`. |
| `bcftools` | Header verification and index checks ([acquire_hgdp1kgp.py](../resources/scripts/ld-panel/acquire_hgdp1kgp.py#L25-L31), [same file](../resources/scripts/ld-panel/acquire_hgdp1kgp.py#L64-L71)). | Not versioned; PATH-resolved. |
| `curl` CLI | Resumable gnomAD downloads ([acquire_hgdp1kgp.py](../resources/scripts/ld-panel/acquire_hgdp1kgp.py#L12-L22)). | Not versioned; distinct from the R `curl` package. |
| GitHub CLI `gh` | Checks whether an external consumer issue is closed before smoke testing ([smoke_test_consumer.py](../resources/scripts/ld-panel/smoke_test_consumer.py#L13-L16)). | Not declared/versioned; also introduces network/auth/API availability into a smoke test. |
| `git` | `resources/lib/release_yaml.py` discovers the repository root using `git rev-parse`. | Not declared/versioned. |
| GNU `du` | The ragged-store spike script measures store size with `du -sh` ([build-store.py](../resources/scripts/ragged-spike/build-store.py#L51-L52)); the three production `build-store.py` adapters that used `du -sb` were deleted in `#103`, and the workflow measures bytes in Python instead. | Undeclared platform assumption. |
| `Rscript`, `quarto`, `bash`, `cp` | Generators/tests and documentation build ([build-site.sh](../resources/scripts/build-site.sh#L21-L34)). | Only prose requirements, no versions. |

Standard-library gzip readers handle compressed files in both languages; no
direct `bgzip`/`tabix` subprocess dependency was found. `bcftools` supplies the
VCF/index validation behavior.

### Entry points and orchestration

There is no single project CLI or task definition. Users invoke scripts directly:

- R generator modes (`emit`, `filter`, `effect-scale`, and refresh operations)
  are documented in the shared generator README
  ([README](../resources/generators/gwas-ssf-ragged/README.md#L6-L42)).
- Python store construction and ancestry assignment are separate direct script
  invocations using an external Python interpreter
  ([README](../resources/generators/gwas-ssf-ragged/README.md#L17-L21),
  [README](../resources/generators/gwas-ssf-ragged/README.md#L63-L68)).
- LD panel acquisition, membership, construction, orchestration, calibration,
  backfill, and consumer checks are individual scripts in
  `resources/scripts/ld-panel/`; `run_panels.py` composes `construct_block.py`
  using the current interpreter.
- Tests are standalone Python/R scripts rather than a common test framework;
  the generation suite is run with `python3`
  ([test README](../tests/ld-panel-generation/README.md#L1-L4)).
- The site has its own Bash orchestration script.

Release metadata records a generator script SHA-256 and command
([release.yaml](../families/pqtl-interval-2018/releases/2018-sun-pilot-100/release.yaml#L14-L18)).
This is valuable provenance, but it omits interpreter, libraries, native tools,
platform, and the `opengwasdb` revision.

### Package managers, environments, CI, and containers

No tracked declaration was found for pip/uv/Poetry, Conda/Mamba, `renv`/pak,
Nix, Docker/Apptainer, runtime version managers, or system packages. There are
also no tracked GitHub Actions workflows or other CI definitions. The sole
environment-management statement is the site's comment that it expects an
activated Conda environment, but that environment is not defined
([build-site.sh](../resources/scripts/build-site.sh#L6-L7)).

The `.gitignore` deliberately separates generated/downloaded data from source
([.gitignore](../.gitignore#L1-L22)), but has no standard local-environment
entries such as `.venv/`, `.Rproj.user/`, or Conda prefix directories. This is
another signal that a repository-local environment contract has not yet been
established.

## Inconsistencies and risks

1. **No reproducible bootstrap.** A contributor cannot determine compatible
   Python, R, package, Quarto, PLINK, or bcftools versions from the checkout.
   Test success can depend on whatever happens to be installed on the host.
2. **Software is mixed with reference data.** `materialize_reference.py`
   hard-codes `args.root / "tools/bin/plink2"`
   ([lines 13-16](../resources/scripts/ld-panel/materialize_reference.py#L13-L16)),
   while `construct_block.py` defaults to PATH-resolved `plink2`
   ([lines 158-166](../resources/scripts/ld-panel/construct_block.py#L158-L166)).
   These two resolution policies can silently run different versions and explain
   why an entire Conda prefix appeared beneath `/data/opengwasdb/reference`.
3. **Two undeclared Python environments.** General tests use `python3`; store
   workflows use a sibling checkout's `.venv/bin/python`; two scripts use
   `#!/usr/bin/env python`; LD composition uses `sys.executable`. Results depend
   on invocation path.
4. **External package revision is unrecorded.** `opengwasdb` APIs are central to
   builds but are not pinned. The issue-state gate checks compatibility process,
   not the exact code executed.
5. **R dependency surface is implicit and broad.** `tidyverse` pulls a larger
   transitive set than the directly used packages, while report-only packages
   appear only inside QMD namespace calls. There is no lockfile or restore test.
6. **No continuous verification.** Standalone test scripts cover useful behavior,
   but nothing in the repository ensures they run together on a clean environment.
7. **Platform assumptions are undocumented.** GNU `du -sb`, Bash, and native
   bioinformatics binaries make the practical target Linux, yet no supported
   platform is stated.
8. **Provenance stops at source code.** Generator hashes and pinned data releases
   do not capture software versions, lockfile digest, commands of every pipeline
   stage, or platform. Rebuilding an immutable release therefore remains
   under-specified.

## Requirements for a unified solution

### 1. Adopt one locked Conda-compatible workspace

Add a human-maintained `pixi.toml` and generated `pixi.lock`. Use strict channels
and include:

- Python 3.11 (the effective current minimum), NumPy, SciPy, and the chosen
  packaging/bootstrap tools.
- R plus the enumerated R packages (`data.table`, `yaml`, `curl`, `jsonlite`,
  `dplyr`, `ggplot2`, `knitr`, `scales`, `tibble`, `tidyr`, `DT`; avoid the
  `tidyverse` metapackage unless its full surface is intentionally required).
- Quarto, PLINK2, bcftools, curl, Git, and GitHub CLI.

This repository spans Python, R, Quarto, and compiled bioinformatics tools;
Pixi uses the Conda ecosystem already assumed in project prose and can describe
the whole stack while also providing named environments and tasks. Adding
`renv` as well would create two authorities for R packages.
If exact cross-platform R locking proves unreliable, declare Linux x86-64 as the
supported production platform rather than pretending to offer an untested
portable environment.

Keep the installed prefix in a user/cache location such as
`/data/opengwasdb/software/envs/opengwasdb-stores/<lock-digest>` or the standard
Conda package/env cache. Never place it under
`/data/opengwasdb/reference/<resource-id>`; reference directories should contain
only source data, derived artifacts, manifests, and logs.

### 2. Make `opengwasdb` an immutable dependency

Prefer a released package version. Until one exists, pin an immutable Git commit
in the environment's pip subsection (or a small `pyproject.toml` dependency
group) and install it into the same environment. Remove `PYTHONPATH` mutation and
sibling `.venv` assumptions. If simultaneous development of both repositories is
required, document an explicit editable-install override as a developer-only
workflow; release production must record and use a commit.

### 3. Standardize executable discovery

Resolve `plink2`, `bcftools`, `curl`, and `gh` from PATH after environment
activation, with optional CLI overrides for exceptional deployments. Add a
preflight command that prints and validates every required executable and
package version. Remove `<reference-root>/tools/bin/plink2`; a reference root is
data configuration, not software configuration.

### 4. Add a thin, stable task interface

Define Pixi tasks that orchestrate existing scripts, for example `env-check`,
`test`, `test-python`, `test-r`, `docs`, `ld-acquire`, and `ld-materialize`.
Commands should use `python` and `Rscript` from the selected locked environment.
Standardize all Python shebangs on
`#!/usr/bin/env python3` (or avoid relying on shebangs in recipes).

The task interface should not hide data-intensive behavior: LD acquisition and
panel generation must remain explicit targets requiring a reference root, never
part of the default test target.

### 5. Add clean-environment CI

Add Linux CI that installs the Pixi environments (cached by lock digest)
and runs every lightweight test script plus a site-render smoke test. Large
downloads and full panel builds should be excluded; fixtures already allow the
LD numerical behavior to be tested cheaply. A scheduled or manually dispatched
job may exercise network-dependent metadata acquisition separately.

### 6. Extend release provenance

Alongside the existing generator hash, record:

- Git commits for this repository and `opengwasdb`.
- `pixi.lock` SHA-256 and environment/platform identifier.
- `python --version`, `R --version`, `plink2 --version`, `bcftools --version`,
  and `quarto --version`.
- The exact stage command and relevant non-secret environment configuration.

Store this as a small tracked or externally referenced build-environment
manifest. It complements, rather than replaces, release metadata's generator
hash.

## Suggested migration sequence

1. Create and lock `pixi.toml` from the inventory above; add an environment
   preflight script and verify all current lightweight tests.
2. Pin/install `opengwasdb` in that environment and eliminate sibling checkout
   and `PYTHONPATH` requirements.
3. Change LD scripts to PATH-based tools with optional overrides, then move or
   rebuild the current environment outside the reference resource directory.
   Do not move/delete an environment while active panel processes use it.
4. Introduce the task runner and CI using the same lock files.
5. Add the build-environment provenance manifest to the next generated release
   and reference-resource build.

This sequence leaves existing data and running jobs untouched while establishing
one authority for all future software installation and execution.
