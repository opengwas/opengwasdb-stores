#!/usr/bin/env python3
"""Release-bundle-level smoke test for AF-based ancestry assignment (issues
#23, #25). Run from the repository root:

  python3 tests/ancestry-assignment/run_tests.py

Regenerates the deterministic fixtures, runs ancestry-assign.py against them
exactly as a real Store Family would, and asserts on the emitted
release-bundle outputs (sidecar, analyses.tsv, validation.yaml) rather than on
internal helper functions, matching this repository's testing convention.

Requires numpy/scipy/opengwasdb, e.g. via `pixi run test` (feature: store-build).
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "ancestry-assignment" / "fixtures"
RELEASE_DIR = FIXTURES_DIR / "release"

n_checks = 0


def check(cond: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not cond:
        raise AssertionError(message)


def read_tsv(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["analysis_id"]: row for row in csv.DictReader(fh, delimiter="\t")}


def read_yaml_flat_section(path: Path, header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == header:
            in_section = True
            continue
        if in_section and line and not line.startswith(" "):
            break
        if in_section and ":" in line:
            key, value = line.strip().split(":", 1)
            out[key.strip()] = value.strip().strip("'\"")
    return out


def main() -> None:
    subprocess.run(
        [sys.executable, str(FIXTURES_DIR / "generate_fixtures.py")], cwd=REPO_ROOT, check=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "resources" / "generators" / "lib" / "source-formats" / "gwas-ssf-ragged" / "ancestry-assign.py"),
            f"--release-dir={RELEASE_DIR}",
        ],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise SystemExit("ancestry-assign.py exited non-zero")

    sidecar = read_tsv(RELEASE_DIR / "sidecars" / "ancestry.tsv")
    analyses = read_tsv(RELEASE_DIR / "analyses.tsv")
    checks = read_yaml_flat_section(RELEASE_DIR / "validation.yaml", "checks:")
    warnings_text = RELEASE_DIR.joinpath("validation.yaml").read_text(encoding="utf-8")

    # Clear dominant-population matches -> af_assigned.
    row = sidecar["FIXT_EUR"]
    check(row["gate_reason"] == "ok", f"FIXT_EUR expected gate_reason=ok, got {row['gate_reason']}")
    check(row["dominant_superpop"] == "EUR", f"FIXT_EUR expected dominant EUR, got {row['dominant_superpop']}")
    check(analyses["FIXT_EUR"]["ancestry_assignment_method"] == "af_assigned",
          "FIXT_EUR analyses.tsv should be updated to af_assigned")
    check(analyses["FIXT_EUR"]["assigned_ancestry"] == "EUR",
          "FIXT_EUR analyses.tsv assigned_ancestry should be EUR")

    row = sidecar["FIXT_AFR"]
    check(row["gate_reason"] == "ok" and row["dominant_superpop"] == "AFR",
          f"FIXT_AFR expected a clean AFR assignment, got {row}")

    # Ambiguous 55/45 mixture -> gated out on margin, explicit reason, unassigned.
    row = sidecar["FIXT_MIXED"]
    check(row["gate_reason"] == "margin", f"FIXT_MIXED expected gate_reason=margin, got {row['gate_reason']}")
    check(analyses["FIXT_MIXED"]["ancestry_assignment_method"] == "unassigned",
          "FIXT_MIXED should become unassigned, not silently keep its prior method")
    check(analyses["FIXT_MIXED"]["assigned_ancestry"] == "",
          "FIXT_MIXED assigned_ancestry should be cleared when gated out")

    # Too few overlapping variants -> explicit low-overlap gate reason.
    row = sidecar["FIXT_LOW_OVERLAP"]
    check(row["gate_reason"] == "overlap", f"FIXT_LOW_OVERLAP expected gate_reason=overlap, got {row['gate_reason']}")

    # No usable source AF -> skipped, left at whatever it already was.
    row = sidecar["FIXT_NO_AF"]
    check(row["gate_reason"] == "no_usable_source_af",
          f"FIXT_NO_AF expected gate_reason=no_usable_source_af, got {row['gate_reason']}")
    check(analyses["FIXT_NO_AF"]["ancestry_assignment_method"] == "source_trusted_no_af",
          "FIXT_NO_AF must be left untouched at source_trusted_no_af")

    # Source label disagrees with the AF-based call -> mismatch recorded.
    row = sidecar["FIXT_MISMATCH"]
    check(row["dominant_superpop"] == "AFR", f"FIXT_MISMATCH expected dominant AFR, got {row['dominant_superpop']}")
    check(row["source_assigned_mismatch"] == "true",
          "FIXT_MISMATCH (source=European, AF-based=AFR) should record a mismatch")

    # Reference super-population composition columns are present and dynamic.
    for sp in ("EUR", "AFR", "EAS", "SAS"):
        check(f"ancestry_prop_{sp}" in row, f"sidecar should have a dynamic ancestry_prop_{sp} column")

    # validation.yaml reflects empirical ancestry status without discarding it.
    check(checks.get("ancestry") == "passed_with_warnings",
          f"checks.ancestry should be passed_with_warnings, got {checks.get('ancestry')}")
    check("FIXT_MIXED" in warnings_text and "margin" in warnings_text,
          "validation.yaml warnings should mention the FIXT_MIXED margin gate failure")
    check("FIXT_MISMATCH" in warnings_text and "disagrees" in warnings_text,
          "validation.yaml warnings should mention the FIXT_MISMATCH source/assigned disagreement")

    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
