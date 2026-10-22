#!/usr/bin/env python3
"""Test orchestrator (issue #41): owns ordering and cleanup for every
checked-in lightweight test suite and runs each one under the interpreter
already selected by Pixi (`sys.executable` / `Rscript` on PATH), rather than
having individual test scripts search for a sibling `../opengwasdb/.venv`.

  pixi run test              # every suite
  pixi run test-python       # only the Python suites
  pixi run test-r            # only the R suites
  pixi run test --suite workflow          # named suites only (repeatable)
  pixi run test --skip-release-stack      # all but the suites CI runs on runners of their own
  pixi run test --list-release-stack      # those suites, as JSON, for CI's matrix

Suites are data-only, network-free, and safe to run in CI; they are distinct
from the data-intensive LD-panel acquisition/materialization tasks.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

Suite = tuple[str, str, list[str]]  # (label, kind, command relative to REPO_ROOT)
# A Python file run as named parts: file -> [(label, extra arguments)].
Splits = dict[str, list[tuple[str, list[str]]]]

# R suites are listed: discovery finds only Python ones (#257).
R_SUITES: list[Suite] = [
    ("emit-registry (R)", "r", ["tests/emit-registry/run_tests.R"]),
    ("indexed-subsets-generator-defaults (R)", "r", ["tests/indexed-subsets/test_generator_defaults.R"]),
    ("metadata-resolvers/gwas-catalog-ssf", "r", ["tests/metadata-resolvers/gwas-catalog-ssf/run_tests.R"]),
    ("metadata-resolvers/trait-ontology-mapping", "r", ["tests/metadata-resolvers/trait-ontology-mapping/run_tests.R"]),
    ("materialise-gwas-ssf-ragged", "r", ["tests/materialise-gwas-ssf-ragged/run_tests.R"]),
    ("schema-validation", "r", ["tests/schema-validation/run_tests.R"]),
]

# A Python suite CI runs as separate parts, each on a runner of its own.
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
    the `<directory>/<file>` form. A file in `splits` runs as its named parts
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


# The suites that build Stores through the whole release stack. They used to
# run alone, one after another, for two reasons that no longer hold. Each
# process now has its own Snakemake working directory (see `run_job`), so a
# `snakemake --unlock`, or a run exiting, no longer clears another's locks. And
# they do not need the machine to themselves: with BLAS pinned to one thread, a
# fixture-scale `workflow` test measured the same wall clock (25.8s against
# 23.5s) on a twelfth of the CPU (48s of user time against 601s, on a 224-core
# host). CI gives each of them a runner of its own (`--list-release-stack`).
RELEASE_STACK_SUITES: tuple[str, ...] = ("workflow", "release-faults/boundaries", "release-faults/interrupts")

# The suites whose tests are spread over several processes, side by side,
# because together they take far longer than their slowest test. A
# release-stack test costs a fixture-scale release or more (15-25s, nearly all
# of it process start-up: Snakemake re-reads the Snakefile for every job, and
# every step starts the `opengwasdb` CLI); `phase-b-candidate` is 74 tests of
# 1-4s.
#
# Each process repeats its file's `setUpModule` and the `setUpClass` of every
# class it runs a test of, so there are only as many as there are jobs (see
# `shards`). One process per test on a 4-core CI runner took `workflow` from
# 264s to 567s: its class fixtures, repeated per test, took its CPU time from
# 264s to 2235s.
SHARDED_SUITES: frozenset[str] = frozenset({*RELEASE_STACK_SUITES, "phase-b-candidate"})

# Rough cost in seconds of one process, used only to start the long ones first
# so they overlap with the short ones instead of starting last and running
# alone. For a suite in `SHARDED_SUITES` it is the cost of one of its tests,
# with its fixture, and a process costs that per test it runs. Wrong values
# cost a little wall clock, never correctness; unlisted suites are assumed
# short.
SUITE_COST: dict[str, float] = {
    "release-faults/boundaries": 45.0,
    "release-faults/interrupts": 45.0,
    "workflow": 30.0,
    "phase-b-candidate": 3.0,
    "schema-validation": 12.0,
    "emit-registry (R)": 5.5,
    "run": 5.0,
    "materialise-gwas-ssf-ragged": 4.0,
    "plan": 3.0,
    "readiness": 3.0,
    "migration/id-reorder": 1.0,
    # The #252 guard walks every tracked file and re-checks six bundles.
    "migration/families-retired": 6.0,
    "ld-panel-eigendecomposition": 2.0,
    "dbgap-mvp/end-to-end": 3.0,
    "dbgap-mvp/ancestry-policy": 2.0,
    # One subprocess preflight plus in-process cases over a frozen fixture (#290).
    "dbgap-mvp/selection-partition": 4.0,
    # The five dbGaP snapshots' provenance sidecars list each file's reformat
    # lineage; they are parsed once per suite, not once per test (#290).
    "release-config": 9.0,
    # #289's ukb-b suite: one in-process pipeline run per test, like FinnGen's.
    "ukb-b-pipeline/pipeline-end-to-end": 2.0,
}

# Every suite gets single-threaded BLAS. A process that imports numpy, scipy
# or opengwasdb otherwise starts a thread per core: importing the Analysis
# model alone measured 0.3s of wall clock against 17s of user time, and with
# suites running side by side that contention costs far more than the threads
# win. No suite here does enough numerical work to gain from them.
THREAD_ENV: dict[str, str] = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

# Most processes wait on their own subprocesses, one at a time, so one per core
# is about right. The cap leaves a shared host's other cores to other work; at
# 64, `workflow` runs one process per test (74s for every suite, against 90s
# at 48 on the 224-core host).
DEFAULT_JOBS: int = min(64, os.cpu_count() or 1)

# The id of every test a script would run with the given arguments, one per
# line, without running any: unittest itself selects them, so `-k` and test
# names mean exactly what they mean to the script.
_LIST_TESTS: str = """
import os, runpy, sys, types, unittest

