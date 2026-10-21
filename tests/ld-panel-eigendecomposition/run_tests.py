#!/usr/bin/env python3
"""Smoke test for the eigendecomposition backfill tooling (issue #34):
tools/ld-panel/backfill_eigendecomposition.py and
tools/ld-panel/find_underresolved_blocks.py. Run from the
repository root:

  python3 tests/ld-panel-eigendecomposition/run_tests.py

Regenerates a tiny synthetic panel (fixtures/generate_fixtures.py) and drives
both scripts as subprocesses exactly as a real backfill run would, asserting
on the npz files they produce rather than on internal helper functions,
matching this repository's testing convention (see
tests/materialise-gwas-ssf-ragged/run_tests.R, which drives its script the
same way).

Requires numpy/scipy, e.g. via `pixi run test` (feature: python).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tools" / "ld-panel"
FIXTURES_DIR = REPO_ROOT / "tests" / "ld-panel-eigendecomposition" / "fixtures"
PANEL_DIR = FIXTURES_DIR / "panel"
CHROM_DIR = PANEL_DIR / "EUR" / "1"

n_checks = 0


def check(cond: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not cond:
        raise AssertionError(message)


def run(python: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(python), str(SCRIPTS_DIR / script), *args],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"{script} {' '.join(args)} exited {result.returncode}\n{result.stderr}")
    return result


def load_npz(base: str, suffix: str) -> dict:
    return dict(np.load(CHROM_DIR / f"{base}{suffix}.npz"))


def assert_same_decomposition(a: dict, b: dict, label: str) -> None:
    check(np.array_equal(a["values"], b["values"]), f"{label}: eigenvalues differ between sequential and parallel runs")
    check(a["vectors"].shape == b["vectors"].shape, f"{label}: stored component count differs")
    # LAPACK's sign convention for an eigenvector is arbitrary but deterministic
    # per call; align each column's sign before comparing so a legitimate
    # sign flip (rather than a real numerical divergence) doesn't fail the test.
    for j in range(a["vectors"].shape[1]):
        va, vb = a["vectors"][:, j], b["vectors"][:, j]
        sign = np.sign(np.dot(va, vb)) or 1.0
        check(
            np.allclose(va, sign * vb, atol=1e-10),
            f"{label}: eigenvector column {j} differs between sequential and parallel runs",
        )


def main() -> None:
    subprocess.run(
        [sys.executable, str(FIXTURES_DIR / "generate_fixtures.py")], cwd=REPO_ROOT, check=True,
    )
    python = Path(sys.executable)

    # --- find_underresolved_blocks.py: only the deliberately under-stored
    # block (1000-1040, 1 component stored vs 2 needed at thresh=0.9) should
    # be flagged. 2000-2030 stores all its components (resolved); 3000-3060
    # has no .ldeig.npz yet, so it is "missing" rather than "under-resolved"
    # and the scanner (which globs *.ldeig.npz) must not see it at all.
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "underresolved.txt"
        run(
            python, "find_underresolved_blocks.py",
            "--panel", str(PANEL_DIR), "--ancestry", "EUR", "--thresh", "0.9", "--out", str(out_path),
        )
        flagged = [Path(line).name for line in out_path.read_text().splitlines() if line.strip()]
        check(flagged == ["1000-1040"], f"expected only 1000-1040 flagged as under-resolved, got {flagged}")

    # --- backfill without --force: only the missing block (3000-3060) should
    # be built; the already-under-resolved 1000-1040 and already-resolved
    # 2000-2030 must be left untouched (no --force given).
    run(python, "backfill_eigendecomposition.py", "--panel", str(PANEL_DIR), "--ancestry", "EUR", "--jobs", "1")
    check((CHROM_DIR / "3000-3060.ldeig.npz").exists(), "backfill did not build the missing 3000-3060 block")
    d = load_npz("1000-1040", ".ldeig")
    check(d["vectors"].shape[1] == 1, "backfill overwrote 1000-1040 without --force")
    d = load_npz("2000-2030", ".ldeig")
    check(d["vectors"].shape[1] == 6, "backfill overwrote 2000-2030 without --force")

    # --- --dry-run must not write anything.
    dry_target = CHROM_DIR / "1000-1040.ldeig-dryrun.npz"
    run(
        python, "backfill_eigendecomposition.py", "--panel", str(PANEL_DIR), "--ancestry", "EUR",
        "--force", "--out-suffix", ".ldeig-dryrun", "--dry-run", "--jobs", "1",
    )
    check(not dry_target.exists(), "--dry-run wrote an npz file")

    # --- sequential vs. parallel: rebuild all four blocks twice (--jobs 1
    # then --jobs 4) with a small --min-k so --cumvar actually drives
    # truncation (the default --min-k=250 would just store every component
    # of these tiny fixture blocks). Outputs must match block-for-block. The
    # fourth block (9000-9010) has a ragged matrix on purpose: it must fail
    # and be reported, without stopping the other three from succeeding —
    # per-block error isolation across the worker pool (issue #34), same
    # property the R precedent (generate.R's mclapply pool, issue #32)
    # verified for its own parallel fan-out.
    good_blocks = ["1000-1040", "2000-2030", "3000-3060"]
    all_blocks = good_blocks + ["9000-9010"]
    blocks_file = CHROM_DIR.parent.parent / "all_blocks.txt"
    blocks_file.write_text("\n".join(str(CHROM_DIR / name) for name in all_blocks) + "\n")
    common_args = [
        "--panel", str(PANEL_DIR), "--ancestry", "EUR", "--force",
        "--blocks", str(blocks_file), "--cumvar", "0.9", "--min-k", "1",
    ]
    seq_result = run(python, "backfill_eigendecomposition.py", *common_args, "--out-suffix", ".ldeig-seq", "--jobs", "1")
    par_result = run(python, "backfill_eigendecomposition.py", *common_args, "--out-suffix", ".ldeig-par", "--jobs", "4")

    for result, label in [(seq_result, "sequential"), (par_result, "parallel")]:
        check("9000-9010" in result.stdout and "failed" in result.stdout.lower(), f"{label} run did not report the ragged block as failed")

    expected_k = {"1000-1040": 2, "2000-2030": 2, "3000-3060": 9}
    for name, k in expected_k.items():
        seq, par = load_npz(name, ".ldeig-seq"), load_npz(name, ".ldeig-par")
        check(seq["vectors"].shape[1] == k, f"{name}: sequential run stored k={seq['vectors'].shape[1]}, expected {k}")
        assert_same_decomposition(seq, par, name)
    check(not (CHROM_DIR / "9000-9010.ldeig-seq.npz").exists(), "ragged block should not have produced a sequential npz")
    check(not (CHROM_DIR / "9000-9010.ldeig-par.npz").exists(), "ragged block should not have produced a parallel npz")

    # --- verify: rescanning the rebuilt (.ldeig-par) suffix at thresh=0.9
    # should report zero under-resolved blocks, matching the issue's "step 3"
    # workflow (rebuild alongside, then verify before swapping).
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "underresolved.txt"
        run(
            python, "find_underresolved_blocks.py",
            "--panel", str(PANEL_DIR), "--ancestry", "EUR", "--thresh", "0.9",
            "--suffix", ".ldeig-par", "--out", str(out_path),
        )
        check(out_path.read_text().strip() == "", "rebuilt blocks still under-resolved after backfill")

    blocks_file.unlink()
    print(f"ok — {n_checks} checks passed")


if __name__ == "__main__":
    main()
