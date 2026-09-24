#!/usr/bin/env python3
"""Test orchestrator (issue #41): owns ordering and cleanup for every
checked-in lightweight test suite and runs each one under the interpreter
already selected by Pixi (`sys.executable` / `Rscript` on PATH), rather than
having individual test scripts search for a sibling `../opengwasdb/.venv`.

  pixi run test              # every suite
  pixi run test-python       # only the Python suites
  pixi run test-r            # only the R suites

Suites are data-only, network-free, and safe to run in CI; they are distinct
from the data-intensive LD-panel acquisition/materialization tasks.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# (label, kind, command relative to REPO_ROOT)
SUITES: list[tuple[str, str, list[str]]] = [
    ("ancestry-assignment", "python", ["tests/ancestry-assignment/run_tests.py"]),
    ("bundle", "python", ["tests/bundle/test_bundle.py"]),
    ("curation/gap-scan", "python", ["tests/curation/test_gap_scan.py"]),
    ("curation/candidates", "python", ["tests/curation/test_candidates.py"]),
    ("curation/choice", "python", ["tests/curation/test_choice.py"]),
    ("effect-scale-validation (R)", "r", ["tests/effect-scale-validation/run_tests.R"]),
    ("eqtlgen-besd-ragged", "python", ["tests/eqtlgen-besd-ragged/test_subset_besd.py"]),
    ("finngen-r13-pilot", "r", ["tests/finngen-r13-pilot/run_tests.R"]),
    ("finngen-r13-acquisition", "python", ["tests/finngen-r13-pilot/test_acquire.py"]),
    ("finngen-r13-annotation", "python", ["tests/finngen-r13-pilot/test_annotation.py"]),
    ("finngen-r13-assessment", "python", ["tests/finngen-r13-pilot/test_assessment.py"]),
    ("index", "python", ["tests/index/test_index.py"]),
    ("ld-panel-eigendecomposition", "python", ["tests/ld-panel-eigendecomposition/run_tests.py"]),
    ("ld-panel-generation", "python", ["tests/ld-panel-generation/run_tests.py"]),
    ("manifest", "python", ["tests/manifest/test_manifest.py"]),
    ("metadata-resolvers/gwas-catalog-ssf", "r", ["tests/metadata-resolvers/gwas-catalog-ssf/run_tests.R"]),
    ("metadata-resolvers/finngen-manifest", "r", ["tests/metadata-resolvers/finngen-manifest/run_tests.R"]),
    ("metadata-resolvers/opengwas-api", "r", ["tests/metadata-resolvers/opengwas-api/run_tests.R"]),
    ("metadata-resolvers/trait-ontology-mapping", "r", ["tests/metadata-resolvers/trait-ontology-mapping/run_tests.R"]),
    ("materialise-gwas-ssf-ragged", "r", ["tests/materialise-gwas-ssf-ragged/run_tests.R"]),
    ("no-cis-region-policy", "r", ["tests/no-cis-region-policy/run_tests.R"]),
    ("opengwas-gwas-vcf-dense", "r", ["tests/opengwas-gwas-vcf-dense/run_tests.R"]),
    ("opengwas-gwas-vcf-dense annotation", "python", ["tests/opengwas-gwas-vcf-dense/test_annotation.py"]),
    ("phase-b-candidate", "python", ["tests/phase-b-candidate/test_phase_b_candidate.py"]),
    ("plan", "python", ["tests/plan/test_plan.py"]),
    ("qc-panel-concordance", "python", ["tests/qc-panel-concordance/test_qc_panel_concordance.py"]),
    ("qc-panel-retention", "r", ["tests/qc-panel-retention/run_tests.R"]),
    ("reconcile-build-yaml", "python", ["tests/reconcile-build-yaml/test_reconcile_build_yaml.py"]),
    ("register", "python", ["tests/register/test_register.py"]),
    ("run", "python", ["tests/run/test_run.py"]),
    ("schema-validation", "r", ["tests/schema-validation/run_tests.R"]),
    ("source-inventory", "python", ["tests/source-inventory/test_source_inventory.py"]),
    ("validation-record", "python", ["tests/validation-record/test_validation_record.py"]),
    ("workflow", "python", ["tests/workflow/test_workflow.py"]),
]


# Most suites get single-threaded BLAS. A suite that imports numpy, scipy or
# opengwasdb otherwise spins up a thread pool per process: importing the Analysis
# model alone measured 0.3s of wall clock against 17s of user time, and with
# suites running concurrently that contention costs far more than the threads win.
#
# The exception is a suite that does real numerical work rather than just
# importing the libraries. Pinning `workflow`, which builds fixture-scale Stores
# through opengwasdb for real, took it from 43s to 122s. Measure before adding
# to this set: for every other suite here, pinning is free.
THREADED_SUITES: frozenset[str] = frozenset({"workflow"})

# Rough per-suite cost in seconds, used only to schedule the long suites first
# so they overlap with the short ones instead of starting last and running alone.
# Wrong values cost a little wall clock, never correctness; unlisted suites are
# assumed short.
SUITE_COST: dict[str, float] = {
    "workflow": 45.0,
    "qc-panel-retention": 6.0,
    "effect-scale-validation (R)": 6.0,
    "run": 5.0,
    "materialise-gwas-ssf-ragged": 4.0,
    "schema-validation": 4.0,
    "no-cis-region-policy": 3.5,
    "plan": 2.5,
    "opengwas-gwas-vcf-dense": 2.5,
    "finngen-r13-pilot": 2.5,
    "ld-panel-eigendecomposition": 2.0,
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


def main() -> int:
    only: str | None = None
    jobs: int = DEFAULT_JOBS

    args = sys.argv[1:]
    while args:
        flag = args.pop(0)
        if flag == "--only" and args:
            only = args.pop(0)
        elif flag == "--jobs" and args:
            jobs = max(1, int(args.pop(0)))
        else:
            print(f"usage: {sys.argv[0]} [--only python|r] [--jobs N]", file=sys.stderr)
            return 2

    suites = [s for s in SUITES if only is None or s[1] == only]
    if not suites:
        print(f"no suites match --only {only}", file=sys.stderr)
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
    exclusive = [s for s in suites if s[0] in THREADED_SUITES]

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
