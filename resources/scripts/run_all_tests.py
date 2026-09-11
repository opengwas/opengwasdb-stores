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

import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# (label, kind, command relative to REPO_ROOT)
SUITES: list[tuple[str, str, list[str]]] = [
    ("ancestry-assignment", "python", ["tests/ancestry-assignment/run_tests.py"]),
    ("build-yaml-migration", "python", ["tests/build-yaml-migration/run_tests.py"]),
    ("build-yaml-migration generators (R)", "r", ["tests/build-yaml-migration/run_tests.R"]),
    ("effect-scale-validation (R)", "r", ["tests/effect-scale-validation/run_tests.R"]),
    ("effect-scale-validation (merge)", "python", ["tests/effect-scale-validation/test_build_store_validation_merge.py"]),
    ("eqtlgen-besd-ragged", "python", ["tests/eqtlgen-besd-ragged/test_subset_besd.py"]),
    ("finngen-r13-pilot", "r", ["tests/finngen-r13-pilot/run_tests.R"]),
    ("finngen-r13-acquisition", "python", ["tests/finngen-r13-pilot/test_acquire.py"]),
    ("finngen-r13-annotation", "python", ["tests/finngen-r13-pilot/test_annotation.py"]),
    ("finngen-r13-store-envelope", "python", ["tests/finngen-r13-pilot/test_build_store.py"]),
    ("finngen-r13-assessment", "python", ["tests/finngen-r13-pilot/test_assessment.py"]),
    ("ld-panel-eigendecomposition", "python", ["tests/ld-panel-eigendecomposition/run_tests.py"]),
    ("ld-panel-generation", "python", ["tests/ld-panel-generation/run_tests.py"]),
    ("metadata-resolution", "python", ["tests/metadata-resolution/run_tests.py"]),
    ("metadata-resolvers/gwas-catalog-ssf", "r", ["tests/metadata-resolvers/gwas-catalog-ssf/run_tests.R"]),
    ("metadata-resolvers/finngen-manifest", "r", ["tests/metadata-resolvers/finngen-manifest/run_tests.R"]),
    ("metadata-resolvers/opengwas-api", "r", ["tests/metadata-resolvers/opengwas-api/run_tests.R"]),
    ("metadata-resolvers/trait-ontology-mapping", "r", ["tests/metadata-resolvers/trait-ontology-mapping/run_tests.R"]),
    ("no-cis-region-policy", "r", ["tests/no-cis-region-policy/run_tests.R"]),
    ("opengwas-gwas-vcf-dense", "r", ["tests/opengwas-gwas-vcf-dense/run_tests.R"]),
    ("opengwas-gwas-vcf-dense annotation", "python", ["tests/opengwas-gwas-vcf-dense/test_annotation.py"]),
    ("qc-panel-retention", "r", ["tests/qc-panel-retention/run_tests.R"]),
    ("release-manifest", "python", ["tests/release-manifest/run_tests.py"]),
    ("release-plan", "python", ["tests/release-plan/run_tests.py"]),
    ("release-workflow", "python", ["tests/release-workflow/run_tests.py"]),
    ("schema-validation", "r", ["tests/schema-validation/run_tests.R"]),
]


def run_suite(label: str, kind: str, script: list[str]) -> tuple[bool, float]:
    if kind == "python":
        cmd = [sys.executable, *script]
    elif kind == "r":
        rscript = shutil.which("Rscript")
        if rscript is None:
            print(f"==> {label}: SKIPPED (Rscript not found on PATH)")
            return False, 0.0
        cmd = [rscript, *script]
    else:
        raise ValueError(f"unknown suite kind: {kind}")

    print(f"==> {label}  ({' '.join(cmd)})")
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    elapsed = time.monotonic() - start
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"<== {label}: {status} ({elapsed:.1f}s)\n")
    return result.returncode == 0, elapsed


def main() -> int:
    only = None
    if len(sys.argv) > 1:
        if sys.argv[1] == "--only" and len(sys.argv) > 2:
            only = sys.argv[2]
        else:
            print(f"usage: {sys.argv[0]} [--only python|r]", file=sys.stderr)
            return 2

    suites = [s for s in SUITES if only is None or s[1] == only]
    if not suites:
        print(f"no suites match --only {only}", file=sys.stderr)
        return 2

    results = [(label, *run_suite(label, kind, script)) for label, kind, script in suites]

    total_time = sum(elapsed for _, _, elapsed in results)
    failed = [label for label, ok, _ in results if not ok]

    print("=" * 60)
    for label, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL':4s}  {label}  ({elapsed:.1f}s)")
    print(f"{len(results) - len(failed)}/{len(results)} suites passed in {total_time:.1f}s")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
