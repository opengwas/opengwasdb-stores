#!/usr/bin/env python3
"""Test orchestrator (issue #41): owns ordering and cleanup for every
checked-in lightweight test suite and runs each one under the interpreter
already selected by Pixi (`sys.executable` / `Rscript` on PATH), rather than
having individual test scripts search for a sibling `../opengwasdb/.venv`.

  pixi run test              # every suite
  pixi run test-python       # only the Python suites
  pixi run test-r            # only the R suites
  pixi run test --suite workflow       # named suites only (repeatable)
  pixi run test --skip-exclusive       # all but the suites that run alone
  pixi run test --list-exclusive       # those suites, as JSON, for CI's matrix

Suites are data-only, network-free, and safe to run in CI; they are distinct
from the data-intensive LD-panel acquisition/materialization tasks.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

Suite = tuple[str, str, list[str]]  # (label, kind, command relative to REPO_ROOT)
# A Python file run as named halves: file -> [(label, extra arguments)].
Splits = dict[str, list[tuple[str, list[str]]]]

# R suites are listed: discovery finds only Python ones (#257).
R_SUITES: list[Suite] = [
    ("emit-registry (R)", "r", ["tests/emit-registry/run_tests.R"]),
    ("indexed-subsets-generator-defaults (R)", "r", ["tests/indexed-subsets/test_generator_defaults.R"]),
    ("finngen-r13-pilot", "r", ["tests/finngen-r13-pilot/run_tests.R"]),
    ("metadata-resolvers/gwas-catalog-ssf", "r", ["tests/metadata-resolvers/gwas-catalog-ssf/run_tests.R"]),
    ("metadata-resolvers/finngen-manifest", "r", ["tests/metadata-resolvers/finngen-manifest/run_tests.R"]),
    ("metadata-resolvers/opengwas-api", "r", ["tests/metadata-resolvers/opengwas-api/run_tests.R"]),
    ("metadata-resolvers/trait-ontology-mapping", "r", ["tests/metadata-resolvers/trait-ontology-mapping/run_tests.R"]),
    ("materialise-gwas-ssf-ragged", "r", ["tests/materialise-gwas-ssf-ragged/run_tests.R"]),
    ("opengwas-gwas-vcf-dense", "r", ["tests/opengwas-gwas-vcf-dense/run_tests.R"]),
    ("schema-validation", "r", ["tests/schema-validation/run_tests.R"]),
]

# A Python suite CI runs as separate halves, each on a runner of its own.
SPLITS: Splits = {
    "tests/workflow/test_release_faults.py": [
        ("release-faults/boundaries", ["-k", "TestEveryBoundaryThroughTheEntryPoint"]),
        ("release-faults/interrupts", ["-k", "TestLiveAndInterruptedRuns"]),
    ],
}


def _suite_files(tests_root: Path) -> list[Path]:
    return sorted(
        p for p in tests_root.rglob("*.py")
        if "__pycache__" not in p.parts and (p.name.startswith("test_") or p.name == "run_tests.py")
    )


def discover_suites(tests_root: Path, *, r_suites: list[Suite], splits: Splits) -> list[Suite]:
    """Every Python suite under `tests_root`, then `r_suites` (#257).

    A Python suite is a `test_*.py` or `run_tests.py`, run as a script. It is
    named after its directory when that holds no other Python suite, when it
    is a `run_tests.py`, or when the file is named after the directory
    (`workflow/test_workflow.py`); otherwise `<directory>/<file>`, without
    `test_` and with `-` for `_`. A name an R suite already has falls back to
    the `<directory>/<file>` form. A file in `splits` runs as its named halves
    instead. Two suites with one name are refused rather than one hiding the
    other.
    """
    files = _suite_files(tests_root)
    per_dir: dict[Path, int] = {}
    for path in files:
        per_dir[path.parent] = per_dir.get(path.parent, 0) + 1
    r_labels = {label for label, _, _ in r_suites}
    suites: list[Suite] = []
    for path in files:
        command = str(Path(tests_root.name) / path.relative_to(tests_root))
        if command in splits:
            suites.extend((label, "python", [command, *extra]) for label, extra in splits[command])
            continue
        directory = str(path.parent.relative_to(tests_root))
        stem = path.stem.removeprefix("test_").replace("_", "-")
        named_after_directory = (
            per_dir[path.parent] == 1 or path.name == "run_tests.py" or stem == path.parent.name
        )
        label = directory if named_after_directory and directory not in r_labels else f"{directory}/{stem}"
        suites.append((label, "python", [command]))
    suites += list(r_suites)
    labels = [label for label, _, _ in suites]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(f"two test suites share a name: {', '.join(duplicates)}")
    return suites


SUITES: list[Suite] = discover_suites(REPO_ROOT / "tests", r_suites=R_SUITES, splits=SPLITS)


# Most suites get single-threaded BLAS. A suite that imports numpy, scipy or
# opengwasdb otherwise spins up a thread pool per process: importing the Analysis
# model alone measured 0.3s of wall clock against 17s of user time, and with
# suites running concurrently that contention costs far more than the threads win.
#
# The exception is a suite that does real numerical work rather than just
# importing the libraries. Pinning `workflow`, which builds fixture-scale Stores
# through opengwasdb for real, took it from 43s to 122s. Measure before adding
# to this set: for every other suite here, pinning is free.
#
# `release-faults` (#195) runs alone too, and after `workflow`, never beside it:
# it SIGKILLs whole Snakemake runs and then runs `snakemake --unlock`, which
# clears every lock in the shared `.snakemake/` directory. Any Snakemake run
# also removes that whole directory of locks when it exits. CI gives each of
# these suites a runner, and so a checkout and `.snakemake/`, of its own
# (`--list-exclusive`).
EXCLUSIVE_ORDER: tuple[str, ...] = ("workflow", "release-faults/boundaries", "release-faults/interrupts")
THREADED_SUITES: frozenset[str] = frozenset(EXCLUSIVE_ORDER)

# Rough per-suite cost in seconds, used only to schedule the long suites first
# so they overlap with the short ones instead of starting last and running alone.
# Wrong values cost a little wall clock, never correctness; unlisted suites are
# assumed short.
SUITE_COST: dict[str, float] = {
    "workflow": 45.0,
    "run": 5.0,
    "materialise-gwas-ssf-ragged": 4.0,
    "schema-validation": 4.0,
    "plan": 2.5,
    "readiness": 3.0,
    "migration/id-reorder": 1.0,
    # The #252 guard walks every tracked file and re-checks six bundles.
    "migration/families-retired": 6.0,
    "opengwas-gwas-vcf-dense": 2.5,
    "finngen-r13-pilot": 2.5,
    "ld-panel-eigendecomposition": 2.0,
    "dbgap-mvp/end-to-end": 3.0,
    "dbgap-mvp/ancestry-policy": 2.0,
    # One subprocess preflight plus in-process cases over a frozen fixture (#290).
    "dbgap-mvp/selection-partition": 4.0,
    # The five dbGaP snapshots' provenance sidecars list each file's reformat
    # lineage; they are parsed once per suite, not once per test (#290).
    "release-config": 9.0,
}

THREAD_ENV: dict[str, str] = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

DEFAULT_JOBS: int = min(8, os.cpu_count() or 1)


def run_suite(label: str, kind: str, script: list[str]) -> tuple[bool, float, str]:
    """Run one suite to completion, returning its verdict, elapsed time and output.

    Output is captured rather than streamed because suites run concurrently and
    interleaved output is unreadable. It is printed for failures only.
    """
    if kind == "python":
        cmd = [sys.executable, *script]
    elif kind == "r":
        rscript = shutil.which("Rscript")
        if rscript is None:
            return False, 0.0, f"{label}: SKIPPED (Rscript not found on PATH)"
        cmd = [rscript, *script]
    else:
        raise ValueError(f"unknown suite kind: {kind}")

    env = dict(os.environ) if label in THREADED_SUITES else {**os.environ, **THREAD_ENV}
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    elapsed = time.monotonic() - start
    return result.returncode == 0, elapsed, result.stdout + result.stderr


USAGE: str = (
    "[--only python|r] [--suite LABEL]... [--skip-exclusive] [--list-exclusive] [--jobs N]"
)


def foreign_install() -> str | None:
    """The reason this environment would test other code than this checkout's, or None (#257).

    `ogstores` installs editable, so it imports from the checkout whose
    `pixi install` made the environment. This runner under another checkout's
    environment would test the other checkout's code.
    """
    try:
        import ogstores
    except ImportError:
        return "ogstores is not installed in this environment; run `pixi install` in this checkout"
    installed = Path(ogstores.__file__).resolve().parents[2]
    if installed == REPO_ROOT:
        return None
    return (
        f"this environment's ogstores is installed from {installed}, not {REPO_ROOT}; "
        "run `pixi install` in this checkout and use its own environment"
    )


def main() -> int:
    only: str | None = None
    named: list[str] = []
    skip_exclusive = False
    jobs: int = DEFAULT_JOBS

    args = sys.argv[1:]
    while args:
        flag = args.pop(0)
        if flag == "--only" and args:
            only = args.pop(0)
        elif flag == "--suite" and args:
            named.append(args.pop(0))
        elif flag == "--skip-exclusive":
            skip_exclusive = True
        elif flag == "--list-exclusive":
            print(json.dumps(list(EXCLUSIVE_ORDER)))
            return 0
        elif flag == "--jobs" and args:
            jobs = max(1, int(args.pop(0)))
        else:
            print(f"usage: {sys.argv[0]} {USAGE}", file=sys.stderr)
            return 2

    foreign = foreign_install()
    if foreign:
        print(f"refusing to run: {foreign}", file=sys.stderr)
        return 2

    unknown = sorted(set(named) - {label for label, _, _ in SUITES})
    if unknown:
        print(f"no such suite: {', '.join(unknown)}", file=sys.stderr)
        return 2
    suites = [
        s for s in SUITES
        if (only is None or s[1] == only)
        and (not named or s[0] in named)
        and not (skip_exclusive and s[0] in THREADED_SUITES)
    ]
    if not suites:
        print("no suites match the selection", file=sys.stderr)
        return 2

    # Suites are independent processes over their own fixtures, so they run
    # concurrently. Wall clock is then bounded by the slowest single suite
    # rather than by their sum.
    # Two groups, because they want opposite things from the machine. The many
    # short suites are mostly process startup, so they run concurrently. A
    # threaded suite does real numerical work and wants every core: running
    # `workflow` alongside seven others took it from 43s to 145s, which is
    # slower than simply giving it the machine to itself.
    shared = [s for s in suites if s[0] not in THREADED_SUITES]
    exclusive = sorted((s for s in suites if s[0] in THREADED_SUITES), key=lambda s: EXCLUSIVE_ORDER.index(s[0]))

    results: list[tuple[str, bool, float, str]] = []

    def record(label: str, outcome: tuple[bool, float, str]) -> None:
        ok, elapsed, output = outcome
        print(f"  {'PASS' if ok else 'FAIL':4s}  {label}  ({elapsed:.1f}s)", flush=True)
        results.append((label, ok, elapsed, output))

    started = time.monotonic()

    if shared:
        print(f"running {len(shared)} suites, {jobs} at a time\n")
        # Longest first, so a slow suite overlaps with the short ones rather
        # than starting last and running alone.
        order = sorted(shared, key=lambda s: -SUITE_COST.get(s[0], 1.0))
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(run_suite, label, kind, script): label
                for label, kind, script in order
            }
            for future in as_completed(futures):
                record(futures[future], future.result())

    for label, kind, script in exclusive:
        print(f"\nrunning {label} with the machine to itself\n")
        record(label, run_suite(label, kind, script))

    wall = time.monotonic() - started

    order = {label: n for n, (label, _, _) in enumerate(suites)}
    results.sort(key=lambda r: order[r[0]])
    failed = [(label, output) for label, ok, _, output in results if not ok]

    for label, output in failed:
        print("=" * 60)
        print(f"FAILED: {label}")
        print(output.rstrip())

    print("=" * 60)
    for label, ok, elapsed, _ in results:
        print(f"  {'PASS' if ok else 'FAIL':4s}  {label}  ({elapsed:.1f}s)")
    serial = sum(elapsed for _, _, elapsed, _ in results)
    print(
        f"{len(results) - len(failed)}/{len(results)} suites passed "
        f"in {wall:.1f}s wall ({serial:.1f}s serial)"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