path, *args = sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
module = types.ModuleType("__listed__")
module.__dict__.update(runpy.run_path(path, run_name="__listed__"))


class Lister:
    def run(self, test):
        if isinstance(test, unittest.TestSuite):
            for each in test:
                self.run(each)
        else:
            print(f"{type(test).__qualname__}.{test._testMethodName}")


unittest.main(module=module, argv=[path, *args], testRunner=Lister(), exit=False)
"""


def list_tests(script: list[str]) -> list[str]:
    """`Class.test` for every test `script` (a file and its arguments) would run."""
    result = subprocess.run(
        [sys.executable, "-c", _LIST_TESTS, *script],
        cwd=REPO_ROOT, capture_output=True, text=True, env={**os.environ, **THREAD_ENV},
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not list the tests of {' '.join(script)}:\n{result.stdout}{result.stderr}")
    return result.stdout.split()


def shards(tests: list[str], count: int) -> list[list[str]]:
    """`tests`, in order, cut into `count` runs whose sizes differ by at most one.

    Runs of the listed order keep a class's tests together, so a class's
    `setUpClass` is repeated only where a cut falls inside it.
    """
    size, extra = divmod(len(tests), count)
    runs: list[list[str]] = []
    start = 0
    for n in range(count):
        end = start + size + (1 if n < extra else 0)
        runs.append(tests[start:end])
        start = end
    return runs


def jobs_for(label: str, script: list[str], jobs: int) -> list[tuple[list[str], list[str]]]:
    """The processes that run one suite: (the tests it names, command).

    A suite in `SHARDED_SUITES` runs as up to `jobs` processes, one per test
    while there are no more tests than that; every other suite is one process,
    naming no test. One that selects no test is refused, so a stale `-k` in
    `SPLITS` cannot pass by running nothing.
    """
    if label not in SHARDED_SUITES:
        return [([], script)]
    tests = list_tests(script)
    if not tests:
        raise RuntimeError(f"{' '.join(script)} selects no tests")
    return [(run, [script[0], *run]) for run in shards(tests, min(jobs, len(tests)))]


def run_job(label: str, kind: str, script: list[str]) -> tuple[bool, float, str]:
    """Run one process to completion, returning its verdict, elapsed time and output.

    Output is captured rather than streamed because processes run concurrently
    and interleaved output is unreadable. It is printed for failures only.

    Every process gets a Snakemake working directory of its own, where every
    Snakemake run it starts keeps its `.snakemake/` locks and metadata, instead
    of sharing the checkout's. A Snakemake run deletes that whole lock
    directory when it exits, and `--unlock` clears every lock in it.
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

    from ogstores.release import SNAKEMAKE_DIRECTORY_ENV

    snakemake_directory = tempfile.mkdtemp(prefix="ogstores-test-snakemake-")
    env = {**os.environ, **THREAD_ENV, SNAKEMAKE_DIRECTORY_ENV: snakemake_directory}
    start = time.monotonic()
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    finally:
        shutil.rmtree(snakemake_directory, ignore_errors=True)
    elapsed = time.monotonic() - start
    return result.returncode == 0, elapsed, result.stdout + result.stderr


USAGE: str = (
    "[--only python|r] [--suite LABEL]... [--skip-release-stack] [--list-release-stack] [--jobs N]"
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
    skip_release_stack = False
    jobs: int = DEFAULT_JOBS

    args = sys.argv[1:]
    while args:
        flag = args.pop(0)
        if flag == "--only" and args:
            only = args.pop(0)
        elif flag == "--suite" and args:
            named.append(args.pop(0))
        elif flag == "--skip-release-stack":
            skip_release_stack = True
        elif flag == "--list-release-stack":
            print(json.dumps(list(RELEASE_STACK_SUITES)))
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
        and not (skip_release_stack and s[0] in RELEASE_STACK_SUITES)
    ]
    if not suites:
        print("no suites match the selection", file=sys.stderr)
        return 2

    started = time.monotonic()

    # Each suite's processes, and the outcome of each once it has run.
    pending: dict[str, int] = {}
    outcomes: dict[str, list[tuple[list[str], bool, float, str]]] = {label: [] for label, _, _ in suites}
    queue: list[tuple[str, str, list[str], list[str]]] = []
    for label, kind, script in suites:
        try:
            parts = jobs_for(label, script, jobs)
        except RuntimeError as exc:
            outcomes[label].append(([], False, 0.0, str(exc)))
            continue
        pending[label] = len(parts)
        queue.extend((label, kind, tests, command) for tests, command in parts)

    def report(label: str) -> None:
        ok = all(passed for _, passed, _, _ in outcomes[label])
        longest = max((elapsed for _, _, elapsed, _ in outcomes[label]), default=0.0)
        across = f", {len(outcomes[label])} processes" if len(outcomes[label]) > 1 else ""
        print(f"  {'PASS' if ok else 'FAIL':4s}  {label}  ({longest:.1f}s{across})", flush=True)

    for label in outcomes:
        if label not in pending:
            report(label)

    # Processes are independent over their own fixtures, so they run
    # concurrently, longest first, so a slow one overlaps with the short ones
    # rather than starting last and running alone. Wall clock is then bounded
    # by the slowest single process rather than by their sum.
    print(f"running {len(suites)} suites as {len(queue)} processes, {jobs} at a time\n")
    queue.sort(key=lambda job: -SUITE_COST.get(job[0], 1.0) * max(1, len(job[2])))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(run_job, label, kind, command): (label, tests)
            for label, kind, tests, command in queue
        }
        for future in as_completed(futures):
            label, tests = futures[future]
            outcomes[label].append((tests, *future.result()))
            pending[label] -= 1
            if pending[label] == 0:
                report(label)

    wall = time.monotonic() - started

    failed = [label for label, _, _ in suites if not all(passed for _, passed, _, _ in outcomes[label])]
    for label in failed:
        for tests, passed, _, output in outcomes[label]:
            if passed:
                continue
            print("=" * 60)
            more = f" and {len(tests) - 1} more" if len(tests) > 1 else ""
            print(f"FAILED: {label}{f' ({tests[0]}{more})' if tests else ''}")
            print(output.rstrip())

    print("=" * 60)
    for label, _, _ in suites:
        report(label)
    serial = sum(elapsed for results in outcomes.values() for _, _, elapsed, _ in results)
    print(
        f"{len(suites) - len(failed)}/{len(suites)} suites passed "
        f"in {wall:.1f}s wall ({serial:.1f}s serial)"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
